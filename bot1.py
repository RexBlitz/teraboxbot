import asyncio
import re
import os
import aiohttp
import aiofiles
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# ===== CONFIG =====
BOT_TOKEN = os.getenv("BOT_TOKEN", "8366499465:AAE72m_WzZ-sb9aJJ4YGv4KKMIXLjSafijA")
API_BASE = "https://terabox-worker.robinkumarshakya103.workers.dev/api"
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2GB
CONCURRENT_DOWNLOADS = 5  # 15 may overload CPU/RAM on free hosts
# ==================
semaphore = asyncio.Semaphore(CONCURRENT_DOWNLOADS)

# ===== Commands =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👋 *Terabox Downloader Bot*\n\n"
        "📥 Send me any Terabox link and I'll fetch the file for you.\n"
        "⚠️ *Max file size:* 2GB\n\n"
        "Commands:\n"
        "/start - Show this help message"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

# ===== Download Function =====
async def download_and_send(update: Update, link: str, failed_links: list, session: aiohttp.ClientSession):
    async with semaphore:
        try:
            # ✅ Fetch file info from API
            async with session.get(f"{API_BASE}?url={link}", timeout=60) as resp:
                if resp.status != 200:
                    failed_links.append(link)
                    return
                data = await resp.json()

            if not data.get("success") or not data.get("files"):
                failed_links.append(link)
                return

            file = data["files"][0]
            filename = file.get("file_name", "unknown_file")
            size_str = file.get("size", "Unknown size")
            download_url = file.get("download_url")

            if not download_url:
                failed_links.append(link)
                return

            size_bytes = int(file.get("size_bytes", 0)) if "size_bytes" in file else 0
            if size_bytes > MAX_FILE_SIZE:
                await update.message.reply_text(f"⚠️ File `{filename}` exceeds 2GB limit. Skipped.", parse_mode="Markdown")
                return

            caption = f"🎬 *{filename}*\n📦 Size: {size_str}\n"

            # ✅ Download file asynchronously
            file_path = f"/tmp/{filename}"
            async with session.get(download_url) as r:
                if r.status != 200:
                    failed_links.append(link)
                    return
                async with aiofiles.open(file_path, "wb") as f:
                    async for chunk in r.content.iter_chunked(8192):
                        await f.write(chunk)

            # ✅ Send video/file safely
            try:
                await update.message.reply_video(
                    video=open(file_path, "rb"),
                    caption=caption,
                    parse_mode="Markdown"
                )
            except Exception:
                # fallback: send as document if Telegram rejects video
                await update.message.reply_document(
                    document=open(file_path, "rb"),
                    caption=caption,
                    parse_mode="Markdown"
                )
            finally:
                os.remove(file_path)

        except Exception as e:
            failed_links.append(link)
            print(f"❌ Error downloading {link}: {e}")

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

    await msg.delete()

    if failed_links:
        await update.message.reply_text(
            "❌ Failed to process the following link(s):\n" + "\n".join(failed_links)
        )

# ===== Bot Launcher =====
def run_bot():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("🚀 Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    run_bot()
