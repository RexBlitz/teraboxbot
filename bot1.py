import asyncio 
import re 
import os 
import aiohttp 
import aiofiles 
from telegram import Update 
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes 

# ===== CONFIG =====
BOT_TOKEN = "8366499465:AAE72m_WzZ-sb9aJJ4YGv4KKMIXLjSafijA"
API_BASE = "https://terabox-worker.robinkumarshakya103.workers.dev" # Base URL without the /api endpoint
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024 # 2GB
CONCURRENT_DOWNLOADS = 15
# ==================

semaphore = asyncio.Semaphore(CONCURRENT_DOWNLOADS)

# ===== Commands =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👋 *Terabox Downloader Bot*\n\n"
        "📥 Send me Terabox link(s) and I'll download them.\n"
        "⚠️ Max file size: 2GB\n"
        "⚠️ Maximum concurrent downloads: 15\n\n"
        "Available commands:\n"
        "/start - Show this message"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

# ===== Download Function =====
async def download_and_send(update: Update, link: str, failed_links: list, session: aiohttp.ClientSession):
    """Fetches file info, downloads the file, and sends it to the user."""
    file_path = None
    async with semaphore:
        try:
            # 1. Get file info (FIXED: Added /api to the endpoint)
            api_url = f"{API_BASE}/api?url={link}"
            
            async with session.get(api_url, timeout=60) as resp:
                resp.raise_for_status() # Raise exception for bad status codes (4xx or 5xx)
                data = await resp.json()
            
            # Check for API failure or missing files
            if not data.get("success") or not data.get("files"):
                failed_links.append(f"{link} (API failed or no files found)")
                return

            file = data["files"][0]
            filename = file["file_name"]
            # Convert size string (e.g., "724.52 MB") into a number of bytes for checking
            size_bytes = int(file.get("size_bytes", 0)) 
            download_url = file["download_url"]
            
            caption = f"🎬 *{filename}*\n📦 Size: {file['size']}\n"

            if size_bytes > MAX_FILE_SIZE:
                failed_links.append(f"{link} (File size {file['size']} exceeds 2GB limit)")
                return

            # 2. Download file asynchronously
            file_path = f"/tmp/{filename}"
            
            # Attempt to download the file using the worker's redirect URL
            async with session.get(download_url) as r:
                r.raise_for_status()
                # Ensure the file path exists and is writable
                os.makedirs(os.path.dirname(file_path) or '.', exist_ok=True) 
                
                async with aiofiles.open(file_path, "wb") as f:
                    # Use iter_chunked for large file downloads
                    async for chunk in r.content.iter_chunked(8192):
                        await f.write(chunk)

            # 3. Send video
            # The context object is available via 'context' but telegram-python-bot v20+ 
            # requires file object to be passed as File in memory or a file path.
            # Using standard open() for compatibility with the library's internal upload logic.
            with open(file_path, "rb") as video_file:
                 await update.message.reply_video(
                    video=video_file,
                    caption=caption,
                    parse_mode="Markdown"
                )

        except aiohttp.ClientResponseError as e:
            failed_links.append(f"{link} (HTTP Error: {e.status})")
        except asyncio.TimeoutError:
            failed_links.append(f"{link} (Request timed out)")
        except Exception as e:
            # Catch all other exceptions, like JSON parsing errors or file I/O issues
            print(f"Error processing link {link}: {e}")
            failed_links.append(f"{link} (Internal Error)")
        finally:
            # 4. Clean up temporary file
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except OSError as e:
                    print(f"Error removing temporary file {file_path}: {e}")


# ===== Message Handler =====
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or getattr(update.message, "caption", None)
    if not text:
        return

    # Clean text to remove non-ASCII characters and normalize spaces
    clean_text = re.sub(r"[^\x20-\x7E]+", " ", text)
    clean_text = re.sub(r"\s+", " ", clean_text)
    
    # Use re.IGNORECASE for robust link matching
    link_regex = re.compile(
        r"https?://(?:www\.)?(?:terabox|1024terabox|teraboxshare)\.com/s/[A-Za-z0-9_-]+",
        re.IGNORECASE
    )
    
    # Extract unique links while preserving order
    links = list(dict.fromkeys(link_regex.findall(clean_text)))

    if not links:
        return

    msg = await update.message.reply_text(f"🔍 Found {len(links)} link(s). Starting downloads...")
    failed_links = []

    # Use a single aiohttp session for all parallel tasks
    async with aiohttp.ClientSession() as session:
        tasks = [
            asyncio.create_task(download_and_send(update, link, failed_links, session)) 
            for link in links
        ]
        await asyncio.gather(*tasks)

    # Report failures
    if failed_links:
        await update.message.reply_text(
            "❌ Failed to download or process the following link(s):\n" 
            + "\n".join(failed_links[:10]) # Limit output to 10 failures for cleaner message
            + (f"\n... and {len(failed_links)-10} more." if len(failed_links) > 10 else "")
        )
        
    await msg.delete()

# ===== Bot Launcher =====
def run_bot():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🚀 Bot is running...")
    # Using run_polling to start the bot
    app.run_polling(poll_interval=1.0) 

if __name__ == "__main__":
    run_bot()
