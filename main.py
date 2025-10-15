import logging
import requests
import re
import os
import asyncio
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from flask import Flask
from threading import Thread

# ===== CONFIG =====
BOT_TOKEN = "8008678561:AAH80tlSuc-tqEYb12eXMfUGfeo7Wz8qUEU"
API_BASE = "https://terabox-worker.robinkumarshakya103.workers.dev/api"
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2GB
CONCURRENT_DOWNLOADS = 3
# ==================

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
semaphore = asyncio.Semaphore(CONCURRENT_DOWNLOADS)

# ===== Telegram Handlers =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👋 *Welcome to Terabox Downloader Bot!*\n\n"
        "📥 Send me any Terabox link(s) and I’ll download them for you.\n\n"
        "⚙️ Max file size: 2GB"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def download_and_send(update: Update, link: str):
    try:
        # Step 1: Get info from API
        resp = requests.get(f"{API_BASE}?url={link}", timeout=60)
        data = resp.json()

        if not data.get("success") or not data.get("files"):
            return await update.message.reply_text(f"❌ Failed to get info for:\n{link}")

        file = data["files"][0]
        filename = file["file_name"]
        size = file["size"]
        download_url = file["download_url"]
        size_bytes = int(file.get("size_bytes", 0))

        caption = f"🎬 *{filename}*\n📦 Size: {size}\n"

        # Step 2: If > 2GB → send link only
        if size_bytes > MAX_FILE_SIZE:
            caption += f"\n⚠️ File > 2GB.\n📥 [Download Link]({download_url})"
            return await update.message.reply_text(caption, parse_mode="Markdown")

        # Step 3: Download and send directly
        file_path = f"/tmp/{filename}"
        with requests.get(download_url, stream=True) as r:
            r.raise_for_status()
            with open(file_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)

        await update.message.reply_video(
            video=open(file_path, "rb"),
            caption=caption,
            parse_mode="Markdown"
        )

        os.remove(file_path)

    except Exception as e:
        await update.message.reply_text(f"❌ Error:\n`{e}`", parse_mode="Markdown")



async def handle_message(update, context):
    # Get text from message or caption (for photos/videos with text)
    text = update.message.text or update.message.caption
    if not text:
        return  # Nothing to process

    # Normalize text to remove weird unicode/formatting
    clean_text = re.sub(r"[^\x20-\x7E]+", " ", text)
    clean_text = re.sub(r"\s+", " ", clean_text)

    # Detect Terabox /s/ links
    links = re.findall(
        r"https?://(?:www\.)?(?:terabox|1024terabox|teraboxshare)\.com/s/[A-Za-z0-9_-]+",
        clean_text
    )

    if not links:
        return await update.message.reply_text("❌ No valid Terabox link found.")

    # Remove duplicates
    links = list(dict.fromkeys(links))

    await update.message.reply_text(f"🔍 Found {len(links)} Terabox link(s). Starting downloads...")

    # Start downloads with concurrency
    tasks = []
    for link in links:
        async with semaphore:
            tasks.append(asyncio.create_task(download_and_send(update, link)))

    await asyncio.gather(*tasks)
# ===== Telegram Launcher =====
def run_bot():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("🚀 Telegram Bot is running...")
    app.run_polling()


# ===== Flask for Hosting =====
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "✅ Terabox Telegram Bot Running"


if __name__ == "__main__":
    if os.getenv("KOYEB") or os.getenv("RENDER") or os.getenv("VERCEL"):
        Thread(target=run_bot, daemon=True).start()
        flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
    else:
        run_bot()
