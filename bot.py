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
CONCURRENT_DOWNLOADS = 100  # 100 parallel downloads (your server can handle it!)
CONCURRENT_UPLOADS = 30     # Telegram rate limit is the bottleneck
RETRY_ATTEMPTS = 3          # Reduced retries (faster failure)
RETRY_DELAY = 1             # Faster retry
CHUNK_SIZE = 131072         # 128KB chunks (2x faster on fast network)
MAX_CONNECTIONS = 200       # Increased for 100 concurrent
DOWNLOAD_TIMEOUT = 600      # 10 minutes per file
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

# Global session for reuse across messages
GLOBAL_SESSION = None

async def get_or_create_session():
    """Get or create a global session"""
    global GLOBAL_SESSION
    if GLOBAL_SESSION is None or GLOBAL_SESSION.closed:
        import ssl
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        
        connector = aiohttp.TCPConnector(
            limit=MAX_CONNECTIONS, 
            limit_per_host=50,
            ssl=ssl_context,
            force_close=False,
            enable_cleanup_closed=True,
            ttl_dns_cache=300,
            keepalive_timeout=30
        )
        GLOBAL_SESSION = aiohttp.ClientSession(
            connector=connector, 
            timeout=aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT)
        )
    return GLOBAL_SESSION

semaphore = asyncio.Semaphore(CONCURRENT_DOWNLOADS)
upload_semaphore = asyncio.Semaphore(CONCURRENT_UPLOADS)  # Separate semaphore for uploads

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
            timeout = aiohttp.ClientTimeout(total=60, connect=30)
            async with session.get(f"{API_BASE}?url={link}", timeout=timeout, allow_redirects=True) as resp:
                resp.raise_for_status()
                data = await resp.json()
            if not data.get("links"):
                raise ValueError("API returned no links")
            return data
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            last_exc = e
            log.warning(f"API fetch failed for {link}: {e}")
            if attempt < RETRY_ATTEMPTS - 1:
                await asyncio.sleep(RETRY_DELAY * (attempt + 1))  # Exponential backoff
            else:
                raise last_exc

# ===== Helper: download URL to file with parallel chunks =====
async def download_chunk(session: aiohttp.ClientSession, url: str, start: int, end: int, file_path: str, chunk_index: int):
    """Download a specific chunk of the file"""
    headers = {
        'Range': f'bytes={start}-{end}',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': 'https://www.terabox.app/',
    }
    timeout = aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT, connect=30)
    
    async with session.get(url, headers=headers, timeout=timeout, allow_redirects=True) as r:
        if r.status not in (200, 206):
            raise Exception(f"Failed to download chunk {chunk_index}")
        
        chunk_file = f"{file_path}.part{chunk_index}"
        async with aiofiles.open(chunk_file, "wb") as f:
            async for chunk in r.content.iter_chunked(CHUNK_SIZE):
                if chunk:
                    await f.write(chunk)
    return chunk_file

async def try_download_url(session: aiohttp.ClientSession, url: str, file_path: str):
    """Download file with parallel chunks for maximum speed"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': 'https://www.terabox.app/',
    }
    timeout = aiohttp.ClientTimeout(total=60, connect=30)
    
    # Get file size to enable parallel downloads
    try:
        async with session.head(url, headers=headers, timeout=timeout, allow_redirects=True) as r:
            file_size = int(r.headers.get('Content-Length', 0))
            accepts_ranges = r.headers.get('Accept-Ranges') == 'bytes'
    except:
        # If HEAD fails, fallback to GET
        file_size = 0
        accepts_ranges = False
    
    # If server doesn't support ranges or file is small, download normally
    if not accepts_ranges or file_size < 10 * 1024 * 1024:  # Less than 10MB
        timeout = aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT, connect=30)
        async with session.get(url, headers=headers, timeout=timeout, allow_redirects=True) as r:
            r.raise_for_status()
            async with aiofiles.open(file_path, "wb") as f:
                async for chunk in r.content.iter_chunked(CHUNK_SIZE):
                    if chunk:
                        await f.write(chunk)
        return
    
    # Parallel download for large files (16 chunks on your powerful server!)
    num_chunks = 16  # Increased from 8 to 16 for your hardware
    chunk_size = file_size // num_chunks
    
    tasks = []
    for i in range(num_chunks):
        start = i * chunk_size
        end = start + chunk_size - 1 if i < num_chunks - 1 else file_size - 1
        tasks.append(download_chunk(session, url, start, end, file_path, i))
    
    # Download all chunks in parallel
    chunk_files = await asyncio.gather(*tasks)
    
    # Merge chunks into final file
    async with aiofiles.open(file_path, "wb") as final_file:
        for chunk_file in chunk_files:
            async with aiofiles.open(chunk_file, "rb") as cf:
                while True:
                    data = await cf.read(CHUNK_SIZE)
                    if not data:
                        break
                    await final_file.write(data)
            os.remove(chunk_file)  # Clean up chunk file

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

    # Download with semaphore (limits concurrent downloads)
    async with semaphore:
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
                    await asyncio.sleep(RETRY_DELAY * (attempt + 1))
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
    
    # Upload to Telegram (with separate upload semaphore)
    async with upload_semaphore:
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
    # Handle both text messages and captions
    text = None
    if update.message.text:
        text = update.message.text
    elif update.message.caption:
        text = update.message.caption
    
    if not text:
        return

    links = extract_terabox_links(text)
    if not links:
        return

    log.info(f"🧾 User {update.effective_user.id} sent {len(links)} link(s)")
    
    # No status message - just process in background
    session = await get_or_create_session()
    
    # Process each link immediately in background
    for link in links:
        asyncio.create_task(process_link(update, link, session))

async def process_link(update: Update, link: str, session: aiohttp.ClientSession):
    """Process a single link in background"""
    try:
        log.info(f"🔗 Processing link: {link}")
        data = await fetch_api_info(session, link)
        
        # Process all files from this link
        for file_info in data.get("links", []):
            asyncio.create_task(download_file(update, link, file_info, session))
            
    except Exception as e:
        log.error(f"❌ Failed to process {link}: {e}")
        await update.message.reply_text(f"❌ Failed to process link")

# ===== Bot Launcher =====
def run_bot():
    log.info("🚀 Starting Terabox Telegram Bot...")
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handle_message
    ))
    app.add_handler(MessageHandler(
        filters.CAPTION & ~filters.COMMAND,
        handle_message
    ))

    async def post_init(app):
        await setup_bot_commands(app)

    app.post_init = post_init
    app.run_polling()

if __name__ == "__main__":
    run_bot()
