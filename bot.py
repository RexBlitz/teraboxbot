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
pending_auth = {} # Used for admin password/ID entry


async def get_config():
    """
    Retrieves global config, creating the default if none exists,
    or merging with defaults to ensure all keys are present.
    """
    config = await config_col.find_one({"_id": "global"})
    
    if not config:
        # No config found: insert default and return it
        await config_col.insert_one(DEFAULT_CONFIG)
        logger.info("Inserted new global configuration.")
        return DEFAULT_CONFIG
    
    # Config found: check for missing keys and merge
    needs_update = False
    for key, default_value in DEFAULT_CONFIG.items():
        if key not in config:
            config[key] = default_value
            needs_update = True
            logger.warning(f"Config missing key '{key}'. Added default value: {default_value}")
    
    # If keys were missing, update the database document
    if needs_update:
        # Use update_one to save the merged dictionary back to MongoDB
        await config_col.update_one({"_id": "global"}, {"$set": {k: v for k, v in config.items() if k != "_id"}})
        logger.info("Updated existing global configuration with missing keys.")
        
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
    # ... (Keep this function as is)
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
            try:
                 await bot.set_my_commands(commands, scope=types.BotCommandScopeChat(chat_id=user_id))
            except Exception as e:
                logger.warning(f"Failed to set commands for user {user_id}: {e}")
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

async def download_file(dl_url: str, filename: str, size_mb: float, status_message: Message, attempt: int = 0):
    """
    Downloads the file and updates the status_message with progress.
    """
    path = tempfile.NamedTemporaryFile(delete=False).name
    downloaded = 0
    start_time = time.time()
    last_update_time = 0
    
    logger.info(f"Starting download of {filename} from {dl_url} (attempt {attempt + 1})")
    
    try:
        async with sem:
            async with aiohttp.ClientSession() as session:
                async with session.get(dl_url) as resp:
                    if resp.status != 200:
                        logger.error(f"Download failed for {filename}, status: {resp.status}")
                        raise Exception(f"HTTP Status {resp.status}")
                        
                    content_length = int(resp.headers.get('Content-Length', 0))
                    total_mb = content_length / (1024 * 1024) if content_length else size_mb
                    
                    # 5MB chunk size for downloading
                    async for chunk in resp.content.iter_chunked(5 * 1024 * 1024):
                        with open(path, 'ab') as f:
                            f.write(chunk)
                        downloaded += len(chunk)
                        
                        # Update progress every 5 seconds to avoid rate limits
                        if time.time() - last_update_time > 5:
                            
                            elapsed = time.time() - start_time
                            speed_bps = (downloaded / elapsed) if elapsed > 0 else 0
                            speed_mbps = speed_bps / (1024 * 1024)
                            
                            percent = (downloaded / (total_mb * 1024 * 1024)) * 100 if total_mb else 0
                            
                            progress_text = (
                                f"📥 **Downloading** `{filename}`\n"
                                f"📦 Size: **{total_mb:.2f} MB**\n"
                                f"⬇️ Progress: **{downloaded / (1024 * 1024):.2f}/{total_mb:.2f} MB** (**{percent:.0f}%**)\n"
                                f"⚡ Speed: **{speed_mbps:.2f} MB/s**"
                            )
                            
                            try:
                                await bot.edit_message_text(
                                    chat_id=status_message.chat.id, 
                                    message_id=status_message.message_id, 
                                    text=progress_text,
                                    parse_mode="Markdown"
                                )
                                last_update_time = time.time()
                            except TelegramBadRequest as e:
                                # Ignore "Message is not modified" errors
                                if "message is not modified" not in str(e):
                                    logger.error(f"Telegram update error: {e}")
                                
                    logger.info(f"Download completed for {filename}")
    
    except Exception as e:
        logger.error(f"Download error for {filename}: {str(e)}")
        if os.path.exists(path):
            os.unlink(path)
        
        if attempt < 2:
            backoff = 2 ** attempt
            logger.info(f"Retrying download for {filename} after {backoff}s")
            await asyncio.sleep(backoff)
            return await download_file(dl_url, filename, size_mb, status_message, attempt + 1)
        
        # If all attempts fail, update the message with a failure notice
        try:
            await status_message.edit_text(f"❌ Failed to download {filename} after {attempt+1} attempts.")
        except:
            pass
        return False, None
    
    # After successful download, delete the status message before sending the video
    try:
        await bot.delete_message(status_message.chat.id, status_message.message_id)
    except:
        pass # Ignore failure to delete if message was already gone
        
    return True, path

# ... (Keep broadcast_video, send_video_to_user as is)
async def broadcast_video(file_path: str, video_name: str, broadcast_type: str):
    """
    Broadcast video to configured chats
    broadcast_type: 'admin' or 'channel'
    """
    config = await get_config()
    # ... (Keep the rest of the original broadcast_video logic)
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

async def process_file(link: dict, source_url: str, original_chat_id: int = None, source_type: str = "user", status_message: Message = None):
    """
    Process and download file
    source_type: 'user' (regular user), 'admin' (admin user), 'channel' (channel post)
    """
    name = link.get("name", "unknown")
    size_mb = link.get("size_mb", 0)
    size_gb = size_mb / 1024
    logger.info(f"Processing file: {name}, size: {size_mb} MB, source: {source_type}")
    
    # Pre-checks (Only for non-channel)
    if source_type != "channel":
        if size_gb > 2:
            logger.warning(f"File {name} size {size_gb:.2f} GB exceeds 2 GB limit")
            if status_message:
                await status_message.edit_text(f"❌ File `{name}` is too large (**{size_gb:.2f} GB**). Max 2 GB.", parse_mode="Markdown")
            return

        # Only process videos
        if not name.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.webm')):
            logger.info(f"Skipping non-video file: {name}")
            if status_message:
                await status_message.edit_text(f"ℹ️ Skipped non-video file: `{name}`. Only video files are processed.", parse_mode="Markdown")
            return

    file_path = None
    new_link = None
    
    # If it's a channel post and checks passed (or no status_message), send a placeholder
    if not status_message and original_chat_id and source_type != "channel":
        status_message = await bot.send_message(original_chat_id, f"🔍 Found: `{name}`. Initializing download...", parse_mode="Markdown")
    
    # If a channel post, we don't send a status message, we just process silently
    if source_type == "channel":
        logger.info(f"Silently processing channel file: {name}")


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
                # Pass the status message for progress updates
                success, file_path = await download_file(dl_url, name, size_mb, status_message)
                if success:
                    break
                logger.warning(f"{label.capitalize()} failed for {name}, retrying...")

            if not file_path:
                logger.error(f"File {name} failed to download after all retries")
                # If no status message (channel), log only
                if status_message and source_type != "channel":
                    # download_file already updated the message with failure
                    pass
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
            if status_message and source_type != "channel":
                await status_message.edit_text(f"❌ An error occurred while processing `{name}`.", parse_mode="Markdown")
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

    links = [link for link in response["links"] if link.get("name", "").lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.webm'))]
    non_video_count = len(response["links"]) - len(links)
    
    logger.info(f"Found {len(links)} video files for {source_url}")
    
    if source_type != "channel":
        
        if len(links) == 0:
            await bot.send_message(chat_id, f"⚠️ Found no video files in the link.")
        else:
            await bot.send_message(chat_id, f"📥 Found **{len(links)}** video file(s) and **{non_video_count}** other file(s). Starting downloads...", parse_mode="Markdown")

    for link in links:
        # For non-channel posts, send an initial message to be used for progress updates
        status_message = None
        if source_type != "channel":
            name = link.get("name", "unknown")
            status_message = await bot.send_message(chat_id, f"🔍 Found: `{name}`. Initializing download...", parse_mode="Markdown")
            
        asyncio.create_task(process_file(link, source_url, chat_id, source_type, status_message))


@router.message(Command("start"))
async def start(message: Message):
    # ... (Keep this function as is)
    logger.info(f"Start command received from user {message.from_user.id}")
    user_is_admin = await is_admin(message.from_user.id)
    
    welcome_msg = "🤖 **TeraBox Downloader Bot**\n\n"
    if user_is_admin:
        welcome_msg += "👑 Welcome back, Admin!\n\n"
        welcome_msg += "📌 **Admin Features:**\n"
        welcome_msg += "• Send TeraBox links to download videos\n"
        welcome_msg += "• Use /settings to configure bot\n"
        welcome_msg += "• Configure admin & channel broadcasting\n\n"
    else:
        welcome_msg += "📥 Send me TeraBox links and I'll download videos for you!\n\n"
        welcome_msg += "🔐 **Admin Access:**\n"
        welcome_msg += "If you're an admin, type /settings to unlock admin features."
    
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


async def show_settings(message: Message):
    # ... (Keep this function as is)
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
    try:
        await message.answer(
            build_settings_text(config),
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
    except TelegramBadRequest as e:
        # This handles cases where settings is called via /settings and then via a password
        if "message is not modified" not in str(e):
             await message.answer(
                build_settings_text(config),
                reply_markup=keyboard,
                parse_mode="Markdown"
            )
        
def build_settings_text(config):
    # ... (Keep this function as is)
    return (
        f"⚙️ **Bot Settings**\n\n"
        f"📡 Admin Broadcast: {'✅ Enabled' if config['admin_broadcast_enabled'] else '❌ Disabled'}\n"
        f"  _(When enabled, videos from admin links are broadcasted)_\n\n"
        f"📺 Channel Broadcast: {'✅ Enabled' if config['channel_broadcast_enabled'] else '❌ Disabled'}\n"
        f"  _(When enabled, videos from channel posts are broadcasted)_\n\n"
        f"🆔 Broadcast Chats: {', '.join(map(str, config['broadcast_chats'])) if config['broadcast_chats'] else 'None'}"
    )

@router.callback_query()
async def settings_callback(callback: CallbackQuery):
    # ... (Keep this function as is)
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
    # ... (Keep this function as is)
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
    
    # Check for commands explicitly to prevent interference (e.g. if user types /start in password entry)
    if text.startswith("/"):
        return

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
                await message.answer("❌ Wrong password. Try /settings again.")
                del pending_auth[user_id]
            return
        
        # Broadcast ID entry
        if state == "await_broadcast_ids":
            try:
                ids = [int(x.strip()) for x in text.split(",") if x.strip()]
                await update_config({"broadcast_chats": ids})
                await message.answer(f"✅ Updated broadcast chats: {ids}")
                del pending_auth[user_id]
                # Show updated settings menu
                await show_settings(message)
            except ValueError:
                await message.answer("❌ Invalid format. Please enter numeric chat IDs separated by commas.")
                del pending_auth[user_id]
            return
    
    # Handle TeraBox URLs
    urls = LINK_REGEX.findall(text)
    if not urls:
        # If the message is not a command, not an admin password, and contains no links, ignore it silently.
        return

    chat_id = message.chat.id
    user_is_admin = await is_admin(user_id)
    
    # Determine source type
    source_type = "admin" if user_is_admin else "user"
    
    logger.info(f"{source_type.capitalize()} {user_id} sent TeraBox URL(s)")
    
    for url in urls:
        url = url.rstrip('.,!?')
        logger.info(f"Processing {source_type} URL: {url}")
        # Process the URL to find links and start tasks
        asyncio.create_task(process_url(url, chat_id, source_type))

@router.channel_post()
async def handle_channel_post(message: Message):
    # ... (Keep this function as is, it's correct for silent channel processing)
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
        # Process URL without sending status messages (status_message=None in process_file)
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
