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
DOWNLOADS = 100  # Parallel downloads
UPLOADS = 30     # Parallel uploads
CHUNK_SIZE = 262144  # 256KB chunks (ultra fast)
# ==================

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("TeraboxBot")
logging.getLogger("httpx").setLevel(logging.WARNING)

# Global resources
SESSION = None
DL_SEM = asyncio.Semaphore(DOWNLOADS)
UP_SEM = asyncio.Semaphore(UPLOADS)

LINK_REGEX = re.compile(
    r"https?://[^\s]*?(?:terabox|teraboxapp|teraboxshare|nephobox|1024tera|1024terabox|freeterabox|terasharefile|terasharelink|mirrobox|momerybox|teraboxlink)\.[^\s]+",
    re.IGNORECASE
)

# ===== Session =====
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
            ttl_dns_cache=300
        )
        SESSION = aiohttp.ClientSession(connector=connector)
    return SESSION

# ===== Download with parallel chunks =====
async def download_file(url: str, path: str, session: aiohttp.ClientSession):
    headers = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.terabox.app/'}
    
    # Get file size
    try:
        async with session.head(url, headers=headers, timeout=30) as r:
            size = int(r.headers.get('Content-Length', 0))
            ranges = r.headers.get('Accept-Ranges') == 'bytes'
    except:
        size = 0
        ranges = False
    
    # Small file or no range support
    if not ranges or size < 10 * 1024 * 1024:
        async with session.get(url, headers=headers) as r:
            r.raise_for_status()
            async with aiofiles.open(path, 'wb') as f:
                async for chunk in r.content.iter_chunked(CHUNK_SIZE):
                    await f.write(chunk)
        return
    
    # Large file: 16 parallel chunks
    chunks = 16
    chunk_size = size // chunks
    
    async def get_chunk(i):
        start = i * chunk_size
        end = start + chunk_size - 1 if i < chunks - 1 else size - 1
        h = {**headers, 'Range': f'bytes={start}-{end}'}
        async with session.get(url, headers=h) as r:
            return await r.read()
    
    parts = await asyncio.gather(*[get_chunk(i) for i in range(chunks)])
    
    # Write all at once
    async with aiofiles.open(path, 'wb') as f:
        for part in parts:
            await f.write(part)

# ===== Process single file =====
async def process_file(update: Update, file_info: dict):
    async with DL_SEM:
        name = file_info['name']
        size_mb = file_info.get('size_mb', 0)
        url = file_info.get('original_url')
        
        if size_mb * 1024 * 1024 > MAX_SIZE:
            log.warning(f"❌ Too large: {name}")
            return
        
        session = await get_session()
        path = f"/tmp/{hashlib.md5(url.encode()).hexdigest()}_{name}"
        
        try:
            log.info(f"⬇️ {name}")
            await download_file(url, path, session)
            log.info(f"✅ {name}")
        except Exception as e:
            log.error(f"❌ Download failed {name}: {e}")
            return
    
    # Upload (outside download semaphore)
    async with UP_SEM:
        try:
            log.info(f"📤 {name}")
            with open(path, 'rb') as f:
                if name.lower().endswith(('.mp4', '.mkv', '.avi', '.mov')):
                    await update.message.reply_video(video=f)
                else:
                    await update.message.reply_document(document=f)
            log.info(f"✨ {name}")
        except Exception as e:
            log.error(f"❌ Upload failed {name}: {e}")
        finally:
            if os.path.exists(path):
                os.remove(path)

# ===== Process link =====
async def process_link(update: Update, link: str):
    try:
        session = await get_session()
        async with session.get(f"{API_BASE}?url={link}", timeout=30) as r:
            data = await r.json()
        
        files = data.get('links', [])
        if not files:
            log.warning(f"No files for {link}")
            return
        
        log.info(f"📦 {len(files)} files from {link}")
        
        # Start all files immediately
        for file_info in files:
            asyncio.create_task(process_file(update, file_info))
            
    except Exception as e:
        log.error(f"❌ Link failed {link}: {e}")

# ===== Message Handler =====
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or update.message.caption
    if not text:
        return
    
    links = list(dict.fromkeys(LINK_REGEX.findall(text)))
    if not links:
        return
    
    log.info(f"🔗 {len(links)} link(s) from user {update.effective_user.id}")
    
    # Process all links immediately in background
    for link in links:
        asyncio.create_task(process_link(update, link))

# ===== Commands =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚡ *Ultra-Fast Terabox Bot*\n\n"
        "📥 Send Terabox links, get files instantly!\n",
        parse_mode="Markdown"
    )

# ===== Bot =====
def main():
    log.info("🚀 Ultra-Fast Terabox Bot Starting...")
    
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, handle_message))
    
    async def init(app):
        await get_session()
    
    async def cleanup(app):
        if SESSION and not SESSION.closed:
            await SESSION.close()
    
    app.post_init = init
    app.post_shutdown = cleanup
    app.run_polling()

if __name__ == "__main__":
    main()
