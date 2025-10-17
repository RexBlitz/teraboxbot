import asyncio
import re
import os
import aiohttp
import aiofiles
import logging
import urllib.parse
from telegram import Update, BotCommand
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
import hashlib
from motor.motor_asyncio import AsyncIOMotorClient
from aiohttp import ClientPayloadError, ClientResponseError

# ===== CONFIG =====
BOT_TOKEN = "8008678561:AAH80tlSuc-tqEYb12eXMfUGfeo7Wz8qUEU"
API_BASE = "https://terabox.itxarshman.workers.dev/api"
MONGO_URI = "mongodb+srv://irexanon:xUf7PCf9cvMHy8g6@rexdb.d9rwo.mongodb.net/?retryWrites=true&w=majority&appName=RexDB"
MAX_TELEGRAM_SIZE = 2000 * 1024 * 1024  # 2GB
CONCURRENT_DOWNLOADS = 100
RETRY_ATTEMPTS = 5
RETRY_DELAY = 1
# ==================

# ===== Logging Setup =====
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("TeraboxBot")

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("telegram.client").setLevel(logging.WARNING)
logging.getLogger("telegram.vendor.ptb_urllib3.urllib3").setLevel(logging.WARNING)

semaphore = asyncio.Semaphore(CONCURRENT_DOWNLOADS)

# ===== MongoDB Setup =====
mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client["terabox"]
chats_col = db["terabox_chats"]

# ===== Database Helpers =====
async def get_chat_config(chat_id: int):
    doc = await chats_col.find_one({"_id": chat_id})
    if not doc:
        is_private = chat_id > 0  # Positive chat_id indicates private chat
        doc = {"_id": chat_id, "source_chat": None, "target_chat": None, "auto_fetch": is_private}
        await chats_col.insert_one(doc)
    return doc

async def update_chat_config(chat_id: int, data: dict):
    await chats_col.update_one({"_id": chat_id}, {"$set": data}, upsert=True)

# ===== Commands =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👋 *Terabox Downloader Bot*\n\n"
        "📥 Send Terabox links in private chat or set a source channel.\n"
        "⚠️ Max upload size: 2GB (Telegram limit)\n\n"
        "Commands:\n"
        "/start - Show this message\n"
        "/setsource <chat_id> - Set source channel for links\n"
        "/settarget <chat_id> - Set target chat for files\n"
        "/autofetch on|off - Enable/disable auto-fetch for source channel\n"
        "/fetch [count|all] - Fetch links from source channel\n"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def set_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Usage: `/setsource <chat_id>`", parse_mode="Markdown")
    
    try:
        source_chat = int(context.args[0])
        await update_chat_config(update.effective_chat.id, {"source_chat": source_chat})
        await update.message.reply_text(f"🎯 Source channel set to `{source_chat}`", parse_mode="Markdown")
        log.info(f"🧾 Source channel set to {source_chat}")
    except ValueError:
        await update.message.reply_text("❌ Invalid chat ID", parse_mode="Markdown")

async def set_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Usage: `/settarget <chat_id>`", parse_mode="Markdown")
    
    try:
        target_chat = int(context.args[0])
        await update_chat_config(update.effective_chat.id, {"target_chat": target_chat})
        await update.message.reply_text(f"🎯 Target chat set to `{target_chat}`", parse_mode="Markdown")
        log.info(f"🧾 Target chat set to {target_chat}")
    except ValueError:
        await update.message.reply_text("❌ Invalid chat ID", parse_mode="Markdown")

async def auto_fetch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Usage: `/autofetch on|off`", parse_mode="Markdown")

    chat_cfg = await get_chat_config(update.effective_chat.id)
    if not chat_cfg.get("source_chat"):
        return await update.message.reply_text("❌ Set a source channel first with `/setsource <chat_id>`", parse_mode="Markdown")

    choice = context.args[0].lower()
    enabled = choice == "on"
    await update_chat_config(update.effective_chat.id, {"auto_fetch": enabled})
    msg = f"✅ Auto-fetch enabled for source channel `{chat_cfg['source_chat']}`." if enabled else f"⛔ Auto-fetch disabled for source channel `{chat_cfg['source_chat']}`."
    await update.message.reply_text(msg, parse_mode="Markdown")
    log.info(f"🧾 Auto-fetch set to {enabled} for source channel {chat_cfg['source_chat']}")

async def fetch_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_cfg = await get_chat_config(update.effective_chat.id)
    source_chat = chat_cfg.get("source_chat")
    if not source_chat:
        return await update.message.reply_text("❌ Set a source channel first with `/setsource <chat_id>`", parse_mode="Markdown")

    count_arg = context.args[0] if context.args else "5"
    count = None if count_arg.lower() == "all" else int(count_arg)

    try:
        messages = await context.bot.get_chat_history(chat_id=source_chat, limit=count)
        links = []
        for msg in messages:
            text = msg.text or getattr(msg, "caption", None)
            if text:
                links.extend(extract_links(text))
        links = list(dict.fromkeys(links))  # Remove duplicates

        if not links:
            return await update.message.reply_text("❌ No Terabox links found in source channel.")

        send_chat_id = chat_cfg.get("target_chat") or update.effective_chat.id
        await update.message.reply_text(f"🔍 Found {len(links)} link(s). Processing in background...")
        log.info(f"🧾 User {update.effective_user.id} sent {len(links)} link(s)")

        async with semaphore:
            session = aiohttp.ClientSession()
            try:
                for link in links:
                    asyncio.create_task(run_task(context.bot, link, session, send_chat_id, update.effective_chat.id))
            finally:
                asyncio.create_task(close_session_later(session))
    except Exception as e:
        log.error(f"Task failed for fetch command: {e}")
        await update.message.reply_text(f"❌ Error fetching history: {e}", parse_mode="Markdown")

# ===== Helpers =====
async def fetch_api_info(session: aiohttp.ClientSession, link: str):
    encoded_link = urllib.parse.quote(link, safe='')
    url = f"{API_BASE}?url={encoded_link}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    last_exc = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            log.info(f"🔍 API: fetching file info for {link} (attempt {attempt+1})")
            timeout = aiohttp.ClientTimeout(total=60)
            async with session.get(url, headers=headers, timeout=timeout) as resp:
                resp.raise_for_status()
                data = await resp.json()
                if not data.get("links"):
                    raise ValueError("API returned no links")
                return data
        except Exception as e:
            last_exc = e
            log.warning(f"API fetch failed for {link}: {e}")
            if attempt < RETRY_ATTEMPTS - 1:
                await asyncio.sleep(RETRY_DELAY)
            else:
                raise last_exc

async def try_download_url(session: aiohttp.ClientSession, url: str, file_path: str):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    timeout = aiohttp.ClientTimeout(total=1200)  # 20 minutes for large files
    async with session.get(url, headers=headers, timeout=timeout) as r:
        r.raise_for_status()
        async with aiofiles.open(file_path, "wb") as f:
            async for chunk in r.content.iter_chunked(8192):
                if not chunk:
                    log.debug(f"Received empty chunk for {file_path}")
                    continue
                await f.write(chunk)

async def download_file_with_refresh(bot, link: str, file_info: dict, session: aiohttp.ClientSession, send_chat_id: int, user_chat_id: int):
    filename = file_info["name"]
    size_mb = file_info.get("size_mb", 0)
    download_url = file_info.get("download_url")
    original_url = file_info.get("original_url")

    if size_mb * 1024 * 1024 > MAX_TELEGRAM_SIZE:
        log.warning(f"File too large for Telegram ({size_mb:.2f} MB): {filename}")
        await bot.send_message(
            send_chat_id,
            f"⚠️ *{filename}* is too large ({size_mb:.2f} MB).\n"
            f"👉 [Download Link]({download_url or original_url})",
            parse_mode="Markdown"
        )
        return

    file_hash = hashlib.md5((download_url or original_url).encode()).hexdigest()
    file_path = f"/tmp/{file_hash}_{filename}"

    for attempt in range(RETRY_ATTEMPTS):
        try:
            log.info(f"⬇️ Downloading {filename} (attempt {attempt+1})")
            try:
                if download_url:
                    await try_download_url(session, download_url, file_path)
                else:
                    raise ValueError("No download_url available")
            except Exception as e:
                log.warning(f"Download URL failed: {e}")
                if original_url:
                    log.info(f"Trying original_url for {filename}")
                    await try_download_url(session, original_url, file_path)
                else:
                    raise
            log.info(f"✅ Downloaded {filename}")
            break
        except (ClientPayloadError, ClientResponseError, asyncio.TimeoutError, ValueError) as e:
            log.warning(f"Download failed for {filename}: {e}")
            if attempt < RETRY_ATTEMPTS - 1:
                log.info(f"Refreshing download URL and retrying in {RETRY_DELAY}s...")
                try:
                    data = await fetch_api_info(session, link)
                    fresh_file = next((f for f in data["links"] if f["name"] == filename), None)
                    if fresh_file:
                        download_url = fresh_file.get("download_url")
                        original_url = fresh_file.get("original_url")
                except Exception as e2:
                    log.warning(f"Failed to refresh URL: {e2}")
                await asyncio.sleep(RETRY_DELAY)
            else:
                await bot.send_message(
                    user_chat_id,
                    f"❌ Failed to download {filename} after multiple attempts",
                    parse_mode="Markdown"
                )
                return

    try:
        log.info(f"📤 Uploading {filename} to Telegram")
        if filename.lower().endswith(('.mp4', '.mkv', '.avi')):
            await bot.send_video(chat_id=send_chat_id, video=open(file_path, "rb"))
        else:
            await bot.send_document(chat_id=send_chat_id, document=open(file_path, "rb"))
        log.info(f"✅ Uploaded {filename} to Telegram")
    except Exception as e:
        log.error(f"Upload failed for {filename}: {e}")
        await bot.send_message(
            user_chat_id,
            f"⚠️ Upload failed for {filename}: {e}\n"
            f"👉 [Download Link]({download_url or original_url})",
            parse_mode="Markdown"
        )
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)
            log.debug(f"🧹 Deleted temp file: {file_path}")

def extract_links(text: str):
    clean_text = re.sub(r"[^\x20-\x7E]+", " ", text)
    clean_text = re.sub(r"\s+", " ", clean_text)
    return list(dict.fromkeys(
        re.findall(r"https?://(?:www\.)?(?:terabox|1024terabox|teraboxshare)\.com/s/[A-Za-z0-9_-]+", clean_text)
    ))

# ===== Message Handler =====
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or getattr(update.message, "caption", None)
    if not text:
        return

    links = extract_links(text)
    log.info(f"🧾 User {update.effective_user.id} sent {len(links)} link(s)")
    if not links:
        return

    current_chat_id = update.effective_chat.id
    chat_cfg = await get_chat_config(current_chat_id)
    is_private = current_chat_id > 0
    is_source_chat = chat_cfg.get("source_chat") == current_chat_id
    should_process = is_private or (is_source_chat and chat_cfg.get("auto_fetch", False))

    if not should_process:
        return

    send_chat_id = chat_cfg.get("target_chat") or update.effective_chat.id
    await update.message.reply_text(f"🔍 Found {len(links)} link(s). Processing in background...")

    async with semaphore:
        session = aiohttp.ClientSession()
        try:
            for link in links:
                asyncio.create_task(run_task(context.bot, link, session, send_chat_id, update.effective_chat.id))
        finally:
            asyncio.create_task(close_session_later(session))

async def run_task(bot, link, session, send_chat_id, user_chat_id):
    async with semaphore:
        try:
            data = await fetch_api_info(session, link)
            for file_info in data["links"]:
                await download_file_with_refresh(bot, link, file_info, session, send_chat_id, user_chat_id)
        except Exception as e:
            log.error(f"Task failed for {link}: {e}")
            await bot.send_message(user_chat_id, f"⚠️ Error: {e}", parse_mode="Markdown")

async def close_session_later(session):
    await asyncio.sleep(600)
    if not session.closed:
        await session.close()
        log.info("🧾 Closed aiohttp session after idle timeout.")

# ===== Setup Bot Commands =====
async def setup_bot_commands(application):
    commands = [
        BotCommand("start", "Show start message and usage help"),
        BotCommand("setsource", "Set source channel for links"),
        BotCommand("settarget", "Set target chat for files"),
        BotCommand("autofetch", "Enable/disable auto-fetch for source channel"),
        BotCommand("fetch", "Fetch links from source channel"),
    ]
    await application.bot.set_my_commands(commands)
    log.info("✅ Bot command menu set")

# ===== Bot Launcher =====
def run_bot():
    log.info("🚀 Starting Terabox Telegram Bot...")
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setsource", set_source))
    app.add_handler(CommandHandler("settarget", set_target))
    app.add_handler(CommandHandler("autofetch", auto_fetch))
    app.add_handler(CommandHandler("fetch", fetch_links))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    async def post_init(app):
        await setup_bot_commands(app)
    app.post_init = post_init
    app.run_polling()

if __name__ == "__main__":
    run_bot()
