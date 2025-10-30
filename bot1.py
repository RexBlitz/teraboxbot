import asyncio
import re
import os
import logging
import aiohttp
import aiofiles
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# ===== LOGGING =====
log_dir = "logs"
os.makedirs(log_dir, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(f"{log_dir}/bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ===== CONFIG =====
BOT_TOKEN = "8366499465:AAE72m_WzZ-sb9aJJ4YGv4KKMIXLjSafijA"
API_BASE = "https://terabox-worker.robinkumarshakya103.workers.dev"
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2GB
CONCURRENT_DOWNLOADS = 15
DOWNLOAD_TIMEOUT = 300  # 5 minutes
CHUNK_SIZE = 8192

# ================== 
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
            # Validate link format
            if not re.match(r"https?://(?:www\.)?(?:terabox|1024terabox|teraboxshare)\.com/s/[A-Za-z0-9_-]+", link):
                logger.warning(f"Invalid link format: {link}")
                failed_links.append(link)
                return

            logger.info(f"Processing link: {link}")

            # Get file info from API
            api_url = f"{API_BASE}/api?url={link}"
            async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status != 200:
                    logger.error(f"API error {resp.status} for link: {link}")
                    failed_links.append(link)
                    return

                data = await resp.json()

            # Validate response
            if not data.get("success") or not data.get("files") or len(data["files"]) == 0:
                logger.error(f"Invalid API response for link: {link}")
                failed_links.append(link)
                return

            file = data["files"][0]
            filename = file.get("file_name", "unknown")
            size_bytes = int(file.get("size_bytes", 0))

            # Check file size
            if size_bytes > MAX_FILE_SIZE:
                logger.warning(f"File too large ({size_bytes} bytes): {filename}")
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

            logger.info(f"Downloading: {filename} ({file.get('size', 'unknown')})")

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
                            logger.error(f"API error {resp.status} for link: {link}")
                            break

                        fresh_data = await resp.json()

                    if not fresh_data.get("files"):
                        logger.error(f"No files in fresh API response")
                        break

                    download_url = fresh_data["files"][0].get("download_url")
                    if not download_url:
                        logger.error(f"No download URL in fresh response")
                        break

                    logger.info(f"Download attempt {retry_count + 1}/{max_retries}: {filename}")

                    # Download file with timeout
                    async with session.get(download_url, headers=headers, timeout=aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT)) as r:
                        if r.status == 200:
                            async with aiofiles.open(file_path, "wb") as f:
                                async for chunk in r.content.iter_chunked(CHUNK_SIZE):
                                    await f.write(chunk)
                            
                            logger.info(f"Downloaded successfully: {filename}")
                            break
                        else:
                            error_text = await r.text()
                            logger.warning(f"Download attempt {retry_count + 1} failed - Status: {r.status}")
                            logger.warning(f"Response: {error_text[:200]}")
                            retry_count += 1
                            
                            if retry_count < max_retries:
                                await asyncio.sleep(2)  # Wait before retry
                            else:
                                logger.error(f"All {max_retries} attempts failed")
                                failed_links.append(link)
                                return

                except Exception as e:
                    retry_count += 1
                    logger.warning(f"Download attempt {retry_count} error: {str(e)}")
                    if retry_count < max_retries:
                        await asyncio.sleep(2)
                    else:
                        raise

            # Send video
            if os.path.exists(file_path):
                caption = f"🎬 *{filename}*\n📦 Size: {file.get('size', 'unknown')}"
                with open(file_path, "rb") as video_file:
                    await update.message.reply_video(
                        video=video_file,
                        caption=caption,
                        parse_mode="Markdown"
                    )
                logger.info(f"Sent to user: {filename}")
            else:
                logger.error(f"File not found after download: {file_path}")
                failed_links.append(link)

        except asyncio.TimeoutError:
            logger.error(f"Timeout downloading: {link}")
            failed_links.append(link)
            await update.message.reply_text(f"⏱️ Download timeout for: {link}")
        except aiohttp.ClientError as e:
            logger.error(f"Network error for {link}: {str(e)}")
            failed_links.append(link)
        except Exception as e:
            logger.error(f"Unexpected error for {link}: {str(e)}")
            failed_links.append(link)
        finally:
            # Clean up temp file
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    logger.info(f"Cleaned up temp file: {file_path}")
                except Exception as e:
                    logger.error(f"Failed to cleanup {file_path}: {str(e)}")

# ===== Message Handler =====
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        text = update.message.text or getattr(update.message, "caption", None)
        if not text:
            return

        # Clean and extract links
        clean_text = re.sub(r"[^\x20-\x7E]+", " ", text)
        clean_text = re.sub(r"\s+", " ", clean_text)
        
        links = list(dict.fromkeys(
            re.findall(r"https?://(?:www\.)?(?:terabox|1024terabox|teraboxshare)\.com/s/[A-Za-z0-9_-]+", clean_text)
        ))

        if not links:
            await update.message.reply_text("❌ No Terabox links found in your message.")
            return

        logger.info(f"Found {len(links)} link(s)")
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
                "❌ Failed to download the following link(s):\n" + "\n".join(failed_links),
                parse_mode="Markdown"
            )

        await msg.delete()

    except Exception as e:
        logger.error(f"Error in handle_message: {str(e)}")
        await update.message.reply_text("❌ An error occurred. Please try again.")

# ===== Bot Launcher =====
def run_bot():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("🚀 Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    run_bot()
