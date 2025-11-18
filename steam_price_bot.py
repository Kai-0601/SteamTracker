import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiohttp
import asyncio
import sqlite3
from datetime import datetime
import os
import logging
from dotenv import load_dotenv
from aiohttp import web

# 載入環境變數
load_dotenv()

# ==================== 日誌設定 ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('SteamBot')

# ==================== 設定區 ====================
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
STEAM_STORE_API = "https://store.steampowered.com/api/appdetails"
PORT = int(os.getenv('PORT', 8080))

# 初始化 Bot
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
bot = commands.Bot(command_prefix='/', intents=intents)

# ==================== Steam 特賣活動資訊 ====================
STEAM_SALES_CALENDAR = {
    "春季特賣": {"month": 3, "start_day": 14, "duration": 14, "emoji": "🌸"},
    "夏季特賣": {"month": 6, "start_day": 23, "duration": 14, "emoji": "☀️"},
    "秋季特賣": {"month": 11, "start_day": 21, "duration": 14, "emoji": "🍂"},
    "冬季特賣": {"month": 12, "start_day": 20, "duration": 14, "emoji": "❄️"},
    "農曆新年特賣": {"month": 2, "start_day": 1, "duration": 7, "emoji": "🧧"},
    "萬聖節特賣": {"month": 10, "start_day": 28, "duration": 7, "emoji": "🎃"},
}

# ==================== 健康檢查伺服器 (Render 需要) ====================
async def health_check(request):
    """健康檢查端點"""
    return web.Response(text="Bot is running!", status=200)

async def start_web_server():
    """啟動 Web 伺服器供 Render 健康檢查"""
    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_get('/health', health_check)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"Web 伺服器已啟動在 port {PORT}")

# ==================== 資料庫功能 ====================
def init_db():
    """初始化資料庫"""
    try:
        conn = sqlite3.connect('steam_prices.db')
        c = conn.cursor()
        
        # 價格歷史表
        c.execute('''CREATE TABLE IF NOT EXISTS price_history
                     (app_id INTEGER, region TEXT, price REAL, discount INTEGER, 
                      timestamp TEXT, PRIMARY KEY (app_id, region, timestamp))''')
        
        # 追蹤遊戲表
        c.execute('''CREATE TABLE IF NOT EXISTS tracked_games
                     (app_id INTEGER PRIMARY KEY, name TEXT, last_check TEXT, 
                      is_free BOOLEAN, image_url TEXT)''')
        
        # 歷史最低價表
        c.execute('''CREATE TABLE IF NOT EXISTS historical_low
                     (app_id INTEGER, region TEXT, lowest_price REAL, date TEXT, 
                      PRIMARY KEY (app_id, region))''')
        
        # 伺服器設定表 (移除 alert_threshold)
        c.execute('''CREATE TABLE IF NOT EXISTS server_settings
                     (guild_id INTEGER PRIMARY KEY, notification_channel_id INTEGER, 
                      setup_date TEXT, enable_sale_notifications BOOLEAN DEFAULT 1)''')
        
        # 歷史新低事件表
        c.execute('''CREATE TABLE IF NOT EXISTS new_low_events
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, app_id INTEGER, game_name TEXT, 
                      region TEXT, price REAL, currency TEXT, date TEXT, notified BOOLEAN DEFAULT 1)''')
        
        # 免費遊戲事件表
        c.execute('''CREATE TABLE IF NOT EXISTS free_game_events
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, app_id INTEGER, game_name TEXT, 
                      date TEXT, notified BOOLEAN DEFAULT 1)''')
        
        # 用戶願望清單表
        c.execute('''CREATE TABLE IF NOT EXISTS user_wishlist
                     (user_id INTEGER, app_id INTEGER, added_date TEXT, 
                      target_price REAL, PRIMARY KEY (user_id, app_id))''')
        
        # 特賣活動通知表
        c.execute('''CREATE TABLE IF NOT EXISTS sale_notifications
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                      sale_name TEXT, 
                      notification_date TEXT, 
                      year INTEGER,
                      UNIQUE(sale_name, year))''')
        
        conn.commit()
        logger.info("資料庫初始化成功")
    except Exception as e:
        logger.error(f"資料庫初始化失敗: {e}")
    finally:
        conn.close()

# ==================== 伺服器設定功能 ====================
def set_notification_channel(guild_id: int, channel_id: int, enable_sale: bool = True):
    """設定伺服器的通知頻道"""
    try:
        conn = sqlite3.connect('steam_prices.db')
        c = conn.cursor()
        c.execute('INSERT OR REPLACE INTO server_settings VALUES (?, ?, ?, ?)',
                  (guild_id, channel_id, datetime.now().isoformat(), enable_sale))
        conn.commit()
        logger.info(f"伺服器 {guild_id} 設定通知頻道: {channel_id}")
    except Exception as e:
        logger.error(f"設定通知頻道失敗: {e}")
    finally:
        conn.close()

def get_all_notification_channels():
    """獲取所有已設定的通知頻道"""
    try:
        conn = sqlite3.connect('steam_prices.db')
        c = conn.cursor()
        c.execute('SELECT guild_id, notification_channel_id, enable_sale_notifications FROM server_settings')
        results = c.fetchall()
        return results
    except Exception as e:
        logger.error(f"獲取通知頻道失敗: {e}")
        return []
    finally:
        conn.close()

# ==================== 特賣活動功能 ====================
def check_upcoming_sales():
    """檢查即將到來的 Steam 特賣"""
    now = datetime.now()
    upcoming_sales = []
    
    for sale_name, info in STEAM_SALES_CALENDAR.items():
        # 計算特賣開始日期
        sale_date = datetime(now.year, info['month'], info['start_day'])
        
        # 如果今年的已經過了,檢查明年的
        if sale_date < now:
            sale_date = datetime(now.year + 1, info['month'], info['start_day'])
        
        # 計算距離天數
        days_until = (sale_date - now).days
        
        # 如果在 7 天內即將開始
        if 0 <= days_until <= 7:
            upcoming_sales.append({
                'name': sale_name,
                'date': sale_date,
                'days_until': days_until,
                'emoji': info['emoji'],
                'duration': info['duration']
            })
    
    return upcoming_sales

def is_sale_notified(sale_name: str, year: int) -> bool:
    """檢查該特賣是否已通知過"""
    try:
        conn = sqlite3.connect('steam_prices.db')
        c = conn.cursor()
        c.execute('SELECT id FROM sale_notifications WHERE sale_name=? AND year=?', 
                  (sale_name, year))
        result = c.fetchone()
        return result is not None
    except Exception as e:
        logger.error(f"檢查特賣通知失敗: {e}")
        return False
    finally:
        conn.close()

def mark_sale_notified(sale_name: str, year: int):
    """標記特賣已通知"""
    try:
        conn = sqlite3.connect('steam_prices.db')
        c = conn.cursor()
        c.execute('INSERT OR IGNORE INTO sale_notifications (sale_name, notification_date, year) VALUES (?, ?, ?)',
                  (sale_name, datetime.now().isoformat(), year))
        conn.commit()
        logger.info(f"標記特賣通知: {sale_name} {year}")
    except Exception as e:
        logger.error(f"標記特賣通知失敗: {e}")
    finally:
        conn.close()

# ==================== Steam API 功能 ====================
async def get_steam_game_info(app_id: int, region: str = 'tw'):
    """獲取遊戲的詳細資訊和價格"""
    async with aiohttp.ClientSession() as session:
        url = f"{STEAM_STORE_API}?appids={app_id}&cc={region}"
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get(str(app_id), {}).get('success'):
                        return data[str(app_id)]['data']
        except asyncio.TimeoutError:
            logger.error(f"獲取遊戲 {app_id} 資訊超時")
        except Exception as e:
            logger.error(f"獲取遊戲資訊錯誤 {app_id}: {e}")
    return None

async def get_multi_region_prices(app_id: int):
    """獲取遊戲在不同國家的價格"""
    regions = {
        'tw': '台灣', 'us': '美國', 'uk': '英國', 'jp': '日本',
        'cn': '中國', 'kr': '韓國', 'hk': '香港', 'ar': '阿根廷', 'tr': '土耳其'
    }
    
    prices = {}
    async with aiohttp.ClientSession() as session:
        tasks = []
        for code, name in regions.items():
            url = f"{STEAM_STORE_API}?appids={app_id}&cc={code}"
            tasks.append(fetch_price(session, url, code, name))
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if result and not isinstance(result, Exception):
                prices[result['name']] = result
    
    return prices

async def fetch_price(session, url: str, code: str, name: str):
    """獲取單一地區價格"""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json()
                app_id = list(data.keys())[0]
                if data[app_id].get('success'):
                    game_data = data[app_id]['data']
                    
                    if game_data.get('is_free'):
                        return {
                            'code': code, 'name': name, 'price': 0,
                            'currency': 'FREE', 'discount': 0, 'is_free': True
                        }
                    
                    price_overview = game_data.get('price_overview', {})
                    if price_overview:
                        return {
                            'code': code, 'name': name,
                            'price': price_overview.get('final', 0) / 100,
                            'original_price': price_overview.get('initial', 0) / 100,
                            'currency': price_overview.get('currency', ''),
                            'discount': price_overview.get('discount_percent', 0),
                            'is_free': False
                        }
    except Exception as e:
        logger.error(f"獲取 {name} 價格錯誤: {e}")
    return None

# ==================== 價格檢查功能 ====================
def check_historical_low(app_id: int, region: str, current_price: float) -> tuple:
    """檢查當前價格是否為歷史新低
    
    返回: (是否新低, 舊的最低價, 價格降低百分比)
    """
    try:
        conn = sqlite3.connect('steam_prices.db')
        c = conn.cursor()
        
        c.execute('SELECT lowest_price FROM historical_low WHERE app_id=? AND region=?',
                  (app_id, region))
        result = c.fetchone()
        
        is_new_low = False
        old_price = None
        price_drop_percent = 0
        
        if result is None:
            # 第一次記錄
            c.execute('INSERT INTO historical_low VALUES (?, ?, ?, ?)',
                      (app_id, region, current_price, datetime.now().isoformat()))
            is_new_low = True
        elif current_price < result[0] and current_price > 0:
            # 發現新低
            old_price = result[0]
            price_drop_percent = ((old_price - current_price) / old_price) * 100
            c.execute('UPDATE historical_low SET lowest_price=?, date=? WHERE app_id=? AND region=?',
                      (current_price, datetime.now().isoformat(), app_id, region))
            is_new_low = True
        
        conn.commit()
        return (is_new_low, old_price, price_drop_percent)
    except Exception as e:
        logger.error(f"檢查歷史新低失敗: {e}")
        return (False, None, 0)
    finally:
        conn.close()

def check_free_game(app_id: int, is_currently_free: bool) -> bool:
    """檢查遊戲是否從付費變成免費"""
    try:
        conn = sqlite3.connect('steam_prices.db')
        c = conn.cursor()
        
        c.execute('SELECT is_free FROM tracked_games WHERE app_id=?', (app_id,))
        result = c.fetchone()
        
        became_free = False
        if result is not None:
            was_free = result[0]
            if not was_free and is_currently_free:
                became_free = True
                c.execute('UPDATE tracked_games SET is_free=? WHERE app_id=?',
                          (is_currently_free, app_id))
        
        conn.commit()
        return became_free
    except Exception as e:
        logger.error(f"檢查免費遊戲失敗: {e}")
        return False
    finally:
        conn.close()

def add_tracked_game(app_id: int, name: str, is_free: bool = False, image_url: str = None):
    """添加遊戲到追蹤列表"""
    try:
        conn = sqlite3.connect('steam_prices.db')
        c = conn.cursor()
        c.execute('INSERT OR REPLACE INTO tracked_games VALUES (?, ?, ?, ?, ?)',
                  (app_id, name, datetime.now().isoformat(), is_free, image_url))
        conn.commit()
        logger.info(f"添加追蹤遊戲: {name}")
    except Exception as e:
        logger.error(f"添加追蹤遊戲失敗: {e}")
    finally:
        conn.close()

def record_price(app_id: int, region: str, price: float, discount: int):
    """記錄價格到資料庫"""
    try:
        conn = sqlite3.connect('steam_prices.db')
        c = conn.cursor()
        c.execute('INSERT OR REPLACE INTO price_history VALUES (?, ?, ?, ?, ?)',
                  (app_id, region, price, discount, datetime.now().isoformat()))
        conn.commit()
    except Exception as e:
        logger.error(f"記錄價格失敗: {e}")
    finally:
        conn.close()

def record_new_low_event(app_id: int, game_name: str, region: str, price: float, currency: str):
    """記錄歷史新低事件"""
    try:
        conn = sqlite3.connect('steam_prices.db')
        c = conn.cursor()
        c.execute('INSERT INTO new_low_events (app_id, game_name, region, price, currency, date) VALUES (?, ?, ?, ?, ?, ?)',
                  (app_id, game_name, region, price, currency, datetime.now().isoformat()))
        conn.commit()
        logger.info(f"記錄新低事件: {game_name} - {price} {currency}")
    except Exception as e:
        logger.error(f"記錄新低事件失敗: {e}")
    finally:
        conn.close()

def record_free_game_event(app_id: int, game_name: str):
    """記錄免費遊戲事件"""
    try:
        conn = sqlite3.connect('steam_prices.db')
        c = conn.cursor()
        c.execute('INSERT INTO free_game_events (app_id, game_name, date) VALUES (?, ?, ?)',
                  (app_id, game_name, datetime.now().isoformat()))
        conn.commit()
        logger.info(f"記錄免費遊戲事件: {game_name}")
    except Exception as e:
        logger.error(f"記錄免費遊戲事件失敗: {e}")
    finally:
        conn.close()

def get_historical_low_price(app_id: int, region: str = 'tw'):
    """獲取歷史最低價"""
    try:
        conn = sqlite3.connect('steam_prices.db')
        c = conn.cursor()
        c.execute('SELECT lowest_price, date FROM historical_low WHERE app_id=? AND region=?',
                  (app_id, region))
        result = c.fetchone()
        return result
    except Exception as e:
        logger.error(f"獲取歷史最低價失敗: {e}")
        return None
    finally:
        conn.close()

# ==================== 監控任務 ====================
@tasks.loop(hours=1)
async def monitor_prices():
    """定期監控遊戲價格 - 只通知歷史新低和免費遊戲"""
    logger.info("開始監控價格...")
    
    try:
        conn = sqlite3.connect('steam_prices.db')
        c = conn.cursor()
        c.execute('SELECT app_id, name FROM tracked_games')
        games = c.fetchall()
        conn.close()
        
        # 獲取所有已設定的通知頻道
        channels_info = get_all_notification_channels()
        if not channels_info:
            logger.warning("沒有設定任何通知頻道")
            return
        
        for app_id, name in games:
            try:
                game_info = await get_steam_game_info(app_id, 'tw')
                if not game_info:
                    continue
                
                # 檢查是否免費
                is_free = game_info.get('is_free', False)
                if check_free_game(app_id, is_free):
                    record_free_game_event(app_id, name)
                    embed = discord.Embed(
                        title="🎁 免費遊戲通知",
                        description=f"**{name}** 現在可以免費領取!",
                        color=discord.Color.green(),
                        url=f"https://store.steampowered.com/app/{app_id}"
                    )
                    embed.add_field(name="💡 提示", value="限時免費,快去領取!", inline=False)
                    
                    if 'header_image' in game_info:
                        embed.set_image(url=game_info['header_image'])
                    
                    embed.timestamp = datetime.now()
                    
                    for guild_id, channel_id, enable_sale in channels_info:
                        channel = bot.get_channel(channel_id)
                        if channel:
                            await channel.send(embed=embed)
                
                # 檢查價格 - 只通知歷史新低
                if not is_free and 'price_overview' in game_info:
                    price_data = game_info['price_overview']
                    current_price = price_data['final'] / 100
                    currency = price_data['currency']
                    discount = price_data.get('discount_percent', 0)
                    
                    # 記錄價格
                    record_price(app_id, 'tw', current_price, discount)
                    
                    # 檢查是否為歷史新低
                    is_new_low, old_price, price_drop_percent = check_historical_low(app_id, 'tw', current_price)
                    
                    if is_new_low:
                        # 記錄事件
                        record_new_low_event(app_id, name, '台灣', current_price, currency)
                        
                        # 創建通知訊息
                        embed = discord.Embed(
                            title="🔥 歷史新低價格通知!",
                            description=f"**{name}** 在台灣達到歷史新低價格!",
                            color=discord.Color.red(),
                            url=f"https://store.steampowered.com/app/{app_id}"
                        )
                        
                        # 顯示當前價格
                        price_text = f"**{currency} {current_price:.2f}**"
                        if discount > 0:
                            price_text += f" (-{discount}%)"
                        embed.add_field(name="💰 現在價格", value=price_text, inline=True)
                        
                        # 如果有舊價格,顯示降價幅度
                        if old_price:
                            embed.add_field(name="📉 降價幅度", value=f"{price_drop_percent:.1f}%", inline=True)
                            embed.add_field(name="📊 之前最低價", value=f"{currency} {old_price:.2f}", inline=True)
                        
                        embed.add_field(name="💡 提示", value="這是有史以來的最低價格,不要錯過!", inline=False)
                        
                        if 'header_image' in game_info:
                            embed.set_thumbnail(url=game_info['header_image'])
                        
                        embed.timestamp = datetime.now()
                        embed.set_footer(text="Steam 價格監控 Bot")
                        
                        # 發送到所有通知頻道
                        for guild_id, channel_id, enable_sale in channels_info:
                            channel = bot.get_channel(channel_id)
                            if channel:
                                await channel.send(embed=embed)
                                logger.info(f"發送歷史新低通知: {name} - {current_price} {currency}")
                
                await asyncio.sleep(2)  # 避免 API 限制
                
            except Exception as e:
                logger.error(f"監控遊戲 {app_id} 錯誤: {e}")
        
        logger.info("價格監控完成")
        
    except Exception as e:
        logger.error(f"監控任務錯誤: {e}")

@tasks.loop(hours=12)
async def check_sales_calendar():
    """檢查 Steam 特賣活動"""
    logger.info("檢查 Steam 特賣活動...")
    
    try:
        upcoming_sales = check_upcoming_sales()
        
        if not upcoming_sales:
            logger.info("近期沒有即將開始的特賣活動")
            return
        
        channels_info = get_all_notification_channels()
        if not channels_info:
            logger.warning("沒有設定任何通知頻道")
            return
        
        for sale in upcoming_sales:
            # 檢查是否已通知過
            if is_sale_notified(sale['name'], sale['date'].year):
                continue
            
            # 創建通知訊息
            embed = discord.Embed(
                title=f"{sale['emoji']} Steam {sale['name']} 即將開始!",
                description=f"準備好你的錢包了嗎?",
                color=discord.Color.gold()
            )
            
            if sale['days_until'] == 0:
                time_text = "**今天開始!**"
            elif sale['days_until'] == 1:
                time_text = "**明天開始!**"
            else:
                time_text = f"**{sale['days_until']} 天後開始**"
            
            embed.add_field(
                name="開始時間",
                value=f"{time_text}\n{sale['date'].strftime('%Y年%m月%d日')}",
                inline=True
            )
            
            embed.add_field(
                name="特賣期間",
                value=f"{sale['duration']} 天",
                inline=True
            )

            embed.set_footer(text="Steam 特賣活動提醒")
            embed.timestamp = datetime.now()
            
            # 發送到所有啟用特賣通知的頻道
            for guild_id, channel_id, enable_sale in channels_info:
                if enable_sale:
                    channel = bot.get_channel(channel_id)
                    if channel:
                        await channel.send(embed=embed)
                        logger.info(f"發送特賣通知到頻道 {channel_id}: {sale['name']}")
            
            # 標記為已通知
            mark_sale_notified(sale['name'], sale['date'].year)
        
        logger.info("特賣活動檢查完成")
        
    except Exception as e:
        logger.error(f"檢查特賣活動錯誤: {e}")

@monitor_prices.before_loop
async def before_monitor():
    await bot.wait_until_ready()

@check_sales_calendar.before_loop
async def before_check_sales():
    await bot.wait_until_ready()

# ==================== Bot 事件 ====================
@bot.event
async def on_ready():
    logger.info(f'{bot.user} 已連線!')
    print(f'✅ {bot.user} 已連線!')
    print(f'✅ Bot ID: {bot.user.id}')
    print(f'✅ 在 {len(bot.guilds)} 個伺服器中')
    
    init_db()
    
    # 啟動 Web 伺服器
    bot.loop.create_task(start_web_server())
    
    # 同步 Slash Commands
    try:
        print('⏳ 正在同步斜線指令...')
        synced = await bot.tree.sync()
        logger.info(f"同步了 {len(synced)} 個斜線指令")
        print(f'✅ 成功同步 {len(synced)} 個斜線指令')
        for cmd in synced:
            print(f'   - /{cmd.name}')
    except Exception as e:
        logger.error(f"同步指令錯誤: {e}")
        print(f'❌ 同步指令失敗: {e}')
    
    if not monitor_prices.is_running():
        monitor_prices.start()
        print('✅ 價格監控任務已啟動')
    
    if not check_sales_calendar.is_running():
        check_sales_calendar.start()
        print('✅ 特賣日曆檢查已啟動')
    
    print('\n' + '='*50)
    print('🎮 Steam 價格監控 Bot 已就緒!')
    print('='*50)

# ==================== Slash Commands ====================

@bot.tree.command(name="設定頻道", description="設定遊戲價格通知頻道")
@app_commands.describe(
    頻道="選擇要接收通知的頻道",
    特賣通知="是否接收 Steam 特賣活動通知"
)
async def setup_channel(interaction: discord.Interaction, 頻道: discord.TextChannel, 特賣通知: bool = True):
    """設定通知頻道"""
    try:
        set_notification_channel(interaction.guild.id, 頻道.id, 特賣通知)
        
        embed = discord.Embed(
            title="✅ 設定成功",
            description=f"已將 {頻道.mention} 設定為價格通知頻道",
            color=discord.Color.green()
        )
        embed.add_field(name="📢 通知類型", value="✅ 歷史新低價格通知\n✅ 免費遊戲通知", inline=True)
        embed.add_field(name="📅 特賣通知", value="✅ 已啟用" if 特賣通知 else "❌ 已停用", inline=True)
        embed.add_field(name="💡 提示", value="Bot 只會在遊戲達到**歷史新低**時通知你!", inline=False)
        
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        logger.error(f"設定頻道錯誤: {e}")
        await interaction.response.send_message(f"❌ 設定失敗: {str(e)}")

@bot.tree.command(name="追蹤", description="添加遊戲到追蹤列表")
@app_commands.describe(app_id="Steam 遊戲的 App ID")
async def track_game(interaction: discord.Interaction, app_id: int):
    """添加遊戲到追蹤列表"""
    await interaction.response.defer()
    
    try:
        game_info = await get_steam_game_info(app_id)
        if game_info:
            name = game_info.get('name', 'Unknown')
            is_free = game_info.get('is_free', False)
            image_url = game_info.get('header_image', None)
            
            # 獲取當前價格和歷史最低價
            current_price_info = None
            historical_low_info = get_historical_low_price(app_id, 'tw')
            
            if not is_free and 'price_overview' in game_info:
                price_data = game_info['price_overview']
                current_price = price_data['final'] / 100
                currency = price_data['currency']
                discount = price_data.get('discount_percent', 0)
                current_price_info = (current_price, currency, discount)
            
            add_tracked_game(app_id, name, is_free, image_url)
            
            embed = discord.Embed(
                title="✅ 已添加到追蹤列表",
                description=f"**{name}**",
                color=discord.Color.green(),
                url=f"https://store.steampowered.com/app/{app_id}"
            )
            
            if image_url:
                embed.set_thumbnail(url=image_url)
            
            if is_free:
                embed.add_field(name="狀態", value="🎁 免費遊戲", inline=True)
            else:
                if current_price_info:
                    price, currency, discount = current_price_info
                    price_text = f"{currency} {price:.2f}"
                    if discount > 0:
                        price_text += f" (-{discount}%)"
                    embed.add_field(name="💰 當前價格", value=price_text, inline=True)
                
                if historical_low_info:
                    low_price, low_date = historical_low_info
                    embed.add_field(name="📊 歷史最低價", value=f"NT$ {low_price:.2f}", inline=True)
            
            embed.add_field(name="📢 通知說明", value="Bot 會在此遊戲達到**歷史新低**時通知你", inline=False)
            
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send("❌ 無法獲取遊戲資訊,請確認 App ID 是否正確")
    except Exception as e:
        logger.error(f"追蹤遊戲錯誤: {e}")
        await interaction.followup.send(f"❌ 操作失敗: {str(e)}")

@bot.tree.command(name="價格", description="查詢遊戲在各國的價格")
@app_commands.describe(app_id="Steam 遊戲的 App ID")
async def check_price(interaction: discord.Interaction, app_id: int):
    """查詢遊戲在各國的價格"""
    await interaction.response.defer()
    
    try:
        game_info = await get_steam_game_info(app_id, 'tw')
        if not game_info:
            await interaction.followup.send("❌ 無法獲取遊戲資訊")
            return
        
        prices = await get_multi_region_prices(app_id)
        if prices:
            embed = discord.Embed(
                title=game_info.get('name', 'Unknown Game'),
                color=discord.Color.blue(),
                url=f"https://store.steampowered.com/app/{app_id}"
            )
            
            if 'header_image' in game_info:
                embed.set_thumbnail(url=game_info['header_image'])
            
            # 找出最便宜的地區
            min_price = float('inf')
            min_region = None
            
            for region_name, price_info in prices.items():
                if price_info:
                    if price_info.get('is_free'):
                        value = "🎁 免費遊戲"
                    else:
                        price = price_info['price']
                        if price > 0 and price < min_price:
                            min_price = price
                            min_region = region_name
                        
                        price_str = f"{price_info['currency']} {price:.2f}"
                        if price_info['discount'] > 0:
                            value = f"~~{price_info['currency']} {price_info['original_price']:.2f}~~\n{price_str} **(-{price_info['discount']}%)**"
                        else:
                            value = price_str
                    embed.add_field(name=region_name, value=value, inline=True)
            
            if min_region:
                embed.set_footer(text=f"💡 最便宜地區: {min_region}")
            
            # 顯示台灣歷史最低價
            historical_low = get_historical_low_price(app_id, 'tw')
            if historical_low:
                low_price, low_date = historical_low
                date_obj = datetime.fromisoformat(low_date)
                embed.add_field(
                    name="📊 台灣歷史最低價",
                    value=f"NT$ {low_price:.2f}\n記錄於 {date_obj.strftime('%Y-%m-%d')}",
                    inline=False
                )
            
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send("❌ 無法獲取價格資訊")
    except Exception as e:
        logger.error(f"查詢價格錯誤: {e}")
        await interaction.followup.send(f"❌ 查詢失敗: {str(e)}")

@bot.tree.command(name="特賣日曆", description="查看 Steam 年度特賣活動時間表")
async def sales_calendar(interaction: discord.Interaction):
    """顯示 Steam 特賣日曆"""
    try:
        embed = discord.Embed(
            title="📅 Steam 年度特賣活動日曆",
            description="以下是 Steam 每年的主要特賣活動時間",
            color=discord.Color.purple()
        )
        
        now = datetime.now()
        
        for sale_name, info in STEAM_SALES_CALENDAR.items():
            sale_date = datetime(now.year, info['month'], info['start_day'])
            
            if sale_date < now:
                sale_date = datetime(now.year + 1, info['month'], info['start_day'])
            
            days_until = (sale_date - now).days
            
            if days_until <= 7:
                status = f"🔥 **即將開始! ({days_until} 天後)**"
            elif days_until <= 30:
                status = f"⏰ {days_until} 天後"
            else:
                status = f"📆 {days_until} 天後"
            
            value = f"{info['emoji']} {sale_date.strftime('%Y年%m月%d日')}\n"
            value += f"期間: {info['duration']} 天\n"
            value += status
            
            embed.add_field(name=sale_name, value=value, inline=True)
        
        embed.add_field(
            name="💡 提示",
            value="Bot 會在特賣活動開始前 7 天自動提醒你!",
            inline=False
        )
        
        embed.set_footer(text="資料來源: Steam 官方特賣活動歷史")
        
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        logger.error(f"顯示特賣日曆錯誤: {e}")
        await interaction.response.send_message(f"❌ 操作失敗: {str(e)}")

@bot.tree.command(name="追蹤列表", description="顯示所有追蹤的遊戲")
async def list_tracked(interaction: discord.Interaction):
    """顯示所有追蹤的遊戲"""
    try:
        conn = sqlite3.connect('steam_prices.db')
        c = conn.cursor()
        c.execute('SELECT app_id, name, is_free FROM tracked_games ORDER BY name')
        games = c.fetchall()
        conn.close()
        
        if not games:
            await interaction.response.send_message("目前沒有追蹤任何遊戲")
            return
        
        embed = discord.Embed(
            title="📋 追蹤列表",
            description=f"目前追蹤 {len(games)} 款遊戲\n\n💡 Bot 會在這些遊戲達到**歷史新低**時通知你",
            color=discord.Color.blue()
        )
        
        for app_id, name, is_free in games[:25]:
            status = "🎁 免費" if is_free else "💰 付費"
            embed.add_field(
                name=f"{status} {name}",
                value=f"ID: {app_id}",
                inline=False
            )
        
        if len(games) > 25:
            embed.set_footer(text=f"僅顯示前 25 款,共 {len(games)} 款")
        
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        logger.error(f"顯示追蹤列表錯誤: {e}")
        await interaction.response.send_message(f"❌ 操作失敗: {str(e)}")

@bot.tree.command(name="移除追蹤", description="從追蹤列表移除遊戲")
@app_commands.describe(app_id="要移除的遊戲 App ID")
async def untrack_game(interaction: discord.Interaction, app_id: int):
    """從追蹤列表移除遊戲"""
    try:
        conn = sqlite3.connect('steam_prices.db')
        c = conn.cursor()
        c.execute('SELECT name FROM tracked_games WHERE app_id=?', (app_id,))
        result = c.fetchone()
        
        if result:
            game_name = result[0]
            c.execute('DELETE FROM tracked_games WHERE app_id=?', (app_id,))
            conn.commit()
            await interaction.response.send_message(f"✅ 已從追蹤列表移除 **{game_name}**")
        else:
            await interaction.response.send_message(f"❌ 找不到遊戲 ID: {app_id}")
        
        conn.close()
    except Exception as e:
        logger.error(f"移除追蹤錯誤: {e}")
        await interaction.response.send_message(f"❌ 操作失敗: {str(e)}")

@bot.tree.command(name="歷史低價", description="查詢遊戲的歷史最低價")
@app_commands.describe(app_id="Steam 遊戲的 App ID")
async def historical_low(interaction: discord.Interaction, app_id: int):
    """查詢遊戲的歷史最低價"""
    await interaction.response.defer()
    
    try:
        result = get_historical_low_price(app_id, 'tw')
        game_info = await get_steam_game_info(app_id, 'tw')
        
        if not game_info:
            await interaction.followup.send("❌ 無法獲取遊戲資訊")
            return
        
        name = game_info.get('name', 'Unknown Game')
        
        embed = discord.Embed(
            title=f"📊 歷史低價資訊",
            description=f"**{name}**",
            color=discord.Color.purple(),
            url=f"https://store.steampowered.com/app/{app_id}"
        )
        
        if 'header_image' in game_info:
            embed.set_thumbnail(url=game_info['header_image'])
        
        if result:
            lowest_price, date = result
            date_obj = datetime.fromisoformat(date)
            embed.add_field(name="📉 台灣歷史最低價", value=f"**NT$ {lowest_price:.2f}**", inline=True)
            embed.add_field(name="📅 記錄日期", value=date_obj.strftime('%Y-%m-%d'), inline=True)
        else:
            embed.add_field(name="提示", value="尚無歷史價格記錄", inline=False)
        
        # 獲取當前價格
        if 'price_overview' in game_info:
            price_data = game_info['price_overview']
            current_price = price_data['final'] / 100
            currency = price_data['currency']
            discount = price_data.get('discount_percent', 0)
            
            price_text = f"{currency} {current_price:.2f}"
            if discount > 0:
                price_text += f" (-{discount}%)"
            embed.add_field(name="💰 目前價格", value=price_text, inline=True)
            
            # 如果有歷史最低價,計算差距
            if result:
                if current_price == lowest_price:
                    embed.add_field(name="🔥 狀態", value="**目前就是歷史最低價!**", inline=False)
                elif current_price > lowest_price:
                    diff = current_price - lowest_price
                    diff_percent = (diff / lowest_price) * 100
                    embed.add_field(name="📈 與歷史低價差距", value=f"+{currency} {diff:.2f} (+{diff_percent:.1f}%)", inline=False)
        
        await interaction.followup.send(embed=embed)
    except Exception as e:
        logger.error(f"查詢歷史低價錯誤: {e}")
        await interaction.followup.send(f"❌ 查詢失敗: {str(e)}")

@bot.tree.command(name="help", description="顯示所有可用指令")
async def help_command(interaction: discord.Interaction):
    """顯示所有可用指令"""
    embed = discord.Embed(
        title="🤖 Steam 價格監控 Bot 使用指南",
        description="**本 Bot 專注於歷史新低價格通知!**",
        color=discord.Color.purple()
    )
    
    embed.add_field(name="🔧 設定指令", value="", inline=False)
    embed.add_field(name="/設定頻道 [頻道] [特賣通知]", value="設定接收通知的頻道 **(首次使用必須設定)**", inline=False)
    
    embed.add_field(name="📊 查詢指令", value="", inline=False)
    embed.add_field(name="/價格 <app_id>", value="查詢遊戲在 9 個國家的價格", inline=False)
    embed.add_field(name="/歷史低價 <app_id>", value="查詢遊戲的歷史最低價格", inline=False)
    embed.add_field(name="/特賣日曆", value="查看 Steam 年度特賣活動時間表", inline=False)
    
    embed.add_field(name="⚙️ 管理指令", value="", inline=False)
    embed.add_field(name="/追蹤 <app_id>", value="添加遊戲到監控列表", inline=False)
    embed.add_field(name="/追蹤列表", value="查看所有追蹤的遊戲", inline=False)
    embed.add_field(name="/移除追蹤 <app_id>", value="停止追蹤指定遊戲", inline=False)
    
    embed.add_field(
        name="🔔 通知說明",
        value="Bot 會在以下情況自動通知:\n"
              "• 🔥 遊戲達到**歷史新低**價格\n"
              "• 🎁 遊戲變成**免費**\n"
              "• 📅 Steam 特賣活動**提前 7 天**提醒",
        inline=False
    )
    
    embed.add_field(
        name="💡 使用提示",
        value="1. 先用 `/設定頻道` 設定通知頻道\n"
              "2. 用 `/追蹤` 添加要監控的遊戲\n"
              "3. Bot 會自動每小時檢查價格\n"
              "4. 只有達到**歷史新低**才會通知!",
        inline=False
    )
    
    embed.add_field(
        name="🔍 如何找到 App ID",
        value="從 Steam 商店頁面 URL 中取得\n"
              "例如: `steampowered.com/app/1091500/` 中的 `1091500`",
        inline=False
    )
    
    embed.set_footer(text="💡 專注於歷史新低,不再為普通折扣打擾你!")
    
    await interaction.response.send_message(embed=embed)

# ==================== 啟動 Bot ====================
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        logger.error("請設定 DISCORD_TOKEN 環境變數!")
        print("❌ 錯誤: 未找到 DISCORD_TOKEN")
        print("請在 .env 檔案中設定你的 Discord Bot Token")
    else:
        try:
            print("🚀 正在啟動 Bot...")
            bot.run(DISCORD_TOKEN)
        except Exception as e:
            logger.error(f"Bot 啟動失敗: {e}")
            print(f"❌ Bot 啟動失敗: {e}")