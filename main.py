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
CONCURRENT_DOWNLOADS = 15
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


import asyncio
import re
import os
import requests
from telegram import Update
from telegram.ext import ContextTypes

# Your semaphore limits concurrent downloads
semaphore = asyncio.Semaphore(15)  # adjust CONCURRENT_DOWNLOADS as needed
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2GB
API_BASE = "https://terabox-worker.robinkumarshakya103.workers.dev/api"


async def download_and_send(update: Update, link: str):
    async with semaphore:  # Limit concurrent downloads
        try:
            resp = requests.get(f"{API_BASE}?url={link}", timeout=60)
            data = resp.json()

            if not data.get("success") or not data.get("files"):
                return await update.message.reply_text(f"❌ Failed: {link}")

            file = data["files"][0]
            filename = file["file_name"]
            size = file["size"]
            download_url = file["download_url"]
            size_bytes = int(file.get("size_bytes", 0))

            caption = f"🎬 *{filename}*\n📦 Size: {size}\n"

            if size_bytes > MAX_FILE_SIZE:
                caption += f"\n⚠️ File > 2GB.\n📥 [Download Link]({download_url})"
                return await update.message.reply_text(caption, parse_mode="Markdown")

            # Download file in chunks
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

        except Exception as e:
            await update.message.reply_text(f"❌ Error:\n`{e}`", parse_mode="Markdown")


async def handle_message(update, context):
    # Extract text from message or caption
    text = update.message.text or getattr(update.message, "caption", None)
    if not text:
        return

    # Clean text
    clean_text = re.sub(r"[^\x20-\x7E]+", " ", text)
    clean_text = re.sub(r"\s+", " ", clean_text)

    # Find Terabox links
    links = list(dict.fromkeys(
        re.findall(r"https?://(?:www\.)?(?:terabox|1024terabox|teraboxshare)\.com/s/[A-Za-z0-9_-]+", clean_text)
    ))

    if not links:
        return

    msg = await update.message.reply_text(f"🔍 Found {len(links)} link(s). Starting downloads...")

    # Process links in batches using semaphore
    for i in range(0, len(links), 15):  # batch size = CONCURRENT_DOWNLOADS
        batch = links[i:i+15]
        tasks = [asyncio.create_task(download_and_send(update, link)) for link in batch]
        await asyncio.gather(*tasks)

    await msg.delete()

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
