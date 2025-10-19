import asyncio
import re
import os
import aiohttp
import aiofiles
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
import hashlib
from pymongo import MongoClient
from datetime import datetime

# ===== CONFIG =====
BOT_TOKEN = "8008678561:AAH80tlSuc-tqEYb12eXMfUGfeo7Wz8qUEU"
API_BASE = "https://terabox.itxarshman.workers.dev/api"
MAX_SIZE = 50 * 1024 * 1024           # 50MB (Telegram API limit for videos)
MAX_CONCURRENT_LINKS = 50          # Max links processed at once
CHUNK_SIZE = 1024 * 1024           # 1MB chunks (optimal for high-speed I/O)
TIMEOUT = 120                      # Seconds
# ==================

# ===== MONGODB CONFIG =====
MONGO_URI = "mongodb+srv://irexanon:xUf7PCf9cvMHy8g6@rexdb.d9rwo.mongodb.net/?retryWrites=true&w=majority&appName=RexDB"
DB_NAME = "terabox_bot"
DOWNLOADS_COLLECTION = "downloads"
FAILED_LINKS_COLLECTION = "failed_links"
OVERSIZED_LINKS_COLLECTION = "oversized_links"
USER_SETTINGS_COLLECTION = "user_settings"
# ==========================

# ===== BROADCAST CONFIG =====
BROADCAST_CHATS = [
    -1002780909369,
]
# ============================

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


class MongoDBManager:
    def __init__(self):
        self.client = MongoClient(MONGO_URI)
        self.db = self.client[DB_NAME]
        self.downloads = self.db[DOWNLOADS_COLLECTION]
        self.failed_links = self.db[FAILED_LINKS_COLLECTION]
        self.oversized_links = self.db[OVERSIZED_LINKS_COLLECTION]
        self.user_settings = self.db[USER_SETTINGS_COLLECTION]
        self._create_indexes()

    def _create_indexes(self):
        """Create indexes for faster queries"""
        self.downloads.create_index("timestamp")
        self.downloads.create_index("user_id")
        self.failed_links.create_index("timestamp")
        self.failed_links.create_index("user_id")
        self.failed_links.create_index("retry_count")
        self.oversized_links.create_index("timestamp")
        self.oversized_links.create_index("user_id")
        self.user_settings.create_index("user_id")

    def record_success(self, user_id: int, link: str, file_name: str, file_size: int, video_link: str = None):
        """Record successful download and remove from failed links"""
        try:
            record = {
                "user_id": user_id,
                "original_link": link,
                "file_name": file_name,
                "file_size": file_size,
                "video_link": video_link,
                "timestamp": datetime.utcnow(),
                "status": "success"
            }
            result = self.downloads.insert_one(record)
            log.info(f"✅ Recorded success: {file_name}")
            
            # Remove from failed links if it exists
            self.failed_links.delete_one({
                "user_id": user_id,
                "original_link": link
            })
            log.info(f"🗑️ Removed from failed links: {link}")
            
            return result.inserted_id
        except Exception as e:
            log.error(f"❌ Failed to record success: {e}")

    def record_failure(self, user_id: int, link: str, error: str):
        """Record failed download"""
        try:
            existing = self.failed_links.find_one({
                "original_link": link,
                "user_id": user_id
            })
            
            if existing:
                self.failed_links.update_one(
                    {"_id": existing["_id"]},
                    {
                        "$inc": {"retry_count": 1},
                        "$set": {"last_error": error, "last_attempt": datetime.utcnow()}
                    }
                )
            else:
                record = {
                    "user_id": user_id,
                    "original_link": link,
                    "error": error,
                    "last_error": error,
                    "retry_count": 1,
                    "timestamp": datetime.utcnow(),
                    "last_attempt": datetime.utcnow(),
                    "status": "failed"
                }
                self.failed_links.insert_one(record)
            
            log.info(f"❌ Recorded failure: {link}")
        except Exception as e:
            log.error(f"❌ Failed to record failure: {e}")

    def record_oversized(self, user_id: int, link: str, file_name: str, file_size: int):
        """Record oversized file"""
        try:
            existing = self.oversized_links.find_one({
                "original_link": link,
                "user_id": user_id,
                "file_name": file_name
            })
            
            if not existing:
                record = {
                    "user_id": user_id,
                    "original_link": link,
                    "file_name": file_name,
                    "file_size": file_size,
                    "timestamp": datetime.utcnow(),
                    "status": "oversized"
                }
                self.oversized_links.insert_one(record)
            
            log.info(f"⚠️ Recorded oversized: {file_name} ({file_size / 1e6:.1f} MB)")
        except Exception as e:
            log.error(f"❌ Failed to record oversized: {e}")

    def get_stats(self, user_id: int = None):
        """Get download statistics"""
        try:
            query = {"user_id": user_id} if user_id else {}
            
            total_success = self.downloads.count_documents(query)
            total_failed = self.failed_links.count_documents(query)
            
            total_size = 0
            for doc in self.downloads.find(query):
                total_size += doc.get("file_size", 0)
            
            return {
                "total_success": total_success,
                "total_failed": total_failed,
                "total_size_gb": round(total_size / (1024**3), 2),
                "total_size_bytes": total_size
            }
        except Exception as e:
            log.error(f"❌ Failed to get stats: {e}")
            return None

    def get_failed_links(self, user_id: int = None, limit: int = 10):
        """Get list of failed links"""
        try:
            query = {"user_id": user_id} if user_id else {}
            failed = list(self.failed_links.find(query).sort("last_attempt", -1).limit(limit))
            return failed
        except Exception as e:
            log.error(f"❌ Failed to get failed links: {e}")
            return []

    def get_oversized_links(self, user_id: int = None, limit: int = 10):
        """Get list of oversized links"""
        try:
            query = {"user_id": user_id} if user_id else {}
            oversized = list(self.oversized_links.find(query).sort("timestamp", -1).limit(limit))
            return oversized
        except Exception as e:
            log.error(f"❌ Failed to get oversized links: {e}")
            return []

    def retry_failed_link(self, link_id: str):
        """Mark failed link for retry"""
        try:
            from bson.objectid import ObjectId
            self.failed_links.update_one(
                {"_id": ObjectId(link_id)},
                {"$set": {"retry_requested": True, "retry_requested_at": datetime.utcnow()}}
            )
            log.info(f"🔄 Marked for retry: {link_id}")
            return True
        except Exception as e:
            log.error(f"❌ Failed to mark retry: {e}")
            return False

    def get_user_setting(self, user_id: int, setting: str, default=False):
        """Get user setting"""
        try:
            doc = self.user_settings.find_one({"user_id": user_id})
            if doc:
                return doc.get(setting, default)
            return default
        except Exception as e:
            log.error(f"❌ Failed to get user setting: {e}")
            return default

    def set_user_setting(self, user_id: int, setting: str, value):
        """Set user setting"""
        try:
            self.user_settings.update_one(
                {"user_id": user_id},
                {"$set": {setting: value, "updated_at": datetime.utcnow()}},
                upsert=True
            )
            log.info(f"✅ Updated setting {setting} for user {user_id}: {value}")
        except Exception as e:
            log.error(f"❌ Failed to set user setting: {e}")

    def check_duplicate_download(self, user_id: int, link: str, file_name: str):
        """Check if file already downloaded"""
        try:
            existing = self.downloads.find_one({
                "user_id": user_id,
                "original_link": link,
                "file_name": file_name
            })
            return existing is not None
        except Exception as e:
            log.error(f"❌ Failed to check duplicate: {e}")
            return False


# Initialize MongoDB
try:
    db_manager = MongoDBManager()
    log.info("✅ MongoDB connected")
except Exception as e:
    log.error(f"❌ MongoDB connection failed: {e}")
    db_manager = None


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
import aiohttp
import aiofiles
import os
import asyncio
import hashlib
import logging


async def download_file(url: str, path: str, session: aiohttp.ClientSession, max_retries: int = 3):
    """
    Resumable, hash-verified downloader for large files.
    Supports retries, content-length validation, and integrity check.
    """
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': 'https://www.terabox.app/'
    }

    temp_path = path + ".part"

    for attempt in range(1, max_retries + 1):
        try:
            # Resume if file partially exists
            resume_pos = 0
            if os.path.exists(temp_path):
                resume_pos = os.path.getsize(temp_path)
                headers['Range'] = f"bytes={resume_pos}-"

            async with session.get(url, headers=headers, ssl=False) as r:
                if r.status in (200, 206):  # 206 = partial content
                    total = int(r.headers.get('Content-Length', 0)) + resume_pos
                    hasher = hashlib.md5()

                    async with aiofiles.open(temp_path, 'ab') as f:
                        downloaded = resume_pos
                        async for chunk in r.content.iter_chunked(1024 * 1024):  # 1MB
                            if chunk:
                                await f.write(chunk)
                                hasher.update(chunk)
                                downloaded += len(chunk)

                    # Check if full file downloaded
                    actual_size = os.path.getsize(temp_path)
                    if total > 0 and actual_size < total:
                        raise aiohttp.ContentLengthError(
                            f"Incomplete download ({actual_size}/{total})"
                        )

                    # Finalize and rename
                    os.replace(temp_path, path)
                    log_hash = hasher.hexdigest()[:8]
                    logging.info(f"✅ Download complete ({actual_size/1e6:.1f} MB, md5={log_hash})")
                    return

                else:
                    raise RuntimeError(f"Bad status {r.status}")

        except (aiohttp.ClientPayloadError, aiohttp.ContentLengthError, asyncio.TimeoutError) as e:
            logging.warning(f"⚠️ Retry {attempt}/{max_retries} for {os.path.basename(path)}: {e}")
            await asyncio.sleep(2 * attempt)  # exponential backoff
            continue

        except Exception as e:
            logging.error(f"❌ Download error ({attempt}/{max_retries}): {e}")
            await asyncio.sleep(2 * attempt)
            continue

    # If still incomplete after retries
    if os.path.exists(temp_path):
        os.remove(temp_path)
    raise RuntimeError(f"Failed after {max_retries} retries — download incomplete.")

async def broadcast_video(file_path: str, video_name: str, update: Update):
    """Broadcasts downloaded video to all preset chats"""
    if not BROADCAST_CHATS:
        log.warning("No broadcast chats configured")
        return

    if not video_name.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.webm')):
        return

    broadcast_count = 0
    for chat_id in BROADCAST_CHATS:
        try:
            with open(file_path, 'rb') as f:
                await update.get_bot().send_video(
                    chat_id=chat_id,
                    video=f,
                    supports_streaming=True
                )
            log.info(f"📤 Broadcasted {video_name} to chat {chat_id}")
            broadcast_count += 1
        except Exception as e:
            log.error(f"❌ Broadcast failed for chat {chat_id}: {str(e)[:100]}")

    if broadcast_count > 0:
        log.info(f"✅ Broadcast complete: {broadcast_count}/{len(BROADCAST_CHATS)} chats")


async def upload_and_cleanup(update: Update, path: str, name: str, link: str, size: int):
    try:
        with open(path, 'rb') as f:
            is_video = name.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.webm'))
            if is_video:
                await update.message.reply_video(video=f, supports_streaming=True)
            else:
                await update.message.reply_document(document=f)
        
        # Record success in MongoDB
        if db_manager:
            db_manager.record_success(
                user_id=update.effective_user.id,
                link=link,
                file_name=name,
                file_size=size,
                video_link=path
            )
        
        # Broadcast video to other chats
        if is_video:
            asyncio.create_task(broadcast_video(path, name, update))
    
    finally:
        await asyncio.sleep(2)
        try:
            os.remove(path)
        except OSError:
            pass


async def process_single_file(update: Update, file_info: dict, original_link: str):
    name = file_info.get('name', 'unknown')
    size_mb = file_info.get('size_mb', 0)
    size_bytes = int(size_mb * 1024 * 1024)
    url = file_info.get('original_url')

    if not url:
        await update.message.reply_text(f"❌ No download URL for: {name}")
        if db_manager:
            db_manager.record_failure(update.effective_user.id, original_link, "No download URL")
        return

    if size_bytes > MAX_SIZE:
        await update.message.reply_text(f"⚠️ File too large ({size_mb:.1f} MB): {name}")
        if db_manager:
            db_manager.record_oversized(update.effective_user.id, original_link, name, size_bytes)
        log.warning(f"⚠️ Oversized file skipped: {name} ({size_mb:.1f} MB)")
        return

    safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in name)
    path = f"/tmp/terabox_{hashlib.md5(url.encode()).hexdigest()}_{safe_name}"  # Use /tmp/ for disk storage

    try:
        session = await get_session()
        log.info(f"⬇️ {name} ({size_mb:.1f} MB)")
        await download_file(url, path, session)
        log.info(f"✅ Downloaded: {name}")
        await upload_and_cleanup(update, path, name, original_link, size_bytes)
    except Exception as e:
        error_msg = f"❌ Failed: {name} – {str(e)[:120]}"
        log.error(error_msg)
        if db_manager:
            db_manager.record_failure(update.effective_user.id, original_link, str(e)[:200])
        await update.message.reply_text(error_msg)
    finally:
        if os.path.exists(path):
            try:
                os.remove(path)
                log.info(f"🗑️ Cleaned up: {path}")
            except OSError as e:
                log.error(f"❌ Failed to clean up {path}: {e}")


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
            if db_manager:
                db_manager.record_failure(update.effective_user.id, link, str(e)[:200])
            return

        files = data.get('links', [])
        if not files:
            await update.message.reply_text("⚠️ No files found in the link.")
            if db_manager:
                db_manager.record_failure(update.effective_user.id, link, "No files found")
            return

        log.info(f"📦 {len(files)} file(s) from {link}")

        # Process files one by one (Terabox is per-file limited)
        for file_info in files:
            await process_single_file(update, file_info, link)


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


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show download statistics"""
    if not db_manager:
        await update.message.reply_text("❌ Database not connected")
        return

    user_id = update.effective_user.id
    stats = db_manager.get_stats(user_id)
    allow_duplicates = db_manager.get_user_setting(user_id, "allow_duplicates", True)
    
    if not stats:
        await update.message.reply_text("❌ Could not retrieve stats")
        return

    dup_status = "✅ Allowed" if allow_duplicates else "❌ Blocked"
    message = (
        f"📊 *Your Download Stats*\n\n"
        f"✅ Successful: `{stats['total_success']}`\n"
        f"❌ Failed: `{stats['total_failed']}`\n"
        f"🔄 Duplicates: `{dup_status}`\n"
    )
    await update.message.reply_text(message, parse_mode="Markdown")


async def failed_links_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show failed links with optional count parameter"""
    if not db_manager:
        await update.message.reply_text("❌ Database not connected")
        return

    user_id = update.effective_user.id
    
    # Get count from command args, default to 10
    limit = 10
    if context.args and context.args[0].isdigit():
        limit = min(int(context.args[0]), 100)  # Max 100 to prevent spam
    
    failed = db_manager.get_failed_links(user_id, limit=limit)
    
    if not failed:
        await update.message.reply_text("✅ No failed links!")
        return

    message = f"❌ *Failed Links (Last {len(failed)})*\n\n"
    for idx, item in enumerate(failed, 1):
        retries = item.get('retry_count', 1)
        link_preview = item['original_link'][:50] + "..." if len(item['original_link']) > 50 else item['original_link']
        message += f"{idx}. `{link_preview}`\n   Retries: {retries}\n"

    await update.message.reply_text(message, parse_mode="Markdown")


async def oversized_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show oversized links with file sizes"""
    if not db_manager:
        await update.message.reply_text("❌ Database not connected")
        return

    user_id = update.effective_user.id
    
    # Get count from command args, default to 10
    limit = 10
    if context.args and context.args[0].isdigit():
        limit = min(int(context.args[0]), 100)  # Max 100 to prevent spam
    
    oversized = db_manager.get_oversized_links(user_id, limit=limit)
    
    if not oversized:
        await update.message.reply_text("✅ No oversized files!")
        return

    message = f"⚠️ *Oversized Files (Last {len(oversized)})*\n\n"
    for idx, item in enumerate(oversized, 1):
        file_size_mb = item.get('file_size', 0) / 1e6
        file_name = item.get('file_name', 'unknown')
        link_preview = item['original_link'][:45] + "..." if len(item['original_link']) > 45 else item['original_link']
        message += f"{idx}. `{file_name}`\n   Size: `{file_size_mb:.1f} MB`\n   Link: `{link_preview}`\n\n"

    await update.message.reply_text(message, parse_mode="Markdown")


async def retry_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Retry failed links"""
    if not db_manager:
        await update.message.reply_text("❌ Database not connected")
        return

    user_id = update.effective_user.id
    failed = db_manager.get_failed_links(user_id, limit=50)
    
    if not failed:
        await update.message.reply_text("✅ No failed links to retry!")
        return

    retry_count = 0
    for item in failed:
        link = item['original_link']
        db_manager.retry_failed_link(str(item['_id']))
        asyncio.create_task(process_link_independently(update, link))
        retry_count += 1

    await update.message.reply_text(f"🔄 Retrying {retry_count} failed link(s)...", parse_mode="Markdown")


async def duplicate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle duplicate download detection"""
    if not db_manager:
        await update.message.reply_text("❌ Database not connected")
        return

    user_id = update.effective_user.id
    current_status = db_manager.get_user_setting(user_id, "allow_duplicates", True)
    new_status = not current_status
    
    db_manager.set_user_setting(user_id, "allow_duplicates", new_status)
    
    status_text = "✅ Allowed" if new_status else "❌ Blocked"
    message = (
        f"🔄 *Duplicate Downloads*\n\n"
        f"Status: {status_text}\n\n"
        f"When blocked: Won't download files you already have\n"
        f"When allowed: Downloads everything (default)\n"
    )
    await update.message.reply_text(message, parse_mode="Markdown")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚡ *Ultra-Fast Terabox Bot*\n\n"
        "📥 Send any Terabox link(s)!\n",
        parse_mode="Markdown"
    )


def main():
    log.info("🚀 Terabox Bot Starting (Optimized for High-Speed Server)...")
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("failed", failed_links_command))
    app.add_handler(CommandHandler("oversized", oversized_command))
    app.add_handler(CommandHandler("retry", retry_command))
    app.add_handler(CommandHandler("duplicate", duplicate_command))
    app.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, handle_message))

    async def set_commands(app):
        """Set up command menu"""
        from telegram import BotCommand
        commands = [
            BotCommand("start", "Start the bot"),
            BotCommand("stats", "View your download stats"),
            BotCommand("failed", "Show failed links (optional: /failed 20)"),
            BotCommand("oversized", "Show oversized files (optional: /oversized 20)"),
            BotCommand("retry", "Retry all failed links"),
            BotCommand("duplicate", "Enable/Disable duplicate downloads"),
        ]
        await app.bot.set_my_commands(commands)

    async def cleanup(app):
        global SESSION
        if SESSION and not SESSION.closed:
            await SESSION.close()
    
    app.post_init = set_commands
    app.post_shutdown = cleanup

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
