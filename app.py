import os
import json
import logging
import datetime
import requests
import sys
import locale
import oauthlib.oauth2.rfc6749.errors
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session
from flask_session import Session
from requests_oauthlib import OAuth2Session
import discord
from discord.ext import commands
import asyncio
import threading
from threading import Lock
from dotenv import load_dotenv
from nlp_processor import process_text, load_config, save_config

try:
    locale.setlocale(locale.LC_ALL, 'th_TH.UTF-8')
except:
    try:
        locale.setlocale(locale.LC_ALL, 'Thai_Thailand.utf8')
    except:
        pass

os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot_logs.log", encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

OAUTH2_CLIENT_ID = os.getenv('DISCORD_CLIENT_ID')
OAUTH2_CLIENT_SECRET = os.getenv('DISCORD_CLIENT_SECRET', '').strip()
OAUTH2_REDIRECT_URI = os.getenv('OAUTH2_REDIRECT_URI', 'http://localhost:5000/callback').strip()

AUTHORIZATION_BASE_URL = 'https://discord.com/api/oauth2/authorize'
TOKEN_URL = 'https://discord.com/api/oauth2/token'
API_BASE_URL = 'https://discord.com/api'

TOKEN = os.getenv("DISCORD_TOKEN")
intents = discord.Intents.default()
intents.guilds = True
intents.messages = True
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "default_secret_key_replace_this")
app.config['SESSION_TYPE'] = 'filesystem'
app.config['SESSION_FILE_DIR'] = './.flask_session/'
Session(app)

CONFIG_FILE = 'config.json'
DEFAULT_CONFIG = {
    "categories": {},
    "messages": {},
    "category_mapping": {},
    "products": [],
    "intents": {
        "unknown": {
            "responses": ["ขอโทษค่ะ ฉันไม่แน่ใจว่าเข้าใจถูกมั้ย ลองบอกใหม่ได้มั้ยคะ? 😊"]
        }
    }
}

config = DEFAULT_CONFIG.copy()

class LogHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.logs = []
        self.max_logs = 1000

    def emit(self, record):
        log_entry = {
            'time': datetime.datetime.fromtimestamp(record.created).strftime('%Y-%m-%d %H:%M:%S'),
            'level': record.levelname,
            'message': self.format(record)
        }
        self.logs.append(log_entry)
        if len(self.logs) > self.max_logs:
            self.logs = self.logs[-self.max_logs:]

class StdoutCapture:
    def __init__(self, logger):
        self.logger = logger
        self.original_stdout = sys.stdout
        
    def write(self, message):
        if message.strip():
            self.logger.info(f"[STDOUT] {message.strip()}")
        self.original_stdout.write(message)
        
    def flush(self):
        self.original_stdout.flush()

log_handler = LogHandler()
log_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
logger.addHandler(log_handler)
sys.stdout = StdoutCapture(logger)

GUILD_ID = None

def load_config():
    global config, GUILD_ID
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                loaded_config = json.load(f)
                config = DEFAULT_CONFIG.copy()
                for key in loaded_config:
                    config[key] = loaded_config[key]
                logger.info("Loaded config.json successfully")
        else:
            logger.warning("config.json not found, using default config")
            config = DEFAULT_CONFIG.copy()
        if GUILD_ID is None:
            GUILD_ID = int(os.getenv("GUILD_ID", "0"))
    except Exception as e:
        logger.error(f"Error loading config: {str(e)}")
        config = DEFAULT_CONFIG.copy()

@bot.event
async def on_ready():
    logger.info(f"บอทเริ่มทำงานแล้ว: {bot.user.name}#{bot.user.discriminator}")
    try:
        await register_commands()
        await update_categories_info()
    except Exception as e:
        logger.error(f"เกิดข้อผิดพลาดเมื่อบอทพร้อมทำงาน: {str(e)}")

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    
    if message.content.startswith(bot.command_prefix):
        await bot.process_commands(message)
        return
    
    user_id = str(message.author.id)
    result = process_text(message.content, user_id)
    await message.channel.send(result["response"])

@bot.event
async def on_guild_channel_create(channel):
    logger.info(f"มีการสร้างช่องใหม่: {channel.name} (ID: {channel.id})")
    await process_new_channel(channel)

async def process_new_channel(channel):
    if not isinstance(channel, discord.TextChannel):
        return
    channel_name_lower = channel.name.lower()
    for category_name, category_data in config["category_mapping"].items():
        if "keywords" in category_data and any(keyword.lower() in channel_name_lower for keyword in category_data["keywords"]):
            category_id = category_data["category_id_1"]
            if category_id:
                try:
                    category = bot.get_channel(int(category_id))
                    if category and isinstance(category, discord.CategoryChannel):
                        await channel.edit(category=category)
                        logger.info(f"ย้ายช่อง {channel.name} ไปยัง Category {category.name}")
                        return
                except Exception as e:
                    logger.error(f"ไม่สามารถย้ายช่อง {channel.name} ได้: {str(e)}")
                    backup_category_id = category_data.get("category_id_2")
                    if backup_category_id:
                        try:
                            backup_category = bot.get_channel(int(backup_category_id))
                            if backup_category and isinstance(category, discord.CategoryChannel):
                                await channel.edit(category=backup_category)
                                logger.info(f"ย้ายช่อง {channel.name} ไปยัง Category สำรอง {backup_category.name}")
                                return
                        except Exception as e2:
                            logger.error(f"ไม่สามารถย้ายช่องไปยัง Category สำรองได้: {str(e2)}")

config_update_lock = Lock()

async def update_categories_info():
    global GUILD_ID
    with config_update_lock:
        try:
            await bot.wait_until_ready()
            guild = bot.get_guild(GUILD_ID)
            if not guild:
                logger.error(f"ไม่พบเซิร์ฟเวอร์ที่มี ID: {GUILD_ID}")
                if len(bot.guilds) > 0:
                    guild = bot.guilds[0]
                    GUILD_ID = guild.id
                    logger.info(f"ใช้เซิร์ฟเวอร์แรกที่พบแทน: {guild.name} (ID: {guild.id})")
                else:
                    logger.error("ไม่พบเซิร์ฟเวอร์ที่บอทเข้าถึงได้")
                    return
            categories = {str(category.id): category.name for category in guild.categories}
            config["categories"] = categories
            save_config(config)
            logger.info("อัปเดตข้อมูลหมวดหมู่และบันทึกลง config.json สำเร็จ")
        except Exception as e:
            logger.error(f"เกิดข้อผิดพลาดในการอัปเดตข้อมูลหมวดหมู่: {str(e)}")

command_register_lock = Lock()

async def register_commands():
    with command_register_lock:
        try:
            for command_name in list(config.get('messages', {}).keys()):
                if command_name.startswith('!'):
                    command_name = command_name[1:]
                if command_name in bot.all_commands:
                    bot.remove_command(command_name)
            
            for command_name, command_data in config.get('messages', {}).items():
                if command_name.startswith('!'):
                    command_name = command_name[1:]
                response_text = command_data.get("text", "")
                change_channel = command_data.get("change_channel", False)
                allow_additional_text = command_data.get("allow_additional_text", False)
                command_func = create_command_function(command_name, response_text, change_channel, allow_additional_text)
                bot.command(name=command_name)(command_func)
                logger.info(f"ลงทะเบียนคำสั่ง !{command_name} สำเร็จ")
        except Exception as e:
            logger.error(f"เกิดข้อผิดพลาดในการลงทะเบียนคำสั่ง: {str(e)}")

def create_command_function(cmd_name, response, should_change, allow_additional_text):
    async def command_func(ctx, additional_text: str = None):
        try:
            await ctx.send(response)
            if should_change:
                channel = ctx.channel
                current_name = channel.name
                new_name = current_name
                suffixes = [key for key in config["messages"].keys()]
                parts = new_name.split("-")
                if len(parts) > 1 and parts[-1] in suffixes:
                    new_name = "-".join(parts[:-1])
                elif len(parts) > 2 and parts[-2] in suffixes:
                    new_name = "-".join(parts[:-2])
                if allow_additional_text and additional_text:
                    new_name = f"{new_name}-{additional_text}-{cmd_name}"
                else:
                    new_name = f"{new_name}-{cmd_name}"
                if new_name != current_name:
                    try:
                        await channel.edit(name=new_name)
                        logger.info(f"เปลี่ยนชื่อช่องจาก '{current_name}' เป็น '{new_name}'")
                    except Exception as e:
                        logger.error(f"ไม่สามารถเปลี่ยนชื่อช่องได้: {str(e)}")
                        await ctx.send(f"❌ ไม่สามารถเปลี่ยนชื่อช่องได้: {str(e)}")
        except Exception as e:
            logger.error(f"เกิดข้อผิดพลาดในคำสั่ง !{cmd_name}: {str(e)}")
            await ctx.send(f"❌ เกิดข้อผิดพลาด: {str(e)}")
    command_func.__name__ = f"command_{cmd_name}"
    return command_func

@bot.command()
async def scan_channels(ctx):
    try:
        guild = ctx.guild
        moved_channels = 0
        for channel in guild.text_channels:
            channel_name_lower = channel.name.lower()
            for category_name, category_data in config["category_mapping"].items():
                if "keywords" in category_data and any(keyword.lower() in channel_name_lower for keyword in category_data["keywords"]):
                    category_id = category_data["category_id_1"]
                    if category_id:
                        category = bot.get_channel(int(category_id))
                        if category and isinstance(category, discord.CategoryChannel):
                            await channel.edit(category=category)
                            moved_channels += 1
                            logger.info(f"✅ ย้ายช่อง {channel.name} ไปยัง Category {category.name}")
                            break
        await ctx.send(f"✅ **ย้ายช่องทั้งหมด {moved_channels} ช่องเรียบร้อยแล้ว!**" if moved_channels > 0 else "⚠️ **ไม่พบช่องที่ต้องย้าย!**")
    except Exception as e:
        logger.error(f"เกิดข้อผิดพลาดในคำสั่ง !scan_channels: {str(e)}")
        await ctx.send(f"❌ เกิดข้อผิดพลาด: {str(e)}")

@bot.command()
async def teach(ctx, intent: str, *, response: str):
    try:
        config = load_config()
        if intent not in config["intents"]:
            config["intents"][intent] = {"keywords": [], "responses": []}
        if response not in config["intents"][intent]["responses"]:
            config["intents"][intent]["responses"].append(response)
            save_config(config)
            logger.info(f"Added response '{response}' to intent '{intent}' via !teach command")
            await ctx.send(f"✅ สอนบอทสำเร็จ! เพิ่มคำตอบ '{response}' สำหรับเจตนา '{intent}'")
        else:
            await ctx.send(f"⚠️ คำตอบ '{response}' มีอยู่แล้วในเจตนา '{intent}'")
    except Exception as e:
        logger.error(f"เกิดข้อผิดพลาดในคำสั่ง !teach: {str(e)}")
        await ctx.send(f"❌ เกิดข้อผิดพลาด: {str(e)}")

def token_updater(token):
    session['oauth2_token'] = token

def make_session(token=None, state=None, scope=None):
    return OAuth2Session(
        client_id=OAUTH2_CLIENT_ID,
        token=token,
        state=state,
        scope=scope,
        redirect_uri=OAUTH2_REDIRECT_URI,
        auto_refresh_kwargs={
            'client_id': OAUTH2_CLIENT_ID,
            'client_secret': OAUTH2_CLIENT_SECRET
        },
        auto_refresh_url=TOKEN_URL,
        token_updater=token_updater
    )

@app.route('/login')
def login():
    scope = ['identify', 'guilds']
    discord = make_session(scope=scope)
    authorization_url, state = discord.authorization_url(AUTHORIZATION_BASE_URL)
    session['oauth2_state'] = state
    return redirect(authorization_url)

@app.route('/callback')
def callback():
    if request.values.get('error'):
        flash(f"ข้อผิดพลาดจาก Discord: {request.values.get('error')}", "danger")
        return redirect(url_for('index'))
    
    if 'oauth2_state' not in session:
        flash("เซสชันหมดอายุหรือไม่ถูกต้อง กรุณาลองใหม่อีกครั้ง", "warning")
        return redirect(url_for('index'))
    
    try:
        discord = make_session(state=session['oauth2_state'])
        token = discord.fetch_token(
            TOKEN_URL,
            client_secret=OAUTH2_CLIENT_SECRET,
            authorization_response=request.url
        )
        session['oauth2_token'] = token
        logger.info("ผู้ใช้เข้าสู่ระบบสำเร็จ")
        return redirect(url_for('servers'))
    except Exception as e:
        logger.error(f"เกิดข้อผิดพลาดในขั้นตอนการตรวจสอบสิทธิ์: {str(e)}")
        flash("เกิดข้อผิดพลาดในการตรวจสอบสิทธิ์ กรุณาลองอีกครั้ง", "danger")
        return redirect(url_for('index'))

@app.route('/logout')
def logout():
    session.pop('oauth2_token', None)
    return redirect(url_for('index'))

@app.route('/')
def index():
    if 'oauth2_token' in session:
        return redirect(url_for('servers'))
    return render_template('login.html')

@app.route('/servers')
def servers():
    if 'oauth2_token' not in session:
        return redirect(url_for('login'))
    
    try:
        discord = make_session(token=session['oauth2_token'])
        try:
            user_guilds = discord.get(f'{API_BASE_URL}/users/@me/guilds').json()
            user_info = discord.get(f'{API_BASE_URL}/users/@me').json()
        except oauthlib.oauth2.rfc6749.errors.TokenExpiredError:
            logger.warning("Discord token หมดอายุและไม่สามารถรีเฟรชได้")
            session.pop('oauth2_token', None)
            flash("เซสชันหมดอายุ กรุณาเข้าสู่ระบบใหม่อีกครั้ง", "warning")
            return redirect(url_for('login'))
        except (oauthlib.oauth2.rfc6749.errors.InvalidGrantError, 
                oauthlib.oauth2.rfc6749.errors.OAuth2Error) as e:
            logger.error(f"OAuth2 error: {str(e)}")
            session.pop('oauth2_token', None)
            flash("เกิดข้อผิดพลาดในการตรวจสอบสิทธิ์ กรุณาเข้าสู่ระบบใหม่", "danger")
            return redirect(url_for('login'))
        
        bot_guilds = [guild.id for guild in bot.guilds]
        accessible_guilds = []
        for guild in user_guilds:
            permissions = int(guild['permissions'])
            has_admin = (permissions & 0x8) == 0x8
            has_manage = (permissions & 0x20) == 0x20
            guild_in_bot = int(guild['id']) in bot_guilds
            guild['accessible'] = guild_in_bot and (has_admin or has_manage)
            accessible_guilds.append(guild)
        return render_template('servers.html', guilds=accessible_guilds, user=user_info)
    
    except Exception as e:
        logger.error(f"เกิดข้อผิดพลาดในหน้า servers: {str(e)}")
        flash(f"เกิดข้อผิดพลาด: {str(e)}", "danger")
        return redirect(url_for('index'))

@app.route('/dashboard/<guild_id>')
def dashboard(guild_id):
    if 'oauth2_token' not in session:
        return redirect(url_for('login'))
    guild = bot.get_guild(int(guild_id))
    if not guild:
        flash("บอทไม่ได้อยู่ในเซิร์ฟเวอร์นี้", "danger")
        return redirect(url_for('servers'))
    discord = make_session(token=session['oauth2_token'])
    user_guilds = discord.get(f'{API_BASE_URL}/users/@me/guilds').json()
    user_in_guild = any(user_guild['id'] == guild_id and (int(user_guild['permissions']) & 0x28) for user_guild in user_guilds)
    if not user_in_guild:
        flash("คุณไม่มีสิทธิ์เข้าถึงเซิร์ฟเวอร์นี้", "danger")
        return redirect(url_for('servers'))
    guild_info = {
        'id': guild.id,
        'name': guild.name,
        'icon': guild.icon.url if guild.icon else None,
        'text_channels': len(guild.text_channels),
        'voice_channels': len(guild.voice_channels),
        'categories': len(guild.categories),
        'members': guild.member_count
    }
    return render_template('dashboard.html', guild=guild_info)

@app.route('/logs')
def logs():
    return render_template('logs.html', logs=log_handler.logs)

@app.route('/channels')
def channels():
    return render_template('channels.html')

@app.route('/chat')
def chat():
    if 'oauth2_token' not in session:
        return redirect(url_for('login'))
    return render_template('chat.html')

@app.route('/settings', methods=['GET', 'POST'])
def settings():
    if request.method == 'POST':
        try:
            if 'category_mapping' in request.form:
                new_mapping = json.loads(request.form['category_mapping'])
                config['category_mapping'] = new_mapping
            if 'messages' in request.form:
                new_messages = json.loads(request.form['messages'])
                for cmd in new_messages:
                    if "allow_additional_text" not in new_messages[cmd]:
                        new_messages[cmd]["allow_additional_text"] = False
                config['messages'] = new_messages
                asyncio.run_coroutine_threadsafe(register_commands(), bot.loop)
            save_config(config)
            flash("บันทึกการตั้งค่าเรียบร้อยแล้ว", "success")
            asyncio.run_coroutine_threadsafe(update_categories_info(), bot.loop)
            return redirect(url_for('settings'))
        except Exception as e:
            logger.error(f"เกิดข้อผิดพลาดในการบันทึกการตั้งค่า: {str(e)}")
            flash(f"เกิดข้อผิดพลาดในการบันทึกการตั้งค่า: {str(e)}", "danger")
            return redirect(url_for('settings'))
    return render_template('settings.html', config=config, now=datetime.datetime.now())

@app.route('/settings/categories', methods=['GET', 'POST'])
def category_settings():
    if request.method == 'POST':
        try:
            if 'category_mapping' in request.form:
                new_mapping = json.loads(request.form['category_mapping'])
                config['category_mapping'] = new_mapping
                save_config(config)
                flash("บันทึกการตั้งค่าหมวดหมู่เรียบร้อยแล้ว", "success")
                asyncio.run_coroutine_threadsafe(update_categories_info(), bot.loop)
                return redirect(url_for('category_settings'))
        except Exception as e:
            logger.error(f"เกิดข้อผิดพลาดในการบันทึกการตั้งค่าหมวดหมู่: {str(e)}")
            flash(f"เกิดข้อผิดพลาดในการบันทึกการตั้งค่าหมวดหมู่: {str(e)}", "danger")
            return redirect(url_for('category_settings'))
    return render_template('category_settings.html', config=config, now=datetime.datetime.now())

@app.route('/settings/commands', methods=['GET', 'POST'])
def command_settings():
    if request.method == 'POST':
        try:
            if 'messages' in request.form:
                new_messages = json.loads(request.form['messages'])
                for cmd in new_messages:
                    if "allow_additional_text" not in new_messages[cmd]:
                        new_messages[cmd]["allow_additional_text"] = False
                config['messages'] = new_messages
                save_config(config)
                flash("บันทึกการตั้งค่าคำสั่งเรียบร้อยแล้ว", "success")
                asyncio.run_coroutine_threadsafe(register_commands(), bot.loop)
                return redirect(url_for('command_settings'))
        except Exception as e:
            logger.error(f"เกิดข้อผิดพลาดในการบันทึกการตั้งค่าคำสั่ง: {str(e)}")
            flash(f"เกิดข้อผิดพลาดในการบันทึกการตั้งค่าคำสั่ง: {str(e)}", "danger")
            return redirect(url_for('command_settings'))
    return render_template('command_settings.html', config=config, now=datetime.datetime.now())

@app.route('/settings/intents', methods=['GET', 'POST'])
def intent_settings():
    if request.method == 'POST':
        try:
            if 'intents' in request.form:
                new_intents = json.loads(request.form['intents'])
                logger.info(f"Received intents: {new_intents}")
                config['intents'] = new_intents
                save_config(config)
                flash("บันทึกการตั้งค่าเจตนาเรียบร้อยแล้ว", "success")
            else:
                logger.warning("No intents found in form data")
                flash("ไม่พบข้อมูลเจตนาในฟอร์ม", "warning")
            return redirect(url_for('intent_settings'))
        except Exception as e:
            logger.error(f"Error saving intent settings: {str(e)}")
            flash(f"เกิดข้อผิดพลาดในการบันทึกการตั้งค่า: {str(e)}", "danger")
            return redirect(url_for('intent_settings'))
    return render_template('intent_settings.html', config=config, now=datetime.datetime.now())

@app.route('/api/logs')
def api_logs():
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 50))
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    logs_page = log_handler.logs[start_idx:end_idx]
    return jsonify({'logs': logs_page, 'total': len(log_handler.logs), 'page': page, 'per_page': per_page})

@app.route('/api/categories')
def get_categories():
    return jsonify(config["categories"])

@app.route('/api/categories/refresh', methods=['POST'])
def refresh_categories():
    try:
        asyncio.run_coroutine_threadsafe(update_categories_info(), bot.loop)
        return jsonify({"success": True, "message": "เริ่มรีเฟรชข้อมูลหมวดหมู่แล้ว"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})

@app.route('/api/category_mapping', methods=['GET'])
def get_category_mapping():
    return jsonify(config["category_mapping"])

@app.route('/api/category_mapping', methods=['POST'])
def update_category_mapping():
    try:
        data = request.json
        if not data or "name" not in data:
            return jsonify({"success": False, "message": "ข้อมูลไม่ถูกต้อง"})
        category_name = data["name"]
        if category_name not in config["category_mapping"]:
            config["category_mapping"][category_name] = {"category_id_1": "", "category_id_2": "", "keywords": []}
        config["category_mapping"][category_name].update({k: data[k] for k in data if k in ["category_id_1", "category_id_2", "keywords"]})
        save_config(config)
        return jsonify({"success": True, "message": "อัปเดตหมวดหมู่สำเร็จ"})
    except Exception as e:
        logger.error(f"เกิดข้อผิดพลาดในการอัปเดตหมวดหมู่: {str(e)}")
        return jsonify({"success": False, "message": str(e)})

@app.route('/api/category_mapping/<category_name>', methods=['DELETE'])
def delete_category_mapping(category_name):
    try:
        if category_name in config["category_mapping"]:
            del config["category_mapping"][category_name]
            save_config(config)
            return jsonify({"success": True, "message": f"ลบหมวดหมู่ {category_name} สำเร็จ"})
        return jsonify({"success": False, "message": "ไม่พบหมวดหมู่นี้"})
    except Exception as e:
        logger.error(f"เกิดข้อผิดพลาดในการลบหมวดหมู่: {str(e)}")
        return jsonify({"success": False, "message": str(e)})

@app.route('/api/messages', methods=['GET'])
def get_messages():
    return jsonify(config["messages"])

@app.route('/api/messages', methods=['POST'])
def update_messages():
    try:
        data = request.json
        if not data or "name" not in data:
            return jsonify({"success": False, "message": "ข้อมูลไม่ถูกต้อง"})
        command_name = data["name"].lstrip('!')
        config["messages"][command_name] = {
            "text": data.get("text", ""),
            "change_channel": data.get("change_channel", False),
            "allow_additional_text": data.get("allow_additional_text", False)
        }
        save_config(config)
        asyncio.run_coroutine_threadsafe(register_commands(), bot.loop)
        return jsonify({"success": True, "message": f"อัปเดตคำสั่ง !{command_name} สำเร็จ"})
    except Exception as e:
        logger.error(f"เกิดข้อผิดพลาดในการอัปเดตคำสั่ง: {str(e)}")
        return jsonify({"success": False, "message": str(e)})

@app.route('/api/messages/<command_name>', methods=['DELETE'])
def delete_message(command_name):
    try:
        command_name = command_name.lstrip('!')
        if command_name in config["messages"]:
            del config["messages"][command_name]
            save_config(config)
            asyncio.run_coroutine_threadsafe(register_commands(), bot.loop)
            return jsonify({"success": True, "message": f"ลบคำสั่ง !{command_name} สำเร็จ"})
        return jsonify({"success": False, "message": "ไม่พบคำสั่งนี้"})
    except Exception as e:
        logger.error(f"เกิดข้อผิดพลาดในการลบคำสั่ง: {str(e)}")
        return jsonify({"success": False, "message": str(e)})

@app.route('/api/intents', methods=['GET'])
def get_intents():
    return jsonify(config["intents"])

@app.route('/api/intents', methods=['POST'])
def update_intents():
    try:
        data = request.json
        if not data or "name" not in data:
            return jsonify({"success": False, "message": "ข้อมูลไม่ถูกต้อง"})
        intent_name = data["name"]
        if intent_name not in config["intents"]:
            config["intents"][intent_name] = {"keywords": [], "responses": []}
        config["intents"][intent_name].update({
            "keywords": data.get("keywords", []),
            "responses": data.get("responses", [])
        })
        save_config(config)
        return jsonify({"success": True, "message": f"อัปเดตเจตนา {intent_name} สำเร็จ"})
    except Exception as e:
        logger.error(f"Error updating intents: {str(e)}")
        return jsonify({"success": False, "message": str(e)})

@app.route('/api/intents/<intent_name>', methods=['DELETE'])
def delete_intent(intent_name):
    try:
        if intent_name in config["intents"]:
            del config["intents"][intent_name]
            save_config(config)
            return jsonify({"success": True, "message": f"ลบเจตนา {intent_name} สำเร็จ"})
        return jsonify({"success": False, "message": "ไม่พบเจตนานี้"})
    except Exception as e:
        logger.error(f"Error deleting intent: {str(e)}")
        return jsonify({"success": False, "message": str(e)})

@app.route('/api/channels/count')
def get_channels_count():
    try:
        guild = bot.get_guild(GUILD_ID)
        if not guild:
            return jsonify({'success': False, 'message': 'ไม่พบเซิร์ฟเวอร์', 'count': 0})
        text_channels_count = len(guild.text_channels)
        voice_channels_count = len(guild.voice_channels)
        categories_count = len(guild.categories)
        channels_by_category = {category.name: len(category.channels) for category in guild.categories}
        return jsonify({
            'success': True,
            'text_channels_count': text_channels_count,
            'voice_channels_count': voice_channels_count,
            'categories_count': categories_count,
            'total_channels': text_channels_count + voice_channels_count,
            'channels_by_category': channels_by_category
        })
    except Exception as e:
        logger.error(f"เกิดข้อผิดพลาดในการนับจำนวนช่อง: {str(e)}")
        return jsonify({'success': False, 'message': str(e), 'count': 0})

@app.route('/api/chat', methods=['POST'])
def chat_with_bot():
    try:
        data = request.get_json()
        if not data or 'message' not in data:
            return jsonify({'success': False, 'message': 'กรุณาส่งข้อความ'}), 400
        
        message = data['message']
        user_id = session.get('oauth2_token', {}).get('user_id', 'web_user')
        
        result = process_text(message, user_id)
        
        config = load_config()
        product_name = None
        if result.get("product_id"):
            product = next((p for p in config["products"] if p["id"] == result.get("product_id")), None)
            product_name = product["name"] if product else None
        
        return jsonify({
            'success': True,
            'reply': result["response"],
            'tokens': result["tokens"],
            'intent': result.get("intent"),
            'product_name': product_name
        })
    except Exception as e:
        logger.error(f"เกิดข้อผิดพลาดในการสนทนากับบอท: {str(e)}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/server-status')
def server_status():
    return jsonify({'online': True})

def run_bot():
    try:
        print(f"Starting bot with TOKEN: {TOKEN[:10]}...")
        print(f"Using GUILD_ID: {GUILD_ID if GUILD_ID is not None else 'Not set yet'}")
        asyncio.run(bot.start(TOKEN))
    except Exception as e:
        logger.error(f"เกิดข้อผิดพลาดในการเริ่มบอท: {str(e)}")

if __name__ == '__main__':
    load_config()
    loop = asyncio.get_event_loop()
    
    def run_flask():
        app.run(host='127.0.0.1', port=5000, debug=True, use_reloader=False)
    
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    
    try:
        print(f"Starting bot with TOKEN: {TOKEN[:10]}...")
        print(f"Using GUILD_ID: {GUILD_ID if GUILD_ID is not None else 'Not set yet'}")
        loop.run_until_complete(bot.start(TOKEN))
    except Exception as e:
        logger.error(f"เกิดข้อผิดพลาดในการเริ่มบอท: {str(e)}")
    finally:
        loop.close()