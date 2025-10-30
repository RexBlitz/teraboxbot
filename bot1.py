import asyncio
import re
import os
import aiohttp
import aiofiles
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# ===== CONFIG =====
BOT_TOKEN = "8366499465:AAE72m_WzZ-sb9aJJ4YGv4KKMIXLjSafijA"
# Updated API base URL to use the self-hosted endpoint and the /api path.
API_BASE = "http://tgapi.arshman.space:8088/api"
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024 # 2GB
CONCURRENT_DOWNLOADS = 15
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
        file_path = None # Initialize file_path for cleanup in finally block
        try:
            # Get file info
            # This request now uses the updated API_BASE: http://tgapi.arshman.space:8088/api?url=...
            async with session.get(f"{API_BASE}?url={link}", timeout=60) as resp:
                data = await resp.json()
                if not data.get("success") or not data.get("files"):
                    # Log specific API failure reason
                    failure_message = data.get('message', 'Unknown API error or file list empty')
                    print(f"API Failure ({link}): {failure_message}")
                    failed_links.append(link)
                    return

                file = data["files"][0]
                filename = file["file_name"]
                # Assuming 'size_bytes' is still available or can be derived, or using 0 as fallback.
                size_bytes = int(file.get("size_bytes", 0))
                download_url = file["download_url"]
                caption = f"🎬 *{filename}*\n📦 Size: {file['size']}\n"

                if size_bytes > MAX_FILE_SIZE:
                    # Log file size failure
                    max_gb = MAX_FILE_SIZE / (1024 * 1024 * 1024)
                    print(f"File Too Large ({link}): {filename} ({file['size']}). Max allowed: {max_gb:.1f} GB.")
                    failed_links.append(link)
                    return

            # Download file asynchronously
            file_path = f"/tmp/{filename}"
            async with session.get(download_url) as r:
                r.raise_for_status() # Catches 4xx/5xx HTTP errors during download
                async with aiofiles.open(file_path, "wb") as f:
                    async for chunk in r.content.iter_chunked(8192):
                        await f.write(chunk)

            # Send video
            await update.message.reply_video(
                video=open(file_path, "rb"),
                caption=caption,
                parse_mode="Markdown"
            )

        except aiohttp.ClientResponseError as e:
            # Log specific HTTP errors (like 404, 500 from download URL)
            print(f"HTTP Download Error ({link}): Status {e.status} for URL {e.request_info.url}")
            failed_links.append(link)

        except Exception as e:
            # Catch-all for other errors (Connection, Telegram, file IO, JSON parsing)
            print(f"General Error processing {link}: {type(e).__name__}: {e}")
            failed_links.append(link)

        finally:
            # Ensure file is removed whether success or failure occurred
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except OSError as cleanup_e:
                    print(f"Cleanup Warning: Could not remove temp file {file_path}: {cleanup_e}")


# ===== Message Handler =====
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or getattr(update.message, "caption", None)
    if not text:
        return

    # Cleaning text to extract links
    clean_text = re.sub(r"[^\x20-\x7E]+", " ", text)
    clean_text = re.sub(r"\s+", " ", clean_text)
    links = list(dict.fromkeys(
        re.findall(r"https?://(?:www\.)?(?:terabox|1024terabox|teraboxshare)\.com/s/[A-Za-z0-9_-]+", clean_text)
    ))

    if not links:
        return

    msg = await update.message.reply_text(f"🔍 Found {len(links)} link(s). Starting downloads...")
    failed_links = []
    async with aiohttp.ClientSession() as session:
        tasks = [asyncio.create_task(download_and_send(update, link, failed_links, session)) for link in links]
        await asyncio.gather(*tasks)

    if failed_links:
        await update.message.reply_text(
            "❌ Failed to download the following link(s):\n" + "\n".join(failed_links)
        )
    await msg.delete()

# ===== Bot Launcher =====
def run_bot():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("🚀 Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    run_bot()
