import asyncio
import re
import os
import aiohttp
import aiofiles
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
import hashlib
from collections import defaultdict

# ===== CONFIG =====
BOT_TOKEN = "8008678561:AAH80tlSuc-tqEYb12eXMfUGfeo7Wz8qUEU"
API_BASE = "https://terabox.itxarshman.workers.dev/api"
MAX_SIZE = 2 * 1024 * 1024 * 1024  # 2GB
DOWNLOADS = 100  # Process 100 files at a time
UPLOADS = 30     # Parallel uploads
CHUNK_SIZE = 262144  # 256KB chunks
# ==================

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("TeraboxBot")
logging.getLogger("httpx").setLevel(logging.WARNING)

# Global resources
SESSION = None
DL_SEM = asyncio.Semaphore(DOWNLOADS)
UP_SEM = asyncio.Semaphore(UPLOADS)
QUEUE = asyncio.Queue()  # File queue
STATS = defaultdict(lambda: {'total': 0, 'processing': 0, 'completed': 0, 'failed': 0})

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
    
    try:
        async with session.head(url, headers=headers, timeout=30) as r:
            size = int(r.headers.get('Content-Length', 0))
            ranges = r.headers.get('Accept-Ranges') == 'bytes'
    except:
        size = 0
        ranges = False
    
    if not ranges or size < 10 * 1024 * 1024:
        async with session.get(url, headers=headers) as r:
            r.raise_for_status()
            async with aiofiles.open(path, 'wb') as f:
                async for chunk in r.content.iter_chunked(CHUNK_SIZE):
                    await f.write(chunk)
        return
    
    chunks = 16
    chunk_size = size // chunks
    
    async def get_chunk(i):
        start = i * chunk_size
        end = start + chunk_size - 1 if i < chunks - 1 else size - 1
        h = {**headers, 'Range': f'bytes={start}-{end}'}
        async with session.get(url, headers=h) as r:
            return await r.read()
    
    parts = await asyncio.gather(*[get_chunk(i) for i in range(chunks)])
    
    async with aiofiles.open(path, 'wb') as f:
        for part in parts:
            await f.write(part)

# ===== Process single file from queue =====
async def process_file_from_queue(update: Update, file_info: dict, user_id: int):
    async with DL_SEM:
        name = file_info['name']
        size_mb = file_info.get('size_mb', 0)
        url = file_info.get('original_url')
        
        STATS[user_id]['processing'] += 1
        
        if size_mb * 1024 * 1024 > MAX_SIZE:
            log.warning(f"❌ Too large: {name}")
            STATS[user_id]['processing'] -= 1
            STATS[user_id]['failed'] += 1
            return
        
        session = await get_session()
        path = f"/tmp/{hashlib.md5(url.encode()).hexdigest()}_{name}"
        
        try:
            log.info(f"⬇️ [{STATS[user_id]['processing']}/{STATS[user_id]['total']}] {name}")
            await download_file(url, path, session)
            log.info(f"✅ {name}")
        except Exception as e:
            log.error(f"❌ {name}: {e}")
            STATS[user_id]['processing'] -= 1
            STATS[user_id]['failed'] += 1
            return
        
        STATS[user_id]['processing'] -= 1
    
    # Upload
    async with UP_SEM:
        try:
            log.info(f"📤 {name}")
            with open(path, 'rb') as f:
                if name.lower().endswith(('.mp4', '.mkv', '.avi', '.mov')):
                    await update.message.reply_video(video=f)
                else:
                    await update.message.reply_document(document=f)
            STATS[user_id]['completed'] += 1
            log.info(f"✨ [{STATS[user_id]['completed']}/{STATS[user_id]['total']}] {name}")
        except Exception as e:
            log.error(f"❌ Upload {name}: {e}")
            STATS[user_id]['failed'] += 1
        finally:
            if os.path.exists(path):
                os.remove(path)
            
            # Send progress update every 10 files or when done
            if STATS[user_id]['completed'] % 10 == 0 or \
               STATS[user_id]['completed'] + STATS[user_id]['failed'] == STATS[user_id]['total']:
                await send_progress(update, user_id)

# ===== Progress update =====
async def send_progress(update: Update, user_id: int):
    stats = STATS[user_id]
    total = stats['total']
    done = stats['completed'] + stats['failed']
    processing = stats['processing']
    completed = stats['completed']
    failed = stats['failed']
    
    if done == total:
        msg = (
            f"✅ *All Done!*\n\n"
            f"✨ Completed: {completed}\n"
            f"❌ Failed: {failed}\n"
            f"📦 Total: {total}"
        )
        # Reset stats
        del STATS[user_id]
    else:
        msg = (
            f"⚡ *Progress Update*\n\n"
            f"✨ Completed: {completed}\n"
            f"⚙️ Processing: {processing}\n"
            f"⏳ Queued: {total - done - processing}\n"
            f"📦 Total: {total}"
        )
    
    try:
        await update.message.reply_text(msg, parse_mode="Markdown")
    except:
        pass

# ===== Queue worker =====
async def queue_worker():
    while True:
        update, file_info, user_id = await QUEUE.get()
        try:
            await process_file_from_queue(update, file_info, user_id)
        except Exception as e:
            log.error(f"Queue worker error: {e}")
        finally:
            QUEUE.task_done()

# ===== Process link =====
async def process_link(update: Update, link: str, user_id: int):
    try:
        session = await get_session()
        async with session.get(f"{API_BASE}?url={link}", timeout=30) as r:
            data = await r.json()
        
        files = data.get('links', [])
        if not files:
            log.warning(f"No files for {link}")
            return
        
        log.info(f"📦 {len(files)} files from {link}")
        
        # Add all files to queue
        for file_info in files:
            STATS[user_id]['total'] += 1
            await QUEUE.put((update, file_info, user_id))
            
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
    
    user_id = update.effective_user.id
    log.info(f"🔗 {len(links)} link(s) from user {user_id}")
    
    # Send instant notification
    queue_size = QUEUE.qsize()
    await update.message.reply_text(
        f"🎯 *Got it!*\n\n"
        f"📥 Processing {len(links)} link(s)\n"
        f"⚡ 100 files at a time\n"
        f"⏳ Current queue: {queue_size} files\n\n"
        f"_Starting downloads..._",
        parse_mode="Markdown"
    )
    
    # Process all links
    for link in links:
        await process_link(update, link, user_id)
    
    # Send initial stats
    await send_progress(update, user_id)

# ===== Commands =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚡ *Ultra-Fast Terabox Bot*\n\n"
        "📥 Send any number of Terabox links!\n"
        "🚀 100 parallel downloads\n"
        "📊 Real-time progress updates\n"
        "⏳ Smart queue system\n\n"
        "⚠️ Max: 2GB per file",
        parse_mode="Markdown"
    )

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    queue_size = QUEUE.qsize()
    
    if user_id in STATS:
        s = STATS[user_id]
        msg = (
            f"📊 *Your Stats*\n\n"
            f"✨ Completed: {s['completed']}\n"
            f"⚙️ Processing: {s['processing']}\n"
            f"❌ Failed: {s['failed']}\n"
            f"📦 Total: {s['total']}\n\n"
            f"⏳ Global Queue: {queue_size}"
        )
    else:
        msg = f"📊 *Status*\n\nNo active downloads\n⏳ Global Queue: {queue_size}"
    
    await update.message.reply_text(msg, parse_mode="Markdown")

# ===== Bot =====
def main():
    log.info("🚀 Ultra-Fast Terabox Bot Starting...")
    
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, handle_message))
    
    async def init(app):
        await get_session()
        # Start queue workers
        for _ in range(DOWNLOADS):
            asyncio.create_task(queue_worker())
        log.info(f"✅ Ready! {DOWNLOADS} workers started")
    
    async def cleanup(app):
        if SESSION and not SESSION.closed:
            await SESSION.close()
    
    app.post_init = init
    app.post_shutdown = cleanup
    app.run_polling()

if __name__ == "__main__":
    main()
