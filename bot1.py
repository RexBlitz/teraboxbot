import asyncio
import re
import os
import logging
import aiohttp
import aiofiles
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.request import HTTPXRequest

# ===== LOGGING =====
log_dir = "logs"
os.makedirs(log_dir, exist_ok=True)

logging.basicConfig(
    level=logging.WARNING,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(f"{log_dir}/bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ===== CONFIG =====
BOT_TOKEN = "8366499465:AAE72m_WzZ-sb9aJJ4YGv4KKMIXLjSafijA"
TELEGRAM_API_URL = "http://tgapi.arshman.space:8088"
TERABOX_API = "https://terabox-worker.robinkumarshakya103.workers.dev"
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2GB
CONCURRENT_DOWNLOADS = 15
DOWNLOAD_TIMEOUT = 300  # 5 minutes
CHUNK_SIZE = 8192

# TeraBox-specific URL regex
LINK_REGEX = re.compile(
    r"https?://[^\s]*?(?:terabox|teraboxapp|teraboxshare|nephobox|1024tera|1024terabox|freeterabox|terasharefile|terasharelink|mirrobox|momerybox|teraboxlink|teraboxurl)\.[^\s]+",
    re.IGNORECASE
)

semaphore = asyncio.Semaphore(CONCURRENT_DOWNLOADS)

# ===== Commands =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👋 *Terabox Downloader Bot*\n\n"
        "📥 Send me Terabox link(s) and I'll download them.\n"
        "⚠️ Max file size: 2GB\n\n"
        "Available commands:\n"
        "/start - Show this message"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

# ===== Download Function =====
async def download_and_send(update: Update, link: str, failed_links: list, session: aiohttp.ClientSession):
    async with semaphore:
        file_path = None
        max_retries = 3
        retry_count = 0
        
        try:
            logger.warning(f"Processing: {link}")

            # Get file info from Terabox API
            api_url = f"{TERABOX_API}/api?url={link}"
            
            async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status != 200:
                    logger.warning(f"API error {resp.status}")
                    failed_links.append(link)
                    return

                data = await resp.json()

            # Validate response
            if not data.get("success") or not data.get("files") or len(data["files"]) == 0:
                logger.warning(f"Invalid API response")
                failed_links.append(link)
                return

            file = data["files"][0]
            filename = file.get("file_name", "unknown")
            size_bytes = int(file.get("size_bytes", 0))
            file_size = file.get("size", "unknown")
            
            # Prefer streaming_url to avoid sign errors
            download_url = file.get("streaming_url") or file.get("download_url") or file.get("original_download_url")
            
            if not download_url:
                logger.warning(f"No download URL")
                failed_links.append(link)
                return

            # Check file size
            if size_bytes > MAX_FILE_SIZE:
                await update.message.reply_text(
                    f"❌ File too large: *{filename}*\n"
                    f"Size: {file.get('size', 'unknown')} (Max: 2GB)",
                    parse_mode="Markdown"
                )
                failed_links.append(link)
                return

            # Create temp directory if needed
            os.makedirs("/tmp", exist_ok=True)
            file_path = f"/tmp/{filename}"

            # Headers for download
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Referer': 'https://www.terabox.com/',
                'Accept': '*/*',
                'Accept-Encoding': 'gzip, deflate',
            }

            # Retry loop with fresh URL generation
            while retry_count < max_retries:
                try:
                    # Get fresh download URL before each attempt
                    async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                        if resp.status != 200:
                            break

                        fresh_data = await resp.json()

                    if not fresh_data.get("success") or not fresh_data.get("files"):
                        break

                    download_url = fresh_data["files"][0].get("streaming_url") or fresh_data["files"][0].get("download_url") or fresh_data["files"][0].get("original_download_url")
                    if not download_url:
                        break

                    # Download file with timeout
                    async with session.get(download_url, headers=headers, timeout=aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT)) as r:
                        if r.status == 200:
                            async with aiofiles.open(file_path, "wb") as f:
                                async for chunk in r.content.iter_chunked(CHUNK_SIZE):
                                    await f.write(chunk)
                            
                            logger.warning(f"✅ Downloaded: {filename}")
                            break
                        else:
                            retry_count += 1
                            if retry_count < max_retries:
                                await asyncio.sleep(2)
                            else:
                                logger.warning(f"❌ Failed after {max_retries} attempts")
                                failed_links.append(link)
                                return

                except Exception as e:
                    retry_count += 1
                    if retry_count >= max_retries:
                        raise

            # Send video
            if os.path.exists(file_path):
                caption = f"🎬 *{filename}*\n📦 Size: {file_size}"
                with open(file_path, "rb") as video_file:
                    await update.message.reply_video(
                        video=video_file,
                        caption=caption,
                        parse_mode="Markdown"
                    )
                logger.warning(f"📤 Sent: {filename}")
            else:
                failed_links.append(link)

        except asyncio.TimeoutError:
            logger.warning(f"⏱️ Timeout")
            failed_links.append(link)
            await update.message.reply_text(f"⏱️ Download timeout")
        except aiohttp.ClientError as e:
            logger.warning(f"🌐 Network error: {str(e)}")
            failed_links.append(link)
        except Exception as e:
            logger.warning(f"❌ Error: {str(e)}")
            failed_links.append(link)
        finally:
            # Clean up temp file
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except Exception:
                    pass

# ===== Message Handler =====
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        text = update.message.text or getattr(update.message, "caption", None)
        if not text:
            return

        # Extract links using comprehensive regex
        links = list(dict.fromkeys(LINK_REGEX.findall(text)))

        if not links:
            return

        logger.warning(f"🔍 Found {len(links)} link(s)")
        msg = await update.message.reply_text(f"🔍 Found {len(links)} link(s). Starting downloads...")

        failed_links = []
        async with aiohttp.ClientSession() as session:
            tasks = [
                asyncio.create_task(download_and_send(update, link, failed_links, session))
                for link in links
            ]
            await asyncio.gather(*tasks, return_exceptions=True)

        # Report failures
        if failed_links:
            await update.message.reply_text(
                "❌ Failed links:\n" + "\n".join(failed_links)
            )

        await msg.delete()

    except Exception as e:
        logger.warning(f"Handler error: {str(e)}")
        await update.message.reply_text("❌ An error occurred. Try again.")

# ===== Bot Launcher =====
def run_bot():
    request = HTTPXRequest(base_url=TELEGRAM_API_URL)
    app = ApplicationBuilder().token(BOT_TOKEN).request(request).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.warning("🚀 Bot started")
    app.run_polling()

if __name__ == "__main__":
    run_bot()
