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
CHUNK_SIZE = 1024 * 1024           # 1MB chunks
TIMEOUT = 120

# 🔁 MIRROR FEATURE: Set to your group/channel ID (e.g., -1001234567890) or None to disable
MIRROR_CHAT_ID = None  # 👈 SET THIS TO YOUR CHAT ID IF YOU WANT MIRRORING

# ==================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("TeraboxBot")
logging.getLogger("httpx").setLevel(logging.WARNING)

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

async def upload_and_cleanup(update: Update, path: str, name: str, context: ContextTypes.DEFAULT_TYPE):
    try:
        def send_file(chat_id):
            with open(path, 'rb') as f:
                if name.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.webm', '.flv', '.m4v')):
                    return context.bot.send_video(
                        chat_id=chat_id,
                        video=f,
                        supports_streaming=True,
                        caption=f"📁 {name}" if chat_id != update.effective_chat.id else None
                    )
                else:
                    return context.bot.send_document(
                        chat_id=chat_id,
                        document=f,
                        caption=f"📁 {name}" if chat_id != update.effective_chat.id else None
                    )

        # Send to user
        await send_file(update.effective_chat.id)

        # Mirror to group/channel if enabled
        if MIRROR_CHAT_ID is not None and MIRROR_CHAT_ID != update.effective_chat.id:
            try:
                await send_file(MIRROR_CHAT_ID)
                log.info(f"📤 Mirrored to {MIRROR_CHAT_ID}: {name}")
            except Exception as e:
                log.error(f"❌ Mirror failed for {name}: {e}")

    finally:
        try:
            os.remove(path)
        except OSError:
            pass

async def process_single_file(update: Update, file_info: dict, context: ContextTypes.DEFAULT_TYPE):
    name = file_info.get('name', 'unknown')
    size = file_info.get('size', 0)
    url = file_info.get('original_url')

    if not url:
        await update.message.reply_text(f"❌ No download URL for: {name}")
        return

    if size > MAX_SIZE:
        await update.message.reply_text(f"❌ Skipped (too large >2GB): {name}")
        return

    # Sanitize filename
    safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
    safe_name = safe_name[:200]  # Telegram filename limit
    path = f"/dev/shm/terabox_{hashlib.md5(url.encode()).hexdigest()}_{safe_name}"

    try:
        session = await get_session()
        size_mb = size / (1024**2)
        log.info(f"⬇️ {name} ({size_mb:.1f} MB)")
        await download_file(url, path, session)
        log.info(f"✅ Downloaded: {name}")
        await upload_and_cleanup(update, path, name, context)
    except Exception as e:
        error_msg = f"❌ Failed: {name} – {str(e)[:120]}"
        log.error(error_msg)
        try:
            await update.message.reply_text(error_msg)
        except:
            pass
        if os.path.exists(path):
            try:
                os.remove(path)
            except:
                pass

async def process_link_independently(update: Update, link: str, context: ContextTypes.DEFAULT_TYPE):
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

        # Show preview for single file
        if len(files) == 1:
            f = files[0]
            size_mb = f.get('size', 0) / (1024**2)
            name = f.get('name', 'unknown')
            await update.message.reply_text(
                f"📥 *File detected*\n"
                f"📁 Name: {name}\n"
                f"📦 Size: {size_mb:.1f} MB\n"
                f"⏳ Starting download...",
                parse_mode="Markdown"
            )

        log.info(f"📦 {len(files)} file(s) from {link}")
        for file_info in files:
            await process_single_file(update, file_info, context)

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
        asyncio.create_task(process_link_independently(update, link, context))

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mirror_info = f"\n🔁 Also mirrored to a channel!" if MIRROR_CHAT_ID else ""
    await update.message.reply_text(
        "⚡ *Ultra-Fast Terabox Bot*\n\n"
        "📥 Send any Terabox link i will download",
        parse_mode="Markdown"
    )

def main():
    log.info("🚀 Terabox Bot Starting (with Mirror Support)...")
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
