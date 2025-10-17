import asyncio
import re
import os
import aiohttp
import aiofiles
import logging
from telegram import Update, BotCommand
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
import hashlib
from aiohttp import ClientPayloadError, ClientResponseError
from motor.motor_asyncio import AsyncIOMotorClient

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

semaphore = asyncio.Semaphore(CONCURRENT_DOWNLOADS)

# ===== MongoDB Setup =====
mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client["terabox"]
chats_col = db["terabox_chats"]


# ===== Database Helpers =====
async def get_chat_config(chat_id: int):
    doc = await chats_col.find_one({"_id": chat_id})
    if not doc:
        doc = {"_id": chat_id, "target_chat": None, "auto_fetch": False}
        await chats_col.insert_one(doc)
    return doc


async def update_chat_config(chat_id: int, data: dict):
    await chats_col.update_one({"_id": chat_id}, {"$set": data}, upsert=True)


# ===== Bot Commands =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👋 *Terabox Downloader Bot*\n\n"
        "📥 Send me any Terabox link(s), and I’ll download them for you.\n"
        "⚙️ Works in groups/channels!\n\n"
        "Commands:\n"
        "/start - Show help\n"
        "/fetch [count|all] - Fetch & download links from history\n"
        "/settarget <chat_id> - Set target chat to send results\n"
        "/autofetch on|off - Auto listen for new links\n"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def set_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Usage: `/settarget <chat_id>`", parse_mode="Markdown")

    chat_id = int(context.args[0])
    await update_chat_config(update.effective_chat.id, {"target_chat": chat_id})
    await update.message.reply_text(f"🎯 Target chat set to `{chat_id}`", parse_mode="Markdown")
    log.info(f"🎯 Target chat for {update.effective_chat.id} set to {chat_id}")


async def auto_fetch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Usage: `/autofetch on|off`", parse_mode="Markdown")

    choice = context.args[0].lower()
    enabled = choice == "on"
    await update_chat_config(update.effective_chat.id, {"auto_fetch": enabled})

    msg = "✅ Auto-fetch enabled." if enabled else "⛔ Auto-fetch disabled."
    await update.message.reply_text(msg)
    log.info(f"Auto-fetch for {update.effective_chat.id} -> {enabled}")


# ===== Helpers =====
async def fetch_api_info(session: aiohttp.ClientSession, link: str):
    for attempt in range(RETRY_ATTEMPTS):
        try:
            timeout = aiohttp.ClientTimeout(total=60)
            async with session.get(f"{API_BASE}?url={link}", timeout=timeout) as resp:
                resp.raise_for_status()
                data = await resp.json()
            if not data.get("links"):
                raise ValueError("API returned no links")
            return data
        except Exception as e:
            log.warning(f"Fetch API failed ({attempt+1}/{RETRY_ATTEMPTS}): {e}")
            await asyncio.sleep(RETRY_DELAY)
    raise Exception("API failed after retries")


async def try_download_url(session: aiohttp.ClientSession, url: str, file_path: str):
    timeout = aiohttp.ClientTimeout(total=900)
    async with session.get(url, timeout=timeout) as r:
        r.raise_for_status()
        async with aiofiles.open(file_path, "wb") as f:
            async for chunk in r.content.iter_chunked(8192):
                await f.write(chunk)


async def download_file_with_refresh(bot, link: str, file_info: dict, session: aiohttp.ClientSession, send_chat_id: int):
    filename = file_info["name"]
    size_mb = file_info.get("size_mb", 0)
    download_url = file_info.get("download_url")
    original_url = file_info.get("original_url")

    if size_mb * 1024 * 1024 > MAX_TELEGRAM_SIZE:
        await bot.send_message(send_chat_id, f"⚠️ {filename} too large ({size_mb:.1f} MB)\n👉 {download_url or original_url}")
        return

    file_hash = hashlib.md5((download_url or original_url).encode()).hexdigest()
    file_path = f"/tmp/{file_hash}_{filename}"

    for attempt in range(RETRY_ATTEMPTS):
        try:
            await try_download_url(session, download_url, file_path)
            break
        except Exception as e:
            log.warning(f"Retry download {filename}: {e}")
            await asyncio.sleep(RETRY_DELAY)
    else:
        await bot.send_message(send_chat_id, f"❌ Failed to download {filename}")
        return

    try:
        if filename.lower().endswith(('.mp4', '.mkv', '.avi')):
            await bot.send_video(chat_id=send_chat_id, video=open(file_path, "rb"))
        else:
            await bot.send_document(chat_id=send_chat_id, document=open(file_path, "rb"))
    except Exception as e:
        log.error(f"Upload failed: {e}")
        await bot.send_message(send_chat_id, f"⚠️ Upload failed: {e}\n👉 {download_url or original_url}")
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)


def extract_links(text: str):
    clean_text = re.sub(r"[^\x20-\x7E]+", " ", text)
    clean_text = re.sub(r"\s+", " ", clean_text)
    return list(dict.fromkeys(
        re.findall(r"https?://(?:www\.)?(?:terabox|1024terabox|teraboxshare)\.com/s/[A-Za-z0-9_-]+", clean_text)
    ))


# ===== Handlers =====
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or getattr(update.message, "caption", None)
    if not text:
        return
    links = extract_links(text)
    if not links:
        return

    chat_cfg = await get_chat_config(update.effective_chat.id)
    if not chat_cfg.get("auto_fetch", False):
        return  # only auto download if enabled

    await update.message.reply_text(f"🔍 Found {len(links)} link(s), processing...")
    send_chat = chat_cfg.get("target_chat") or update.message.chat_id

    session = aiohttp.ClientSession()
    for link in links:
        try:
            data = await fetch_api_info(session, link)
            for file_info in data["links"]:
                await download_file_with_refresh(context.bot, link, file_info, session, send_chat)
        except Exception as e:
            log.error(f"Error {link}: {e}")
            await update.message.reply_text(f"⚠️ Error: {e}")
    await session.close()


# ===== Command: fetch links manually =====
async def fetch_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    count_arg = context.args[0] if context.args else "5"
    count = 0 if count_arg.lower() == "all" else int(count_arg)

    msgs = await context.bot.get_chat_history(chat_id=update.effective_chat.id, limit=count or None)
    links = []
    for msg in msgs:
        text = msg.text or msg.caption
        if text:
            links.extend(extract_links(text))
    links = list(dict.fromkeys(links))

    if not links:
        return await update.message.reply_text("❌ No Terabox links found.")

    await update.message.reply_text(f"📦 Found {len(links)} link(s), downloading...")
    chat_cfg = await get_chat_config(update.effective_chat.id)
    send_chat = chat_cfg.get("target_chat") or update.message.chat_id

    session = aiohttp.ClientSession()
    for link in links:
        try:
            data = await fetch_api_info(session, link)
            for file_info in data["links"]:
                await download_file_with_refresh(context.bot, link, file_info, session, send_chat)
        except Exception as e:
            await update.message.reply_text(f"⚠️ Error processing {link}: {e}")
    await session.close()


# ===== Setup Bot Commands =====
async def setup_bot_commands(application):
    commands = [
        BotCommand("start", "Show help message"),
        BotCommand("fetch", "Fetch & download links"),
        BotCommand("settarget", "Set target chat/channel"),
        BotCommand("autofetch", "Enable/disable auto link downloading"),
    ]
    await application.bot.set_my_commands(commands)



# ===== Run Bot =====
def run_bot():
    log.info("🚀 Starting Terabox Telegram Bot...")
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("fetch", fetch_links))
    app.add_handler(CommandHandler("settarget", set_target))
    app.add_handler(CommandHandler("autofetch", auto_fetch))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    async def post_init(app):
        await setup_bot_commands(app)
    app.post_init = post_init

    app.run_polling()


if __name__ == "__main__":
    run_bot()
