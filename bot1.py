import asyncio
import re
import os
import aiohttp
import aiofiles
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode
import traceback

# ===== CONFIG =====
BOT_TOKEN = "8366499465:AAE72m_WzZ-sb9aJJ4YGv4KKMIXLjSafijA"
TERABOX_API_BASE = "https://terabox-worker.robinkumarshakya103.workers.dev"
# FIX: Added a trailing slash (/) to ensure proper URL construction by the library.
SELF_HOSTED_TG_API = "http://tgapi.arshman.space:8088/"  # Your self-hosted Telegram Bot API
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2GB (Telegram limit)
CONCURRENT_DOWNLOADS = 15
# ==================

semaphore = asyncio.Semaphore(CONCURRENT_DOWNLOADS)

# ===== Commands =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends a welcome message and lists available commands."""
    msg = (
        "👋 *Terabox Downloader Bot*\n\n"
        "📥 Send me Terabox link(s) and I'll download them.\n"
        "⚠️ Max file size: 2GB (due to Telegram limitations)\n\n"
        "Available commands:\n"
        "/start - Show this message"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

# ===== Download Function =====
async def download_and_send(update: Update, link: str, failed_links: list, session: aiohttp.ClientSession):
    """Fetches Terabox file info, downloads the file, and sends it to Telegram."""
    
    # Use the semaphore to limit concurrent downloads
    async with semaphore:
        try:
            # 1. Get file info from the worker API
            # Note: Using /api endpoint as per the documentation
            api_url = f"{TERABOX_API_BASE}/api?url={link}"
            
            async with session.get(api_url, timeout=60) as resp:
                if resp.status != 200:
                    raise Exception(f"API failed with status {resp.status}")
                
                data = await resp.json()
                
                if not data.get("success") or not data.get("files"):
                    print(f"API response failed for {link}: {data.get('message', 'No files found')}")
                    raise Exception("API returned unsuccessful response or no files.")

            file = data["files"][0]
            filename = file["file_name"]
            # Ensure size is an integer for comparison, default to 0
            size_bytes = int(file.get("size_bytes", 0))
            download_url = file["download_url"]
            
            caption = f"🎬 *{filename}*\n📦 Size: {file['size']}\n"
            
            # 2. Check max file size
            if size_bytes > MAX_FILE_SIZE:
                await update.message.reply_text(
                    f"❌ File '{filename}' is too large ({file['size']}). Max limit is 2GB.",
                    reply_to_message_id=update.message.message_id
                )
                failed_links.append(link)
                return

            # 3. Download file asynchronously to a temporary location
            file_path = f"/tmp/{os.path.basename(filename)}" # Use basename to avoid path traversal issues

            download_msg = await update.message.reply_text(
                f"📥 Starting download of *{filename}* ({file['size']})...",
                parse_mode=ParseMode.MARKDOWN,
                reply_to_message_id=update.message.message_id
            )

            print(f"Downloading {filename} from {download_url}")
            
            download_headers = {"User-Agent": "Mozilla/5.0"} # Some workers require a UA
            async with session.get(download_url, headers=download_headers, timeout=3600) as r: # Increase timeout for download
                r.raise_for_status() # Raise exception for bad status codes
                
                async with aiofiles.open(file_path, "wb") as f:
                    chunk_count = 0
                    async for chunk in r.content.iter_chunked(8192):
                        await f.write(chunk)
                        chunk_count += 1
                        # Optional: Update status message every N chunks for feedback
                        if chunk_count % 500 == 0: 
                             try:
                                 # Prevent flooding by editing too often
                                 await download_msg.edit_text(
                                     f"⬇️ Downloading *{filename}*... (Progressing)", 
                                     parse_mode=ParseMode.MARKDOWN
                                 )
                             except:
                                 pass # Ignore edit errors (e.g., message not modified)
            
            print(f"Download complete for {filename}. Uploading...")

            # 4. Send video/file to Telegram
            with open(file_path, "rb") as f:
                # Use reply_video or reply_document based on file type if needed, 
                # but for simplicity, reply_video is often enough for common video formats.
                await update.message.reply_video(
                    video=f,
                    caption=caption,
                    parse_mode=ParseMode.MARKDOWN,
                    supports_streaming=True,
                    reply_to_message_id=update.message.message_id
                )
            
            # 5. Clean up temporary message and file
            await download_msg.delete()
            os.remove(file_path)

        except Exception as e:
            print(f"Error processing link {link}: {e}")
            traceback.print_exc()
            failed_links.append(link)
            # Try to clean up file if it exists
            if 'file_path' in locals() and os.path.exists(file_path):
                 os.remove(file_path)

# ===== Message Handler =====
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processes messages, extracts Terabox links, and starts downloads."""
    
    # Check for text in message or caption
    text = update.message.text or getattr(update.message, "caption", None)
    if not text:
        return

    # Sanitize and extract unique Terabox links
    clean_text = re.sub(r"[^\x20-\x7E]+", " ", text)
    clean_text = re.sub(r"\s+", " ", clean_text)
    # The regex is designed to find terabox, 1024terabox, and teraboxshare links
    links = list(dict.fromkeys(
        re.findall(r"https?://(?:www\.)?(?:terabox|1024terabox|teraboxshare|4funbox)\.com/s/[A-Za-z0-9_-]+", clean_text)
    ))

    if not links:
        return

    # Inform user about found links
    msg = await update.message.reply_text(
        f"🔍 Found {len(links)} unique link(s). Starting downloads...",
        reply_to_message_id=update.message.message_id
    )
    
    failed_links = []
    
    # Use a single aiohttp session for all tasks for efficiency
    async with aiohttp.ClientSession() as session:
        tasks = [asyncio.create_task(download_and_send(update, link, failed_links, session)) for link in links]
        await asyncio.gather(*tasks)

    # Report failures
    if failed_links:
        await update.message.reply_text(
            "❌ Failed to download the following link(s):\n" + "\n".join(failed_links),
            reply_to_message_id=update.message.message_id
        )
    
    # Delete the initial status message
    await msg.delete()

# ===== Bot Launcher =====
def run_bot():
    """Builds and runs the Telegram bot using the self-hosted API."""
    
    # FIX: Changed .api_url() to .base_url() to resolve AttributeError based on traceback.
    # The base_url now includes a trailing slash to prevent concatenation issues.
    app = ApplicationBuilder().token(BOT_TOKEN).base_url(SELF_HOSTED_TG_API).build()

    # Add handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🚀 Bot is running with self-hosted API...")
    app.run_polling(poll_interval=1)

if __name__ == "__main__":
    # Ensure /tmp directory exists for temporary file storage
    if not os.path.exists('/tmp'):
        os.makedirs('/tmp')
    run_bot()
