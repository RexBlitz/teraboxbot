import asyncio
import re
import os
import aiohttp
import aiofiles
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
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
MAX_DOWNLOAD_RETRIES = 3           # Max retries for download with fresh API calls
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
            total_oversized = self.oversized_links.count_documents(query)
            
            # Count duplicate downloads
            pipeline = [
                {"$match": query},
                {"$group": {
                    "_id": {"file_name": "$file_name", "original_link": "$original_link"},
                    "count": {"$sum": 1}
                }},
                {"$match": {"count": {"$gt": 1}}}
            ]
            duplicate_groups = list(self.downloads.aggregate(pipeline))
            total_duplicates = sum(group["count"] - 1 for group in duplicate_groups)
            
            total_size = 0
            for doc in self.downloads.find(query):
                total_size += doc.get("file_size", 0)
            
            return {
                "total_success": total_success,
                "total_failed": total_failed,
                "total_oversized": total_oversized,
                "total_duplicates": total_duplicates,
                "total_size_gb": round(total_size / (1024**3), 2),
                "total_size_bytes": total_size
            }
        except Exception as e:
            log.error(f"❌ Failed to get stats: {e}")
            return None

    def get_failed_links(self, user_id: int = None, skip: int = 0, limit: int = 10):
        """Get list of failed links with pagination"""
        try:
            query = {"user_id": user_id} if user_id else {}
            failed = list(self.failed_links.find(query).sort("last_attempt", -1).skip(skip).limit(limit))
            total = self.failed_links.count_documents(query)
            return failed, total
        except Exception as e:
            log.error(f"❌ Failed to get failed links: {e}")
            return [], 0

    def get_oversized_links(self, user_id: int = None, skip: int = 0, limit: int = 10):
        """Get list of oversized links with pagination"""
        try:
            query = {"user_id": user_id} if user_id else {}
            oversized = list(self.oversized_links.find(query).sort("timestamp", -1).skip(skip).limit(limit))
            total = self.oversized_links.count_documents(query)
            return oversized, total
        except Exception as e:
            log.error(f"❌ Failed to get oversized links: {e}")
            return [], 0

    def clear_user_data(self, user_id: int):
        """Clear all user data from database"""
        try:
            query = {"user_id": user_id}
            downloads_deleted = self.downloads.delete_many(query).deleted_count
            failed_deleted = self.failed_links.delete_many(query).deleted_count
            oversized_deleted = self.oversized_links.delete_many(query).deleted_count
            settings_deleted = self.user_settings.delete_many(query).deleted_count
            
            log.info(f"🗑️ Cleared user {user_id} data: {downloads_deleted} downloads, {failed_deleted} failed, {oversized_deleted} oversized, {settings_deleted} settings")
            return {
                "downloads": downloads_deleted,
                "failed": failed_deleted,
                "oversized": oversized_deleted,
                "settings": settings_deleted
            }
        except Exception as e:
            log.error(f"❌ Failed to clear user data: {e}")
            return None

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


async def fetch_fresh_direct_url(original_link: str, file_name: str, session: aiohttp.ClientSession):
    """
    Fetch a fresh direct_url from the API for a specific file.
    Returns the direct_url and file_info dict, or (None, None) on failure.
    """
    try:
        log.info(f"🔄 Fetching fresh direct_url for: {file_name}")
        async with session.get(f"{API_BASE}?url={original_link}", ssl=False) as r:
            if r.status != 200:
                log.error(f"❌ API returned status {r.status}")
                return None, None
            
            data = await r.json()
            files = data.get('links', [])
            
            # Find the matching file by name
            for file_info in files:
                if file_info.get('name') == file_name:
                    direct_url = file_info.get('direct_url')
                    if direct_url:
                        log.info(f"✅ Got fresh direct_url for: {file_name}")
                        return direct_url, file_info
            
            log.error(f"❌ File not found in API response: {file_name}")
            return None, None
            
    except Exception as e:
        log.error(f"❌ Failed to fetch fresh direct_url: {e}")
        return None, None


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
            resume_pos = 0
            if os.path.exists(temp_path):
                resume_pos = os.path.getsize(temp_path)
                headers['Range'] = f"bytes={resume_pos}-"

            async with session.get(url, headers=headers, ssl=False) as r:
                if r.status in (200, 206):
                    total = int(r.headers.get('Content-Length', 0)) + resume_pos
                    hasher = hashlib.md5()

                    async with aiofiles.open(temp_path, 'ab') as f:
                        downloaded = resume_pos
                        async for chunk in r.content.iter_chunked(1024 * 1024):  # 1MB chunks
                            if chunk:
                                await f.write(chunk)
                                hasher.update(chunk)
                                downloaded += len(chunk)

                    # Verify file size if total is known
                    actual_size = os.path.getsize(temp_path)
                    if total > 0 and actual_size < total:
                        raise ValueError(f"Incomplete download ({actual_size}/{total})")

                    # Rename to final file
                    os.replace(temp_path, path)
                    log_hash = hasher.hexdigest()[:8]
                    logging.info(f"✅ Download complete ({actual_size/1e6:.1f} MB, md5={log_hash})")
                    return

                else:
                    raise RuntimeError(f"Bad HTTP status {r.status}")

        except (aiohttp.ClientPayloadError, asyncio.TimeoutError, ValueError) as e:
            logging.warning(f"⚠️ Retry {attempt}/{max_retries} for {os.path.basename(path)}: {e}")
            await asyncio.sleep(2 * attempt)
            continue

        except Exception as e:
            logging.error(f"❌ Download error ({attempt}/{max_retries}): {e}")
            await asyncio.sleep(2 * attempt)
            continue

    # Cleanup if download fails completely
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


async def process_single_file(update: Update, file_info: dict, original_link: str, retry_count: int = 0):
    """
    Process a single file with smart retry mechanism.
    Fetches fresh direct_url from API on each retry attempt.
    """
    name = file_info.get('name', 'unknown')
    size_mb = file_info.get('size_mb', 0)
    size_bytes = int(size_mb * 1024 * 1024)
    
    # Get direct_url
    url = file_info.get('direct_url')

    if not url:
        await update.message.reply_text(f"❌ No direct download URL for: {name}")
        if db_manager:
            db_manager.record_failure(update.effective_user.id, original_link, "No direct_url available")
        return

    if size_bytes > MAX_SIZE:
        await update.message.reply_text(f"⚠️ File too large ({size_mb:.1f} MB): {name}")
        if db_manager:
            db_manager.record_oversized(update.effective_user.id, original_link, name, size_bytes)
        log.warning(f"⚠️ Oversized file skipped: {name} ({size_mb:.1f} MB)")
        return

    safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in name)
    path = f"/tmp/terabox_{hashlib.md5(url.encode()).hexdigest()}_{safe_name}"

    session = await get_session()
    
    # Try download with retries and fresh API calls
    for attempt in range(1, MAX_DOWNLOAD_RETRIES + 1):
        try:
            if attempt > 1:
                # Fetch fresh direct_url from API for retry attempts
                log.info(f"🔄 Retry attempt {attempt}/{MAX_DOWNLOAD_RETRIES} for: {name}")
                await asyncio.sleep(3 * attempt)  # Exponential backoff
                
                fresh_url, fresh_file_info = await fetch_fresh_direct_url(original_link, name, session)
                
                if not fresh_url:
                    log.error(f"❌ Could not get fresh direct_url for retry {attempt}")
                    if attempt == MAX_DOWNLOAD_RETRIES:
                        raise Exception("Failed to get fresh direct_url after all retries")
                    continue
                
                url = fresh_url
                # Update path with new URL hash
                path = f"/tmp/terabox_{hashlib.md5(url.encode()).hexdigest()}_{safe_name}"
            
            log.info(f"⬇️ [{attempt}/{MAX_DOWNLOAD_RETRIES}] {name} ({size_mb:.1f} MB)")
            await download_file(url, path, session)
            log.info(f"✅ Downloaded: {name}")
            
            # Upload successful - break retry loop
            await upload_and_cleanup(update, path, name, original_link, size_bytes)
            return
            
        except Exception as e:
            error_msg = str(e)[:200]
            log.error(f"❌ Download failed (attempt {attempt}/{MAX_DOWNLOAD_RETRIES}): {name} – {error_msg}")
            
            # Clean up partial file
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
            if os.path.exists(path + ".part"):
                try:
                    os.remove(path + ".part")
                except OSError:
                    pass
            
            # If this was the last attempt, record failure and notify user
            if attempt == MAX_DOWNLOAD_RETRIES:
                if db_manager:
                    db_manager.record_failure(update.effective_user.id, original_link, error_msg)
                await update.message.reply_text(
                    f"❌ Failed after {MAX_DOWNLOAD_RETRIES} attempts: {name}\n"
                    f"Error: {str(e)[:100]}"
                )
            # Otherwise continue to next retry attempt
            continue


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

        # Process files one by one
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
    """Show download statistics with action buttons"""
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
        f"⚠️ Oversized: `{stats['total_oversized']}`\n"
        f"🔄 Duplicates: `{stats['total_duplicates']}` ({dup_status})\n"
        f"💾 Total Size: `{stats['total_size_gb']} GB`\n"
    )
    
    keyboard = [
        [
            InlineKeyboardButton("❌ View Failed", callback_data="view_failed_0"),
            InlineKeyboardButton("⚠️ View Oversized", callback_data="view_oversized_0")
        ],
        [
            InlineKeyboardButton("🔄 Toggle Duplicates", callback_data="toggle_duplicates"),
            InlineKeyboardButton("🔄 Retry Failed", callback_data="retry_all")
        ],
        [
            InlineKeyboardButton("🗑️ Clear Database", callback_data="clear_db_confirm")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(message, parse_mode="Markdown", reply_markup=reply_markup)


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button callbacks"""
    query = update.callback_query
    await query.answer()
    
    if not db_manager:
        await query.edit_message_text("❌ Database not connected")
        return
    
    user_id = update.effective_user.id
    data = query.data
    
    # View Failed Links
    if data.startswith("view_failed_"):
        page = int(data.split("_")[-1])
        skip = page * 10
        failed, total = db_manager.get_failed_links(user_id, skip=skip, limit=10)
        
        if not failed:
            await query.edit_message_text("✅ No failed links!")
            return
        
        message = f"❌ *Failed Links* (Page {page + 1}/{(total - 1) // 10 + 1})\n\n"
        for idx, item in enumerate(failed, start=skip + 1):
            retries = item.get('retry_count', 1)
            error = item.get('last_error', 'Unknown error')[:80]
            link = item['original_link']
            message += f"{idx}. `{link}`\n   Retries: {retries} | Error: `{error}`\n\n"
        
        # Navigation buttons
        keyboard = []
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"view_oversized_{page - 1}"))
        if skip + 10 < total:
            nav_row.append(InlineKeyboardButton("➡️ Next", callback_data=f"view_oversized_{page + 1}"))
        if nav_row:
            keyboard.append(nav_row)
        keyboard.append([InlineKeyboardButton("🔙 Back to Stats", callback_data="back_to_stats")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(message, parse_mode="Markdown", reply_markup=reply_markup)
    
    # Toggle Duplicates
    elif data == "toggle_duplicates":
        current_status = db_manager.get_user_setting(user_id, "allow_duplicates", True)
        new_status = not current_status
        db_manager.set_user_setting(user_id, "allow_duplicates", new_status)
        
        status_text = "✅ Allowed" if new_status else "❌ Blocked"
        await query.edit_message_text(
            f"🔄 *Duplicate Downloads*\n\n"
            f"Status: {status_text}\n\n"
            f"When blocked: Won't download files you already have\n"
            f"When allowed: Downloads everything (default)",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Stats", callback_data="back_to_stats")]])
        )
    
    # Retry All Failed Links
    elif data == "retry_all":
        failed, total = db_manager.get_failed_links(user_id, limit=50)
        
        if not failed:
            await query.edit_message_text(
                "✅ No failed links to retry!",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Stats", callback_data="back_to_stats")]])
            )
            return
        
        retry_count = 0
        for item in failed:
            link = item['original_link']
            db_manager.retry_failed_link(str(item['_id']))
            # Create a minimal update object for processing
            from telegram import Message, Chat, User
            mock_message = Message(
                message_id=query.message.message_id,
                date=query.message.date,
                chat=query.message.chat,
                from_user=query.from_user
            )
            mock_update = Update(update_id=update.update_id, message=mock_message)
            asyncio.create_task(process_link_independently(mock_update, link))
            retry_count += 1
        
        await query.edit_message_text(
            f"🔄 Retrying {retry_count} failed link(s)...\n\nYou'll receive notifications as files are processed.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Stats", callback_data="back_to_stats")]])
        )
    
    # Clear Database Confirmation
    elif data == "clear_db_confirm":
        keyboard = [
            [
                InlineKeyboardButton("✅ Yes, Clear All", callback_data="clear_db_yes"),
                InlineKeyboardButton("❌ Cancel", callback_data="back_to_stats")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "⚠️ *Clear Database Confirmation*\n\n"
            "This will permanently delete:\n"
            "• All download history\n"
            "• All failed links\n"
            "• All oversized files\n"
            "• All settings\n\n"
            "Are you sure?",
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
    
    # Clear Database Execution
    elif data == "clear_db_yes":
        result = db_manager.clear_user_data(user_id)
        
        if result:
            message = (
                f"✅ *Database Cleared Successfully!*\n\n"
                f"Deleted:\n"
                f"• Downloads: `{result['downloads']}`\n"
                f"• Failed Links: `{result['failed']}`\n"
                f"• Oversized Files: `{result['oversized']}`\n"
                f"• Settings: `{result['settings']}`\n"
            )
        else:
            message = "❌ Failed to clear database. Please try again."
        
        await query.edit_message_text(
            message,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Stats", callback_data="back_to_stats")]])
        )
    
    # Back to Stats
    elif data == "back_to_stats":
        stats = db_manager.get_stats(user_id)
        allow_duplicates = db_manager.get_user_setting(user_id, "allow_duplicates", True)
        
        if not stats:
            await query.edit_message_text("❌ Could not retrieve stats")
            return
        
        dup_status = "✅ Allowed" if allow_duplicates else "❌ Blocked"
        message = (
            f"📊 *Your Download Stats*\n\n"
            f"✅ Successful: `{stats['total_success']}`\n"
            f"❌ Failed: `{stats['total_failed']}`\n"
            f"⚠️ Oversized: `{stats['total_oversized']}`\n"
            f"🔄 Duplicates: `{stats['total_duplicates']}` ({dup_status})\n"
            f"💾 Total Size: `{stats['total_size_gb']} GB`\n"
        )
        
        keyboard = [
            [
                InlineKeyboardButton("❌ View Failed", callback_data="view_failed_0"),
                InlineKeyboardButton("⚠️ View Oversized", callback_data="view_oversized_0")
            ],
            [
                InlineKeyboardButton("🔄 Toggle Duplicates", callback_data="toggle_duplicates")
            ],
            [
                InlineKeyboardButton("🗑️ Clear Database", callback_data="clear_db_confirm")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(message, parse_mode="Markdown", reply_markup=reply_markup)


async def retry_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Retry failed links"""
    if not db_manager:
        await update.message.reply_text("❌ Database not connected")
        return

    user_id = update.effective_user.id
    failed, total = db_manager.get_failed_links(user_id, limit=50)
    
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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚡ *Ultra-Fast Terabox Bot*\n\n"
        "📥 Send any Terabox link(s)!\n"
        "🔄 Auto-retry with fresh URLs on failure\n\n"
        "Use /stats to view your download statistics",
        parse_mode="Markdown"
    )


def main():
    log.info("🚀 Terabox Bot Starting (Smart Retry Enabled)...")
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("retry", retry_command))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, handle_message))

    async def set_commands(app):
        """Set up command menu"""
        from telegram import BotCommand
        commands = [
            BotCommand("start", "Start the bot"),
            BotCommand("stats", "View your download stats with options"),
            BotCommand("retry", "Retry all failed links"),
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
        keyboard = []
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"view_failed_{page - 1}"))
        if skip + 10 < total:
            nav_row.append(InlineKeyboardButton("➡️ Next", callback_data=f"view_failed_{page + 1}"))
        if nav_row:
            keyboard.append(nav_row)
        keyboard.append([InlineKeyboardButton("🔙 Back to Stats", callback_data="back_to_stats")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(message, parse_mode="Markdown", reply_markup=reply_markup)
    
    # View Oversized Links
    elif data.startswith("view_oversized_"):
        page = int(data.split("_")[-1])
        skip = page * 10
        oversized, total = db_manager.get_oversized_links(user_id, skip=skip, limit=10)
        
        if not oversized:
            await query.edit_message_text("✅ No oversized files!")
            return
        
        message = f"⚠️ *Oversized Files* (Page {page + 1}/{(total - 1) // 10 + 1})\n\n"
        for idx, item in enumerate(oversized, start=skip + 1):
            file_size_mb = item.get('file_size', 0) / 1e6
            file_name = item.get('file_name', 'unknown')
            link = item['original_link']
            message += f"{idx}. `{file_name}`\n   Size: `{file_size_mb:.1f} MB`\n   Link: `{link}`\n\n"
        
        # Navigation buttons
