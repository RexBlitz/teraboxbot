import asyncio
import aiohttp
import re
import os
import tempfile
import time
import logging
from aiogram import Bot, Dispatcher, Router, types
from aiogram.types import Message, FSInputFile
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

# Default global config
DEFAULT_CONFIG = {
    "_id": "global",
    "broadcast_enabled": False,
    "channel_listener_enabled": False,
    "broadcast_chats": [-1002780909369,],
    "admin_password": "11223344"
}



session = AiohttpSession(api=TelegramAPIServer.from_base(SELF_HOSTED_API))
bot = Bot(token=BOT_TOKEN, session=session)
dp = Dispatcher()
router = Router(name="terabox_listener")
sem = asyncio.Semaphore(50)  # 50 concurrent connections

async def get_config():
    config = await config_col.find_one({"_id": "global"})
    if not config:
        await config_col.insert_one(DEFAULT_CONFIG)
        return DEFAULT_CONFIG
    return config

async def update_config(update: dict):
    await config_col.update_one({"_id": "global"}, {"$set": update})



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
                    async for chunk in resp.content.iter_chunked(5 * 1024 * 1024):  # 5MB chunks
                        with open(path, 'ab') as f:
                            f.write(chunk)
                        downloaded += len(chunk)
                        if downloaded % (50 * 1024 * 1024) == 0:  # Log every 50MB
                            percent = (downloaded / (total_mb * 1024 * 1024)) * 100 if total_mb else 0
                            logger.info(f"Downloading {filename}: {downloaded // (1024 * 1024)}/{int(total_mb)} MB ({percent:.0f}%)")
                    logger.info(f"Download completed for {filename}")
    except Exception as e:
        logger.error(f"Download error for {filename}: {str(e)}")
        if os.path.exists(path):
            os.unlink(path)
        if attempt < 2:  # Up to 3 attempts with backoff
            backoff = 2 ** attempt  # 1s, 2s, 4s
            logger.info(f"Retrying download for {filename} after {backoff}s")
            await asyncio.sleep(backoff)
            return await download_file(dl_url, filename, size_mb, attempt + 1)
        return False, None
    return True, path

async def broadcast_video(file_path: str, video_name: str):
    config = await get_config()
    if not config["broadcast_enabled"]:
        logger.info(f"Broadcast disabled - skipping {video_name}")
        return

    # Prevent duplicates
    if await broadcast_col.find_one({"name": video_name}):
        logger.info(f"Duplicate broadcast skipped: {video_name}")
        return

    chats = config.get("broadcast_chats", [])
    if not chats:
        logger.warning("No broadcast chats configured")
        return

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


async def process_file(link: dict, source_url: str, is_channel: bool = False):
    name = link.get("name", "unknown")
    size_mb = link.get("size_mb", 0)
    size_gb = size_mb / 1024
    logger.info(f"Processing file: {name}, size: {size_mb} MB")
    if size_gb > 2:
        logger.warning(f"File {name} size {size_gb:.2f} GB exceeds 2 GB limit")
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
                return

            logger.info(f"Successfully downloaded {name}, preparing to broadcast")

            # Only broadcast videos to broadcast chats (no sending back to source)
            if name.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.webm')):
                await broadcast_video(file_path, name)
            else:
                logger.info(f"Skipping non-video file from broadcast: {name}")

        except Exception as e:
            logger.error(f"Error processing {name}: {str(e)}")
        finally:
            if file_path and os.path.exists(file_path):
                logger.debug(f"Cleaning up temporary file: {file_path}")
                os.unlink(file_path)

async def process_url(source_url: str, chat_id: int, is_channel: bool = False):
    logger.info(f"Processing URL: {source_url} from {'channel' if is_channel else 'chat'} {chat_id}")
    response = await get_links(source_url)
    if not response or "links" not in response:
        logger.error(f"Failed to retrieve links for {source_url}")
        if not is_channel:
            await bot.send_message(chat_id, f"Failed to retrieve links for {source_url}.")
        return

    logger.info(f"Found {len(response['links'])} files for {source_url}")
    for link in response["links"]:
        asyncio.create_task(process_file(link, source_url, is_channel))

@router.message(Command("start"))
async def start(message: Message):
    logger.info(f"Start command received from chat ID: {message.chat.id}")
    await message.answer("Send me TeraBox links to download videos.")


# Handle messages in private chats, groups, and supergroups (NOT channels)
@router.message()
async def handle_message(message: Message):
    text = (message.text or message.caption or "")
    urls = LINK_REGEX.findall(text)
    if not urls:
        logger.debug("No valid TeraBox URLs found in message")
        return

    chat_id = message.chat.id
    sender = getattr(message.from_user, "full_name", "Unknown") if message.from_user else "Unknown"
    logger.info(f"Detected TeraBox URL(s) in chat {chat_id} from {sender}")

    for url in urls:
        url = url.rstrip('.,!?')
        logger.info(f"Found URL: {url}")
        asyncio.create_task(process_url(url, chat_id, is_channel=False))

# Handle channel posts - silent processing only
@router.channel_post()
async def handle_channel_post(message: Message):
    # Check if channel listener is enabled
    if not ENABLE_CHANNEL_LISTENER:
        logger.debug("Channel listener disabled - ignoring channel post")
        return

    # Check if broadcast is enabled and configured
    if not ENABLE_BROADCAST or not BROADCAST_CHATS:
        logger.warning("Channel processing requires ENABLE_BROADCAST=True and configured BROADCAST_CHATS - ignoring channel post")
        return

    text = (message.text or message.caption or "")
    urls = LINK_REGEX.findall(text)
    if not urls:
        logger.debug("No valid TeraBox URLs found in channel post")
        return

    chat_id = message.chat.id
    logger.info(f"🔔 Detected TeraBox URL(s) in channel {chat_id} - processing silently")

    for url in urls:
        url = url.rstrip('.,!?')
        logger.info(f"📥 Found URL in channel: {url} - downloading in background")
        # Process silently in background, videos will be sent to broadcast chats only
        asyncio.create_task(process_url(url, chat_id, is_channel=True))


@router.message(Command("settings"))
async def settings_command(message: Message):
    config = await get_config()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"📡 Broadcast: {'✅ ON' if config['broadcast_enabled'] else '❌ OFF'}",
            callback_data="toggle_broadcast"
        )],
        [InlineKeyboardButton(
            text=f"📺 Channel Listener: {'✅ ON' if config['channel_listener_enabled'] else '❌ OFF'}",
            callback_data="toggle_channel"
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
        f"📡 Broadcast: {'✅ Enabled' if config['broadcast_enabled'] else '❌ Disabled'}\n"
        f"📺 Channel Listener: {'✅ Enabled' if config['channel_listener_enabled'] else '❌ Disabled'}\n"
        f"🆔 Broadcast Chats: {', '.join(map(str, config['broadcast_chats'])) if config['broadcast_chats'] else 'None'}"
    )


pending_auth = {}

@router.callback_query()
async def settings_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    data = callback.data

    config = await get_config()

    # Step 1: Check password
    if user_id not in pending_auth and data != "verify_pass":
        pending_auth[user_id] = data
        await callback.message.answer("🔐 Enter admin password:")
        await callback.answer()
        return

    # Step 2: Toggle or update based on action
    if data == "toggle_broadcast":
        new_state = not config["broadcast_enabled"]
        await update_config({"broadcast_enabled": new_state})
        await update_settings_message(callback, "📡 Broadcast", new_state)

    elif data == "toggle_channel":
        new_state = not config["channel_listener_enabled"]
        await update_config({"channel_listener_enabled": new_state})
        await update_settings_message(callback, "📺 Channel Listener", new_state)

    elif data == "set_broadcast_id":
        await callback.message.answer("📨 Send new broadcast chat ID(s), comma-separated:")
        pending_auth[user_id] = "await_broadcast_ids"
        await callback.answer()
async def update_settings_message(callback: CallbackQuery, label: str, state: bool):
    """Edits the current message with updated config"""
    config = await get_config()

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"📡 Broadcast: {'✅ ON' if config['broadcast_enabled'] else '❌ OFF'}",
            callback_data="toggle_broadcast"
        )],
        [InlineKeyboardButton(
            text=f"📺 Channel Listener: {'✅ ON' if config['channel_listener_enabled'] else '❌ OFF'}",
            callback_data="toggle_channel"
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
async def handle_auth_or_id(message: Message):
    user_id = message.from_user.id
    text = message.text.strip()

    if user_id not in pending_auth:
        return

    state = pending_auth[user_id]
    config = await get_config()

    # Password entry
    if state not in ("await_broadcast_ids",):
        if text == config["admin_password"]:
            await message.answer("✅ Password accepted! You can now change settings.")
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

            # Auto-refresh settings message
            async for msg in message.chat.get_history(limit=5):
                if msg.text and msg.text.startswith("⚙️ **Bot Settings**"):
                    config = await get_config()
                    keyboard = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(
                            text=f"📡 Broadcast: {'✅ ON' if config['broadcast_enabled'] else '❌ OFF'}",
                            callback_data="toggle_broadcast"
                        )],
                        [InlineKeyboardButton(
                            text=f"📺 Channel Listener: {'✅ ON' if config['channel_listener_enabled'] else '❌ OFF'}",
                            callback_data="toggle_channel"
                        )],
                        [InlineKeyboardButton(
                            text="🆔 Set Broadcast Chat ID(s)",
                            callback_data="set_broadcast_id"
                        )],
                    ])
                    await msg.edit_text(
                        build_settings_text(config),
                        reply_markup=keyboard,
                        parse_mode="Markdown"
                    )
                    break
        except ValueError:
            await message.answer("❌ Invalid format. Please enter numeric chat IDs separated by commas.")
        del pending_auth[user_id]





# Attach router to dispatcher
dp.include_router(router)

# Start polling
if __name__ == "__main__":
    async def main():
        await get_config()  # Ensure defaults exist
        await set_bot_commands()
        logger.info("🚀 Starting TeraDownloader bot")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

    asyncio.run(main())
