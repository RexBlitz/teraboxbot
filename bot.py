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
MAX_CONCURRENT_LINKS = 50          # Max links processed at once
CHUNK_SIZE = 1024 * 1024           # 1MB chunks (optimal for high-speed I/O)
TIMEOUT = 240                      # Seconds
# ==================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("TeraboxBot")
logging.getLogger("httpx").setLevel(logging.WARNING)

# Global session
SESSION = None
LINK_SEM = asyncio.Semaphore(MAX_CONCURRENT_LINKS)

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
        connector = aiohttp.TCPConnector(
            limit=200,
            limit_per_host=50,
            ssl=ssl_ctx,
            ttl_dns_cache=300,
            use_dns_cache=True
        )
        SESSION = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=TIMEOUT)
        )
    return SESSION

async def download_file(url: str, path: str, session: aiohttp.ClientSession):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': 'https://www.terabox.app/'
    }
    async with session.get(url, headers=headers, ssl=False) as r:
        r.raise_for_status()
        total = int(r.headers.get('Content-Length', 0))
        async with aiofiles.open(path, 'wb') as f:
            async for chunk in r.content.iter_chunked(CHUNK_SIZE):
                if chunk:
                    await f.write(chunk)
        if total > 0:
            actual = os.path.getsize(path)
            if actual < total:
                raise RuntimeError(f"Incomplete download: {actual}/{total}")

async def upload_and_cleanup(update: Update, path: str, name: str):
    try:
        with open(path, 'rb') as f:
            if name.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.webm')):
                await update.message.reply_video(video=f, supports_streaming=True)
            else:
                await update.message.reply_document(document=f)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

async def process_single_file(update: Update, file_info: dict):
    name = file_info.get('name', 'unknown')
    size = file_info.get('size', 0)  # in bytes
    url = file_info.get('original_url')

    if not url:
        await update.message.reply_text(f"❌ No download URL for: {name}")
        return

    if size > MAX_SIZE:
        await update.message.reply_text(f"❌ Skipped (too large >2GB): {name}")
        return

    # Use RAM disk for temp files (faster!)
    safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in name)
    path = f"/dev/shm/terabox_{hashlib.md5(url.encode()).hexdigest()}_{safe_name}"

    try:
        session = await get_session()
        log.info(f"⬇️ {name} ({size / 1e6:.1f} MB)")
        await download_file(url, path, session)
        log.info(f"✅ Downloaded: {name}")
        await upload_and_cleanup(update, path, name)
    except Exception as e:
        error_msg = f"❌ Failed: {name} – {str(e)[:120]}"
        log.error(error_msg)
        try:
            await update.message.reply_text(error_msg)
        except:
            pass
        # Cleanup on failure
        if os.path.exists(path):
            os.remove(path)

async def process_link_independently(update: Update, link: str):
    async with LINK_SEM:
        try:
            session = await get_session()
            async with session.get(f"{API_BASE}?url={link}", ssl=False) as r:
                if r.status != 200:
                    raise Exception(f"API returned {r.status}")
                data = await r.json()
        except Exception as e:
            await update.message.reply_text(f"❌ Invalid link or API error: {link[:60]}...")
            log.error(f"Link fetch failed: {e}")
            return

        files = data.get('links', [])
        if not files:
            await update.message.reply_text("⚠️ No files found in the link.")
            return

        log.info(f"📦 {len(files)} file(s) from {link}")

        # Process files one by one (Terabox is per-file limited)
        for file_info in files:
            await process_single_file(update, file_info)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or update.message.caption
    if not text:
        return

    links = list(dict.fromkeys(LINK_REGEX.findall(text)))
    if not links:
        return

    user_id = update.effective_user.id
    log.info(f"🔗 {len(links)} link(s) from user {user_id}")

    if len(links) == 1:
        await update.message.reply_text("🚀 Processing your Terabox link...")
    else:
        await update.message.reply_text(f"🚀 Processing {len(links)} Terabox links...")

    for link in links:
        asyncio.create_task(process_link_independently(update, link))

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚡ *Ultra-Fast Terabox Bot*\n\n"
        "📥 Send any Terabox link(s)!\n"
        "🚀 Up to 50 links in parallel\n"
        "📦 Auto-sends videos as streamable\n"
        "⚠️ Max file size: 2GB\n\n"
        "✅ Optimized for speed & stability",
        parse_mode="Markdown"
    )

def main():
    log.info("🚀 Terabox Bot Starting (Optimized for High-Speed Server)...")
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, handle_message))

    async def cleanup(app):
        global SESSION
        if SESSION and not SESSION.closed:
            await SESSION.close()
    app.post_shutdown = cleanup

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
