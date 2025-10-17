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

# ===== CONFIG =====
BOT_TOKEN = "8008678561:AAH80tlSuc-tqEYb12eXMfUGfeo7Wz8qUEU"
API_BASE = "https://terabox.itxarshman.workers.dev/api"
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

# Suppress telegram HTTP spam
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("telegram.client").setLevel(logging.WARNING)
logging.getLogger("telegram.vendor.ptb_urllib3.urllib3").setLevel(logging.WARNING)

semaphore = asyncio.Semaphore(CONCURRENT_DOWNLOADS)


# ===== Commands =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👋 *Terabox Downloader Bot*\n\n"
        "📥 Send me any Terabox link(s), and I’ll download them for you.\n"
        "⚠️ Max upload size: 2GB (Telegram limit)\n\n"
        "Commands:\n"
        "/start - Show this message"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


# ===== Setup bot command menu =====
async def setup_bot_commands(application):
    commands = [
        BotCommand("start", "Show start message and usage help"),
    ]
    await application.bot.set_my_commands(commands)
    log.info("✅ Bot command menu set")


# ===== Helper: fetch API info (with retries) =====
async def fetch_api_info(session: aiohttp.ClientSession, link: str):
    last_exc = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            log.info(f"🔍 API: fetching file info for {link} (attempt {attempt+1})")
            timeout = aiohttp.ClientTimeout(total=60)
            async with session.get(f"{API_BASE}?url={link}", timeout=timeout) as resp:
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


# ===== Helper: try to download a URL to file_path (single attempt) =====
async def try_download_url(session: aiohttp.ClientSession, url: str, file_path: str):
    timeout = aiohttp.ClientTimeout(total=900)  # generous for large files
    async with session.get(url, timeout=timeout) as r:
        r.raise_for_status()
        async with aiofiles.open(file_path, "wb") as f:
            async for chunk in r.content.iter_chunked(8192):
                if not chunk:
                    continue
                await f.write(chunk)


# ===== Core: download with refresh & fallback =====
async def download_file_with_refresh(update: Update, link: str, file_info: dict, session: aiohttp.ClientSession):
    filename = file_info["name"]
    size_mb = file_info.get("size_mb", 0)
    download_url = file_info.get("download_url")
    original_url = file_info.get("original_url")

    if size_mb * 1024 * 1024 > MAX_TELEGRAM_SIZE:
        log.warning(f"File too large for Telegram ({size_mb:.2f} MB): {filename}")
        await update.message.reply_text(
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
                # Refresh download URL from API
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
                await update.message.reply_text(f"❌ Failed to download {filename} after multiple attempts")
                return

    # Send to Telegram
    try:
        log.info(f"📤 Uploading {filename} to Telegram")
        if filename.lower().endswith(('.mp4', '.mkv', '.avi')):
            await update.message.reply_video(video=open(file_path, "rb"))
        else:
            await update.message.reply_document(document=open(file_path, "rb"))
        log.info(f"✅ Uploaded {filename} to Telegram")
    except Exception as e:
        log.error(f"Upload failed for {filename}: {e}")
        await update.message.reply_text(
            f"⚠️ Upload failed for {filename}: {e}\n"
            f"👉 [Download Link]({download_url or original_url})"
        )
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)
            log.debug(f"🧹 Deleted temp file: {file_path}")


# ===== Message Handler =====
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or getattr(update.message, "caption", None)
    if not text:
        return

    clean_text = re.sub(r"[^\x20-\x7E]+", " ", text)
    clean_text = re.sub(r"\s+", " ", clean_text)
    links = list(dict.fromkeys(
        re.findall(r"https?://(?:www\.)?(?:terabox|1024terabox|teraboxshare)\.com/s/[A-Za-z0-9_-]+", clean_text)
    ))

    if not links:
        return

    log.info(f"🧾 User {update.effective_user.id} sent {len(links)} link(s)")
    await update.message.reply_text(f"🔍 Found {len(links)} link(s). Processing in background...")

    session = aiohttp.ClientSession()

    async def run_task(link):
        try:
            data = await fetch_api_info(session, link)
            for file_info in data["links"]:
                await download_file_with_refresh(update, link, file_info, session)
        except Exception as e:
            log.error(f"Task failed for {link}: {e}")
            await update.message.reply_text(f"⚠️ Error: {e}")

    for link in links:
        asyncio.create_task(run_task(link))

    # auto close session after idle
    asyncio.create_task(close_session_later(session))


async def close_session_later(session):
    await asyncio.sleep(600)
    if not session.closed:
        await session.close()
        log.info("🧾 Closed aiohttp session after idle timeout.")


# ===== Bot Launcher =====
def run_bot():
    log.info("🚀 Starting Terabox Telegram Bot...")
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Setup bot command menu
    async def post_init(app):
        await setup_bot_commands(app)

    app.post_init = post_init
    app.run_polling()


if __name__ == "__main__":
    run_bot()
