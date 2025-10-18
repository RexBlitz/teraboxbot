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
CONCURRENT_DOWNLOADS = 15  # Reduced from 100 to avoid overwhelming servers
RETRY_ATTEMPTS = 5
RETRY_DELAY = 2  # Increased from 1 second
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

# ===== Terabox Link Regex =====
TERABOX_REGEX = re.compile(
    r"https?://[^\s]*?(?:terabox|teraboxapp|teraboxshare|nephobox|1024tera|1024terabox|freeterabox|terasharefile|terasharelink|mirrobox|momerybox|teraboxlink)\.[^\s]+",
    re.IGNORECASE
)

# ===== Commands =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👋 *Terabox Downloader Bot*\n\n"
        "📥 Send me any Terabox link(s), and I'll download them for you.\n"
        "⚠️ Max upload size: 2GB (Telegram limit)\n\n"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

# ===== Setup bot command menu =====
async def setup_bot_commands(application):
    commands = [
        BotCommand("start", "Show start message and usage help"),
    ]
    await application.bot.set_my_commands(commands)

# ===== Helper: extract Terabox links =====
def extract_terabox_links(text: str):
    if not text:
        return []
    matches = TERABOX_REGEX.findall(text)
    seen = set()
    unique_links = []
    for link in matches:
        normalized = link.rstrip('/').lower()
        if normalized not in seen:
            seen.add(normalized)
            unique_links.append(link)
    return unique_links

# ===== Helper: fetch API info =====
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
                await asyncio.sleep(RETRY_DELAY * (attempt + 1))  # Exponential backoff
            else:
                raise last_exc

# ===== Helper: download URL to file =====
async def try_download_url(session: aiohttp.ClientSession, url: str, file_path: str):
    timeout = aiohttp.ClientTimeout(total=900)
    async with session.get(url, timeout=timeout) as r:
        r.raise_for_status()
        async with aiofiles.open(file_path, "wb") as f:
            async for chunk in r.content.iter_chunked(8192):
                if chunk:
                    await f.write(chunk)

# ===== Core: download file with API refresh on retry =====
async def download_file(update: Update, link: str, file_info: dict, session: aiohttp.ClientSession):
    filename = file_info["name"]
    size_mb = file_info.get("size_mb", 0)
    download_url = file_info.get("original_url")

    if size_mb * 1024 * 1024 > MAX_TELEGRAM_SIZE:
        log.warning(f"File too large ({size_mb:.2f} MB): {filename}")
        await update.message.reply_text(
            f"⚠️ *{filename}* is too large ({size_mb:.2f} MB).\n"
            f"👉 [Download Link]({download_url})",
            parse_mode="Markdown"
        )
        return

    file_hash = hashlib.md5(download_url.encode()).hexdigest()
    file_path = f"/tmp/{file_hash}_{filename}"

    for attempt in range(RETRY_ATTEMPTS):
        try:
            log.info(f"⬇️ Downloading {filename} (attempt {attempt+1})")
            await try_download_url(session, download_url, file_path)
            log.info(f"✅ Downloaded {filename}")
            break
        except (ClientPayloadError, ClientResponseError, asyncio.TimeoutError, ValueError) as e:
            log.warning(f"Download failed for {filename}: {e}")
            if attempt < RETRY_ATTEMPTS - 1:
                log.info(f"Refreshing API info and retrying in {RETRY_DELAY * (attempt + 1)}s...")
                await asyncio.sleep(RETRY_DELAY * (attempt + 1))  # Exponential backoff
                try:
                    data = await fetch_api_info(session, link)
                    fresh_file = next((f for f in data.get("links", []) if f["name"] == filename), None)
                    if fresh_file:
                        download_url = fresh_file.get("original_url")
                        size_mb = fresh_file.get("size_mb", size_mb)
                    else:
                        log.warning(f"File {filename} not found in refreshed API data")
                except Exception as e2:
                    log.warning(f"Failed to refresh API info: {e2}")
            else:
                await update.message.reply_text(
                    f"❌ Failed to download *{filename}* after multiple attempts",
                    parse_mode="Markdown"
                )
                return

    # Send to Telegram
    try:
        log.info(f"📤 Uploading {filename} to Telegram")
        with open(file_path, "rb") as file:
            if filename.lower().endswith(('.mp4', '.mkv', '.avi')):
                await update.message.reply_video(video=file)
            else:
                await update.message.reply_document(document=file)
        log.info(f"✅ Uploaded {filename} to Telegram")
    except Exception as e:
        log.error(f"Upload failed for {filename}: {e}")
        await update.message.reply_text(
            f"⚠️ Upload failed for *{filename}*\n"
            f"👉 [Download Link]({download_url})",
            parse_mode="Markdown"
        )
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)
            log.debug(f"🧹 Deleted temp file: {file_path}")

# ===== Message Handler =====
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or update.message.caption
    if not text and update.message.forward_date:
        text = update.message.text or update.message.caption

    if not text:
        return

    links = extract_terabox_links(text)
    if not links:
        return

    log.info(f"🧾 User {update.effective_user.id} sent {len(links)} link(s)")
    status_msg = await update.message.reply_text(
        f"🔍 Found {len(links)} Terabox link(s). Processing..."
    )

    failed_links = []
    
    async with aiohttp.ClientSession() as session:
        async def run_task(link):
            async with semaphore:
                try:
                    data = await fetch_api_info(session, link)
                    for file_info in data.get("links", []):
                        await download_file(update, link, file_info, session)
                except Exception as e:
                    log.error(f"Task failed for {link}: {e}")
                    failed_links.append(link)

        tasks = [asyncio.create_task(run_task(link)) for link in links]
        await asyncio.gather(*tasks, return_exceptions=True)

    # Send failed links report
    if failed_links:
        failed_text = "❌ Failed to process the following link(s):\n" + "\n".join(failed_links)
        await update.message.reply_text(failed_text)
    
    # Delete status message
    try:
        await status_msg.delete()
    except:
        pass

# ===== Bot Launcher =====
def run_bot():
    log.info("🚀 Starting Terabox Telegram Bot...")
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.CAPTION) & ~filters.COMMAND,
        handle_message
    ))

    async def post_init(app):
        await setup_bot_commands(app)

    app.post_init = post_init
    app.run_polling()

if __name__ == "__main__":
    run_bot()
