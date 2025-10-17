import asyncio
import re
import os
import aiohttp
import aiofiles
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
import hashlib

# ===== CONFIG =====
BOT_TOKEN = "8008678561:AAH80tlSuc-tqEYb12eXMfUGfeo7Wz8qUEU"
API_BASE = "https://terabox.itxarshman.workers.dev/api"
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2GB (not enforced due to missing Content-Length)
CONCURRENT_DOWNLOADS = 50  # Increased for better performance
RETRY_ATTEMPTS = 5  # Retry failed downloads
RETRY_DELAY = 1  # Seconds between retries
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
        try:
            # Get file info
            async with session.get(f"{API_BASE}?url={link}", timeout=60) as resp:
                data = await resp.json()
            if not data.get("links"):
                failed_links.append(link)
                return

            failed_files = []
            for file in data["links"]:
                filename = file["name"]
                download_url = file["download_url"]
                caption = ""  # Empty caption as requested

                for attempt in range(RETRY_ATTEMPTS):
                    try:
                        # Download file to disk
                        file_path = f"/tmp/{hashlib.md5(download_url.encode()).hexdigest()}_{filename}"
                        async with session.get(download_url, timeout=300) as r:
                            r.raise_for_status()
                            async with aiofiles.open(file_path, "wb") as f:
                                async for chunk in r.content.iter_chunked(8192):
                                    await f.write(chunk)

                        # Send file based on type
                        if filename.lower().endswith(('.mp4', '.mkv', '.avi')):
                            await update.message.reply_video(
                                video=open(file_path, "rb"),
                                caption=caption,
                                parse_mode="Markdown"
                            )
                        else:
                            await update.message.reply_document(
                                document=open(file_path, "rb"),
                                caption=caption,
                                parse_mode="Markdown"
                            )
                        os.remove(file_path)
                        break  # Success, exit retry loop
                    except Exception as e:
                        if attempt < RETRY_ATTEMPTS - 1:
                            await asyncio.sleep(RETRY_DELAY)
                            continue
                        failed_files.append(filename)
                        break

            if failed_files:
                failed_links.append(f"{link} (failed files: {', '.join(failed_files)})")
        except Exception:
            failed_links.append(link)

# ===== Message Handler =====
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or getattr(update.message, "caption", None)
    if not text:
        return

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
