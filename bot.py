import asyncio
import aiohttp
import re
import os
import tempfile
import time
import logging
from aiogram import Bot, Dispatcher, Router, types
from aiogram.types import Message, FSInputFile, BotCommand
from aiogram.filters import Command
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramBadRequest
from motor.motor_asyncio import AsyncIOMotorClient
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('teradownloader.log')
    ]
)
logger = logging.getLogger(__name__)

# TeraBox-specific URL regex
LINK_REGEX = re.compile(
    r"https?://[^\s]*?(?:terabox|teraboxapp|teraboxshare|nephobox|1024tera|1024terabox|freeterabox|terasharefile|terasharelink|mirrobox|momerybox|teraboxlink|teraboxurl)\.[^\s]+",
    re.IGNORECASE
)

# Configuration
BOT_TOKEN = "8008678561:AAH80tlSuc-tqEYb12eXMfUGfeo7Wz8qUEU"
API_ENDPOINT = "https://terabox.itxarshman.workers.dev/api"
SELF_HOSTED_API = "http://tgapi.arshman.space:8088"

# MongoDB setup
MONGO_URI = "mongodb+srv://irexanon:xUf7PCf9cvMHy8g6@rexdb.d9rwo.mongodb.net/?retryWrites=true&w=majority&appName=RexDB"
mongo = AsyncIOMotorClient(MONGO_URI)
db = mongo["teradownloader"]
config_col = db["config"]
broadcast_col = db["broadcasted"]
admins_col = db["admins"]

# Default global config
DEFAULT_CONFIG = {
    "_id": "global",
    "admin_broadcast_enabled": False,  # Admin links broadcast
    "channel_broadcast_enabled": False,  # Channel links broadcast
    "broadcast_chats": [-1002780909369],
    "admin_password": "11223344"
}

session = AiohttpSession(api=TelegramAPIServer.from_base(SELF_HOSTED_API))
bot = Bot(token=BOT_TOKEN, session=session)
dp = Dispatcher()
router = Router(name="terabox_listener")
sem = asyncio.Semaphore(50)

async def get_config():
    config = await config_col.find_one({"_id": "global"})
    if not config:
        await config_col.insert_one(DEFAULT_CONFIG)
        return DEFAULT_CONFIG
    return config

async def update_config(update: dict):
    await config_col.update_one({"_id": "global"}, {"$set": update})

async def is_admin(user_id: int) -> bool:
    """Check if user is admin"""
    admin = await admins_col.find_one({"user_id": user_id})
    return admin is not None

async def add_admin(user_id: int, username: str = None, full_name: str = None):
    """Add user as admin"""
    await admins_col.update_one(
        {"user_id": user_id},
        {"$set": {
            "user_id": user_id,
            "username": username,
            "full_name": full_name,
            "added_at": time.time()
        }},
        upsert=True
    )

async def set_bot_commands(user_id: int = None):
    """Set bot commands based on user role"""
    if user_id and await is_admin(user_id):
        # Admin commands
        commands = [
            BotCommand(command="start", description="Start the bot"),
            BotCommand(command="settings", description="Bot Settings (Admin Only)")
        ]
        await bot.set_my_commands(commands, scope=types.BotCommandScopeChat(chat_id=user_id))
    else:
        # Regular user commands (only /start)
        commands = [
            BotCommand(command="start", description="Start the bot")
        ]
        if user_id:
            await bot.set_my_commands(commands, scope=types.BotCommandScopeChat(chat_id=user_id))
        else:
            await bot.set_my_commands(commands)

async def get_links(source_url: str):
    logger.info(f"Requesting links for URL: {source_url}")
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(f"{API_ENDPOINT}?url={source_url}") as resp:
                if resp.status == 200:
                    logger.info(f"Successfully retrieved links for {source_url}")
                    return await resp.json()
                logger.error(f"API request failed for {source_url}, status: {resp.status}")
        except Exception as e:
            logger.error(f"Error fetching links for {source_url}: {str(e)}")
    return None

async def download_file(dl_url: str, filename: str, size_mb: float, attempt: int = 0):
    path = tempfile.NamedTemporaryFile(delete=False).name
    downloaded = 0
    logger.info(f"Starting download of {filename} from {dl_url} (attempt {attempt + 1})")
    try:
        async with sem:
            async with aiohttp.ClientSession() as session:
                async with session.get(dl_url) as resp:
                    if resp.status != 200:
                        logger.error(f"Download failed for {filename}, status: {resp.status}")
                        return False, None
                    content_length = int(resp.headers.get('Content-Length', 0))
                    total_mb = content_length / (1024 * 1024) if content_length else size_mb
                    async for chunk in resp.content.iter_chunked(5 * 1024 * 1024):
                        with open(path, 'ab') as f:
                            f.write(chunk)
                        downloaded += len(chunk)
                        if downloaded % (50 * 1024 * 1024) == 0:
                            percent = (downloaded / (total_mb * 1024 * 1024)) * 100 if total_mb else 0
                            logger.info(f"Downloading {filename}: {downloaded // (1024 * 1024)}/{int(total_mb)} MB ({percent:.0f}%)")
                    logger.info(f"Download completed for {filename}")
    except Exception as e:
        logger.error(f"Download error for {filename}: {str(e)}")
        if os.path.exists(path):
            os.unlink(path)
        if attempt < 2:
            backoff = 2 ** attempt
            logger.info(f"Retrying download for {filename} after {backoff}s")
            await asyncio.sleep(backoff)
            return await download_file(dl_url, filename, size_mb, attempt + 1)
        return False, None
    return True, path

async def broadcast_video(file_path: str, video_name: str, broadcast_type: str):
    """
    Broadcast video to configured chats
    broadcast_type: 'admin' or 'channel'
    """
    config = await get_config()
    
    # Check if broadcasting is enabled for this type
    if broadcast_type == 'admin' and not config["admin_broadcast_enabled"]:
        logger.info(f"Admin broadcast disabled - skipping {video_name}")
        return False
    
    if broadcast_type == 'channel' and not config["channel_broadcast_enabled"]:
        logger.info(f"Channel broadcast disabled - skipping {video_name}")
        return False

    # Prevent duplicates
    if await broadcast_col.find_one({"name": video_name}):
        logger.info(f"Duplicate broadcast skipped: {video_name}")
        return False

    chats = config.get("broadcast_chats", [])
    if not chats:
        logger.warning("No broadcast chats configured")
        return False

    broadcast_count = 0
    for bc_chat_id in chats:
        try:
            input_file = FSInputFile(file_path, filename=video_name)
            await bot.send_video(chat_id=bc_chat_id, video=input_file, supports_streaming=True)
            await broadcast_col.insert_one({"name": video_name, "chat_id": bc_chat_id, "timestamp": time.time()})
            broadcast_count += 1
            logger.info(f"📤 Broadcasted {video_name} to chat {bc_chat_id}")
        except Exception as e:
            logger.error(f"❌ Broadcast failed for chat {bc_chat_id}: {str(e)[:100]}")

    if broadcast_count > 0:
        logger.info(f"✅ Broadcast complete: {broadcast_count}/{len(chats)} chats")
        return True
    return False

async def send_video_to_user(file_path: str, video_name: str, chat_id: int):
    """Send video directly to user"""
    try:
        input_file = FSInputFile(file_path, filename=video_name)
        await bot.send_video(chat_id=chat_id, video=input_file, supports_streaming=True, caption=f"✅ {video_name}")
        logger.info(f"📤 Sent {video_name} to chat {chat_id}")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to send to chat {chat_id}: {str(e)[:100]}")
        return False

async def process_file(link: dict, source_url: str, original_chat_id: int = None, source_type: str = "user"):
    """
    Process and download file
    source_type: 'user' (regular user), 'admin' (admin user), 'channel' (channel post)
    """
    name = link.get("name", "unknown")
    size_mb = link.get("size_mb", 0)
    size_gb = size_mb / 1024
    logger.info(f"Processing file: {name}, size: {size_mb} MB, source: {source_type}")
    
    if size_gb > 2:
        logger.warning(f"File {name} size {size_gb:.2f} GB exceeds 2 GB limit")
        if original_chat_id and source_type != "channel":
            await bot.send_message(original_chat_id, f"❌ File {name} is too large ({size_gb:.2f} GB). Max 2 GB.")
        return

    # Only process videos
    if not name.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.webm')):
        logger.info(f"Skipping non-video file: {name}")
        if original_chat_id and source_type != "channel":
            await bot.send_message(original_chat_id, f"ℹ️ Skipped non-video file: {name}")
        return

    file_path = None
    new_link = None
    async with sem:
        try:
            for attempt in range(4):
                if attempt == 0:
                    dl_url = link["original_url"]
                    label = "proxied primary"
                elif attempt == 1:
                    dl_url = link["direct_url"]
                    label = "direct fallback"
                elif attempt == 2:
                    logger.info(f"Refreshing links for {name}")
                    new_resp = await get_links(source_url)
                    if not new_resp or "links" not in new_resp:
                        logger.error(f"Failed to refresh links for {name}")
                        break
                    new_link = next((l for l in new_resp["links"] if l.get("name") == name), None)
                    if not new_link:
                        logger.error(f"File {name} not found in refreshed links")
                        break
                    dl_url = new_link["original_url"]
                    label = "new proxied"
                elif attempt == 3:
                    if not new_link:
                        break
                    dl_url = new_link["direct_url"]
                    label = "new direct"
                else:
                    break
                
                logger.info(f"Attempting {label} download for {name}")
                success, file_path = await download_file(dl_url, name, size_mb)
                if success:
                    break
                logger.warning(f"{label.capitalize()} failed for {name}, retrying...")

            if not file_path:
                logger.error(f"File {name} failed to download after all retries")
                if original_chat_id and source_type != "channel":
                    await bot.send_message(original_chat_id, f"❌ Failed to download {name}")
                return

            logger.info(f"Successfully downloaded {name}")

            # Handle based on source type
            if source_type == "user":
                # Regular user - just send video back
                await send_video_to_user(file_path, name, original_chat_id)
            
            elif source_type == "admin":
                # Admin - send back to admin AND broadcast if enabled
                await send_video_to_user(file_path, name, original_chat_id)
                await broadcast_video(file_path, name, 'admin')
            
            elif source_type == "channel":
                # Channel - only broadcast if enabled
                await broadcast_video(file_path, name, 'channel')

        except Exception as e:
            logger.error(f"Error processing {name}: {str(e)}")
        finally:
            if file_path and os.path.exists(file_path):
                logger.debug(f"Cleaning up temporary file: {file_path}")
                os.unlink(file_path)

async def process_url(source_url: str, chat_id: int, source_type: str = "user"):
    """
    Process TeraBox URL
    source_type: 'user', 'admin', or 'channel'
    """
    logger.info(f"Processing URL: {source_url} from {source_type} {chat_id}")
    response = await get_links(source_url)
    
    if not response or "links" not in response:
        logger.error(f"Failed to retrieve links for {source_url}")
        if source_type != "channel":
            await bot.send_message(chat_id, f"❌ Failed to retrieve links for {source_url}")
        return

    logger.info(f"Found {len(response['links'])} files for {source_url}")
    if source_type != "channel":
        await bot.send_message(chat_id, f"📥 Found {len(response['links'])} file(s). Downloading...")
    
    for link in response["links"]:
        asyncio.create_task(process_file(link, source_url, chat_id, source_type))

@router.message(Command("start"))
async def start(message: Message):
    logger.info(f"Start command received from user {message.from_user.id}")
    user_is_admin = await is_admin(message.from_user.id)
    
    welcome_msg = "🤖 **TeraBox Downloader Bot**\n\n"
    if user_is_admin:
        welcome_msg += "👑 Welcome back, Admin!\n\n"
        welcome_msg += "• Send TeraBox links to download videos\n"
        welcome_msg += "• Use /settings to configure bot\n"
        welcome_msg += "• Configure admin & channel broadcasting\n\n"
    else:
        welcome_msg += "📥 Send me TeraBox links and I'll download videos for you!\n\n"
    
    await message.answer(welcome_msg, parse_mode="Markdown")

@router.message(Command("settings"))
async def settings_command(message: Message):
    user_id = message.from_user.id
    
    # Check if user is already admin
    if await is_admin(user_id):
        await show_settings(message)
    else:
        # Ask for password
        pending_auth[user_id] = "awaiting_password"
        await message.answer("🔐 Enter admin password to access settings:")

pending_auth = {}

async def show_settings(message: Message):
    """Show settings menu to admin"""
    config = await get_config()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"📡 Admin Broadcast: {'✅ ON' if config['admin_broadcast_enabled'] else '❌ OFF'}",
            callback_data="toggle_admin_broadcast"
        )],
        [InlineKeyboardButton(
            text=f"📺 Channel Broadcast: {'✅ ON' if config['channel_broadcast_enabled'] else '❌ OFF'}",
            callback_data="toggle_channel_broadcast"
        )],
        [InlineKeyboardButton(
            text="🆔 Set Broadcast Chat ID(s)",
            callback_data="set_broadcast_id"
        )],
    ])
    await message.answer(
        build_settings_text(config),
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

def build_settings_text(config):
    return (
        f"⚙️ **Bot Settings**\n\n"
        f"📡 Admin Broadcast: {'✅ Enabled' if config['admin_broadcast_enabled'] else '❌ Disabled'}\n"
        f"   _(When enabled, videos from admin links are broadcasted)_\n\n"
        f"📺 Channel Broadcast: {'✅ Enabled' if config['channel_broadcast_enabled'] else '❌ Disabled'}\n"
        f"   _(When enabled, videos from channel posts are broadcasted)_\n\n"
        f"🆔 Broadcast Chats: {', '.join(map(str, config['broadcast_chats'])) if config['broadcast_chats'] else 'None'}"
    )

@router.callback_query()
async def settings_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    data = callback.data

    # Check if user is admin
    if not await is_admin(user_id):
        await callback.answer("❌ You need admin access!", show_alert=True)
        return

    config = await get_config()

    if data == "toggle_admin_broadcast":
        new_state = not config["admin_broadcast_enabled"]
        await update_config({"admin_broadcast_enabled": new_state})
        await update_settings_message(callback, "📡 Admin Broadcast", new_state)

    elif data == "toggle_channel_broadcast":
        new_state = not config["channel_broadcast_enabled"]
        await update_config({"channel_broadcast_enabled": new_state})
        await update_settings_message(callback, "📺 Channel Broadcast", new_state)

    elif data == "set_broadcast_id":
        await callback.message.answer("📨 Send new broadcast chat ID(s), comma-separated:")
        pending_auth[user_id] = "await_broadcast_ids"
        await callback.answer()

async def update_settings_message(callback: CallbackQuery, label: str, state: bool):
    """Update settings message with new config"""
    config = await get_config()

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"📡 Admin Broadcast: {'✅ ON' if config['admin_broadcast_enabled'] else '❌ OFF'}",
            callback_data="toggle_admin_broadcast"
        )],
        [InlineKeyboardButton(
            text=f"📺 Channel Broadcast: {'✅ ON' if config['channel_broadcast_enabled'] else '❌ OFF'}",
            callback_data="toggle_channel_broadcast"
        )],
        [InlineKeyboardButton(
            text="🆔 Set Broadcast Chat ID(s)",
            callback_data="set_broadcast_id"
        )],
    ])

    await callback.message.edit_text(
        build_settings_text(config),
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await callback.answer(f"{label} turned {'ON' if state else 'OFF'} ✅")

@router.message()
async def handle_message(message: Message):
    user_id = message.from_user.id
    text = (message.text or message.caption or "").strip()
    
    # Handle password authentication
    if user_id in pending_auth:
        state = pending_auth[user_id]
        config = await get_config()
        
        # Password entry
        if state == "awaiting_password":
            if text == config["admin_password"]:
                # Add user as admin
                await add_admin(
                    user_id,
                    message.from_user.username,
                    message.from_user.full_name
                )
                await message.answer("✅ Password accepted! You are now an admin.")
                # Update commands for this user
                await set_bot_commands(user_id)
                del pending_auth[user_id]
                # Show settings
                await show_settings(message)
            else:
                await message.answer("❌ Wrong password.")
                del pending_auth[user_id]
            return
        
        # Broadcast ID entry
        if state == "await_broadcast_ids":
            try:
                ids = [int(x.strip()) for x in text.split(",") if x.strip()]
                await update_config({"broadcast_chats": ids})
                await message.answer(f"✅ Updated broadcast chats: {ids}")
                del pending_auth[user_id]
            except ValueError:
                await message.answer("❌ Invalid format. Please enter numeric chat IDs separated by commas.")
                del pending_auth[user_id]
            return
    
    # Handle TeraBox URLs
    urls = LINK_REGEX.findall(text)
    if not urls:
        return

    chat_id = message.chat.id
    user_is_admin = await is_admin(user_id)
    
    # Determine source type
    source_type = "admin" if user_is_admin else "user"
    
    logger.info(f"{source_type.capitalize()} {user_id} sent TeraBox URL(s)")
    
    for url in urls:
        url = url.rstrip('.,!?')
        logger.info(f"Processing {source_type} URL: {url}")
        asyncio.create_task(process_url(url, chat_id, source_type))

@router.channel_post()
async def handle_channel_post(message: Message):
    """Handle channel posts - only if channel broadcast is enabled"""
    config = await get_config()
    
    # Only listen to channel if channel broadcast is enabled
    if not config["channel_broadcast_enabled"]:
        logger.debug("Channel broadcast disabled - ignoring channel post")
        return
    
    text = (message.text or message.caption or "")
    urls = LINK_REGEX.findall(text)
    if not urls:
        return

    chat_id = message.chat.id
    logger.info(f"🔔 Detected TeraBox URL(s) in channel {chat_id}")

    for url in urls:
        url = url.rstrip('.,!?')
        logger.info(f"📥 Processing channel URL: {url}")
        asyncio.create_task(process_url(url, chat_id, "channel"))

# Attach router
dp.include_router(router)

# Start bot
if __name__ == "__main__":
    async def main():
        await get_config()
        await set_bot_commands()  # Set default commands for all users
        logger.info("🚀 Starting TeraDownloader bot")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

    asyncio.run(main())
