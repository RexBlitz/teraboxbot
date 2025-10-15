import logging
import requests
import os
import asyncio
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from flask import Flask
from threading import Thread

# ==================== CONFIG ====================
BOT_TOKEN = "8008678561:AAH80tlSuc-tqEYb12eXMfUGfeo7Wz8qUEU"
API_BASE = "https://terabox-worker.robinkumarshakya103.workers.dev/api"
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2GB
# =================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# ========== TELEGRAM HANDLERS ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👋 *Welcome to Terabox Downloader Bot!*\n\n"
        "📥 Send me any Terabox link and I’ll try to download the file directly!\n\n"
        "If it’s too large (>2 GB), I’ll send the download link instead."
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not any(x in text for x in ["terabox.com", "1024terabox.com", "teraboxshare.com"]):
        return await update.message.reply_text("❌ Please send a valid Terabox link.")

    await update.message.reply_text("🔍 Fetching your Terabox file... Please wait...")

    try:
        resp = requests.get(f"{API_BASE}?url={text}", timeout=60)
        data = resp.json()

        if not data.get("success") or not data.get("files"):
            return await update.message.reply_text("⚠️ Failed to get file info. Try another link.")

        file = data["files"][0]
        filename = file["file_name"]
        size = file["size"]
        uploader = file["uploader_name"]
        download_url = file["download_url"]
        stream_url = file.get("streaming_url")

        # Estimate size (in bytes if available)
        size_bytes = int(file.get("size_bytes", 0)) if "size_bytes" in file else 0

        caption = (
            f"🎬 *{filename}*\n"
            f"📦 Size: {size}\n"
            f"👤 Uploader: {uploader}\n"
        )

        if size_bytes and size_bytes > MAX_FILE_SIZE:
            # File too large — send links instead
            caption += f"\n⚠️ File is larger than 2 GB.\n\n📥 [Download Link]({download_url})"
            if stream_url:
                caption += f"\n🎦 [Stream Link]({stream_url})"
            await update.message.reply_text(caption, parse_mode="Markdown", disable_web_page_preview=False)
            return

        # Download the file temporarily
        await update.message.reply_text("⬇️ Downloading the file... This might take a minute.")

        file_path = f"/tmp/{filename}"
        with requests.get(download_url, stream=True) as r:
            r.raise_for_status()
            with open(file_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)

        # Send to Telegram
        await update.message.reply_video(video=open(file_path, "rb"), caption=caption, parse_mode="Markdown")

        os.remove(file_path)

    except Exception as e:
        logging.error(f"Error: {e}")
        await update.message.reply_text("❌ Error while processing file. Try again later.")

# ========== BOT RUNNER ==========
def run_bot():
    asyncio.run(start_bot())

async def start_bot():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("🚀 Telegram Bot is running...")
    await app.run_polling()

# ========== FLASK SERVER ==========
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "✅ Terabox Telegram Bot Running on Koyeb"

if __name__ == "__main__":
    Thread(target=lambda: asyncio.run(start_bot())).start()
    flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
