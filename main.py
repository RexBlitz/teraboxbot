import logging
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from flask import Flask
import os
from threading import Thread
import asyncio

# ========== CONFIG ==========
BOT_TOKEN = "8008678561:AAH80tlSuc-tqEYb12eXMfUGfeo7Wz8qUEU"
API_BASE = "https://terabox-worker.robinkumarshakya103.workers.dev/api"
# =============================

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# ========== TELEGRAM HANDLERS ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👋 *Welcome to Terabox Downloader Bot!*\n\n"
        "📥 Send me any Terabox link and I’ll download the video for you!"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    # Validate Terabox link
    if not any(x in text for x in ["terabox.com", "1024terabox.com", "teraboxshare.com"]):
        return await update.message.reply_text("❌ Please send a valid Terabox link.")

    await update.message.reply_text("🔍 Fetching your Terabox file... Please wait.")

    try:
        resp = requests.get(f"{API_BASE}?url={text}", timeout=30)
        data = resp.json()

        if not data.get("success") or not data.get("files"):
            return await update.message.reply_text("⚠️ Failed to get file info. Try another link.")

        file = data["files"][0]
        file_name = file.get("file_name", "video.mp4")
        download_url = file.get("download_url")

        await update.message.reply_text(f"⬇️ Downloading *{file_name}*...", parse_mode="Markdown")

        # Download the video file
        video_path = f"/tmp/{file_name}"
        with requests.get(download_url, stream=True) as r:
            r.raise_for_status()
            with open(video_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)

        await update.message.reply_video(video=open(video_path, "rb"), caption=f"✅ {file_name}")

        os.remove(video_path)

    except Exception as e:
        logging.error(f"Error: {e}")
        await update.message.reply_text("❌ Something went wrong. Please try again later.")

# ========== BOT LAUNCHER ==========
def run_bot():
    asyncio.set_event_loop(asyncio.new_event_loop())
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("🚀 Telegram Bot is running...")
    app.run_polling(stop_signals=None)  # ✅ Fix: disable signal handlers

# ========== FLASK SERVER ==========
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "✅ Terabox Telegram Bot Running on Koyeb"

if __name__ == "__main__":
    Thread(target=run_bot, daemon=True).start()
    flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
