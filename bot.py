import asyncio
import re
import os
import aiohttp
import aiofiles
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
import hashlib

# ===== CONFIG =====
BOT_TOKEN = "8008678561:AAH80tlSuc-tqEYb12eXMfUGfeo7Wz8qUEU"
API_BASE = "https://terabox.itxarshman.workers.dev/api"
MAX_SIZE = 2 * 1024 * 1024 * 1024  # 2GB
MAX_CONCURRENT_LINKS = 100          # Max links processed at once
MAX_CONCURRENT_FILES = 100          # Max files per link being downloaded/uploaded
CHUNK_SIZE = 524288  # 512KB
# ==================

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("TeraboxBot")
logging.getLogger("httpx").setLevel(logging.WARNING)

# Global session
SESSION = None
LINK_SEM = asyncio.Semaphore(MAX_CONCURRENT_LINKS)
FILE_SEM = asyncio.Semaphore(MAX_CONCURRENT_FILES)

LINK_REGEX = re.compile(
    r"https?://[^\s]*?(?:terabox|teraboxapp|teraboxshare|nephobox|1024tera|1024terabox|freeterabox|terasharefile|terasharelink|mirrobox|momerybox|teraboxlink)\.[^\s]+",
    re.IGNORECASE
)

async def get_session():
    global SESSION
    if SESSION is None or SESSION.closed:
        import ssl
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        connector = aiohttp.TCPConnector(limit=200, limit_per_host=50, ssl=ssl_ctx, ttl_dns_cache=300)
        SESSION = aiohttp.ClientSession(connector=connector)
    return SESSION

async def download_file(url: str, path: str, session: aiohttp.ClientSession):
    headers = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.terabox.app/'}
    async with session.get(url, headers=headers, timeout=60, ssl=False) as r:
        r.raise_for_status()
        total = int(r.headers.get('Content-Length', 0))
        downloaded = 0
        async with aiofiles.open(path, 'wb') as f:
            async for chunk in r.content.iter_chunked(CHUNK_SIZE):
                if chunk:
                    await f.write(chunk)
                    downloaded += len(chunk)
        if total > 0 and downloaded < total:
            raise RuntimeError("Incomplete download")

async def upload_and_cleanup(update: Update, path: str, name: str):
    try:
        with open(path, 'rb') as f:
            if name.lower().endswith(('.mp4', '.mkv', '.avi', '.mov')):
                await update.message.reply_video(video=f)
            else:
                await update.message.reply_document(document=f)
        log.info(f"✨ Uploaded: {name}")
    finally:
        if os.path.exists(path):
            os.remove(path)

async def process_single_file(update: Update, file_info: dict):
    name = file_info['name']
    size = file_info.get('size', 0)  # size in bytes
    url = file_info.get('original_url')

    if size > MAX_SIZE:
        await update.message.reply_text(f"❌ Skipped (too large): {name} (>2GB)")
        return

    session = await get_session()
    path = f"/tmp/{hashlib.md5(url.encode()).hexdigest()}_{name}"

    try:
        log.info(f"⬇️ Downloading: {name}")
        await download_file(url, path, session)
        log.info(f"✅ Downloaded: {name}")
        await upload_and_cleanup(update, path, name)
    except Exception as e:
        log.error(f"❌ Failed {name}: {e}")
        await update.message.reply_text(f"❌ Failed: {name} – {str(e)[:100]}")

async def process_link_independently(update: Update, link: str):
    async with LINK_SEM:  # Limit total concurrent links
        try:
            session = await get_session()
            async with session.get(f"{API_BASE}?url={link}", timeout=30, ssl=False) as r:
                data = await r.json()
        except Exception as e:
            await update.message.reply_text(f"❌ Invalid or unreachable link: {link[:50]}...")
            log.error(f"Link fetch failed: {e}")
            return

        files = data.get('links', [])
        if not files:
            await update.message.reply_text("⚠️ No files found in the link.")
            return

        log.info(f"📦 Found {len(files)} file(s) in {link}")

        # Process each file with limited concurrency per link
        tasks = []
        for file_info in files:
            async with FILE_SEM:  # Prevent too many files from one link overwhelming system
                task = asyncio.create_task(process_single_file(update, file_info))
                tasks.append(task)

        # Wait for all files from this link to finish
        await asyncio.gather(*tasks, return_exceptions=True)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or update.message.caption
    if not text:
        return

    links = list(dict.fromkeys(LINK_REGEX.findall(text)))
    if not links:
        return

    log.info(f"🔗 Received {len(links)} link(s) from user {update.effective_user.id}")

    if len(links) == 1:
        await update.message.reply_text("🚀 Processing your Terabox link...")
    else:
        await update.message.reply_text(f"🚀 Processing {len(links)} Terabox links...")

    # Launch each link independently — up to MAX_CONCURRENT_LINKS
    for link in links:
        asyncio.create_task(process_link_independently(update, link))

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚡ *Ultra-Fast Terabox Bot*\n\n"
        "📥 Send any Terabox link(s)!\n"
        "🚀 Processes up to 100 links in parallel\n"
        "📦 Each link downloads all its files\n"
        "⚠️ Max file size: 2GB",
        parse_mode="Markdown"
    )

def main():
    log.info("🚀 Terabox Bot Starting (Simplified Mode)...")
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, handle_message))
    
    async def cleanup(app):
        global SESSION
        if SESSION and not SESSION.closed:
            await SESSION.close()
    app.post_shutdown = cleanup

    app.run_polling()

if __name__ == "__main__":
    main()
