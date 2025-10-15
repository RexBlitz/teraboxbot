import logging
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from flask import Flask
import os
from threading import Thread

# ========== CONFIG ==========
BOT_TOKEN = os.getenv("BOT_TOKEN", "8008678561:AAH80tlSuc-tqEYb12eXMfUGfeo7Wz8qUEU")  
API_BASE = "https://terabox-worker.robinkumarshakya103.workers.dev/api"
# =============================

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# ========== TELEGRAM HANDLERS ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👋 *Welcome to Terabox Downloader Bot!*\n\n"
        "📥 Send me any Terabox link \n"
        "and I’ll give you:\n"
        "🎬 File name\n"
        "📦 Size\n"
        "👤 Uploader\n"
        "📥 Download link\n"
        "🎦 Streaming link\n\n"

    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    # Validate Terabox link
    if not any(x in text for x in ["terabox.com", "1024terabox.com", "teraboxshare.com"]):
        return await update.message.reply_text("❌ Please send a valid Terabox link.")

    await update.message.reply_text("🔍 Fetching your Terabox file... Please wait.")

    try:
        resp = requests.get(f"{API_BASE}?url={text}")
        data = resp.json()

        if not data.get("success") or not data.get("files"):
            return await update.message.reply_text("⚠️ Failed to get file info. Try another link.")

        file = data["files"][0]
        caption = (
            f"🎬 *{file['file_name']}*\n"
            f"📦 Size: {file['size']}\n"
            f"👤 Uploader: {file['uploader_name']}\n\n"
            f"📥 [Download Link]({file['download_url']})\n"
            f"🎦 [Stream Link]({file['streaming_url']})"
        )

        await update.message.reply_text(caption, parse_mode="Markdown", disable_web_page_preview=False)

    except Exception as e:
        logging.error(f"Error: {e}")
        await update.message.reply_text("❌ API error. Please try again later.")

# ========== BOT LAUNCHER ==========
def run_bot():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("🚀 Telegram Bot is running...")
    app.run_polling()

# ========== FLASK SERVER (for hosting) ==========
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "✅ Terabox Telegram Bot Running"

if __name__ == "__main__":
    if os.getenv("RENDER") or os.getenv("VERCEL"):
        Thread(target=run_bot).start()
        flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
    else:
        run_bot()
