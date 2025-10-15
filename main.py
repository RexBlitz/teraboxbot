import asyncio
import re
import os
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# ===== CONFIG =====
BOT_TOKEN = "8008678561:AAH80tlSuc-tqEYb12eXMfUGfeo7Wz8qUEU"  # your bot token
API_BASE = "https://terabox-worker.robinkumarshakya103.workers.dev/api"
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2GB
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
async def download_and_send(update: Update, link: str, failed_links: list):
    async with semaphore:
        try:
            resp = requests.get(f"{API_BASE}?url={link}", timeout=60)
            data = resp.json()
            if not data.get("success") or not data.get("files"):
                failed_links.append(link)
                return

            file = data["files"][0]
            filename = file["file_name"]
            size_bytes = int(file.get("size_bytes", 0))
            download_url = file["download_url"]
            caption = f"🎬 *{filename}*\n📦 Size: {file['size']}\n"

            if size_bytes > MAX_FILE_SIZE:
                failed_links.append(link)
                return

            # Download file
            file_path = f"/tmp/{filename}"
            with requests.get(download_url, stream=True) as r:
                r.raise_for_status()
                with open(file_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)

            # Send video immediately
            await update.message.reply_video(
                video=open(file_path, "rb"),
                caption=caption,
                parse_mode="Markdown"
            )

            os.remove(file_path)

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
    tasks = [asyncio.create_task(download_and_send(update, link, failed_links)) for link in links]
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
