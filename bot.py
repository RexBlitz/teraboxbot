import asyncio
import aiohttp
import re
import os
import tempfile
import time
import logging
from aiogram import Bot, Dispatcher, Router, types
from aiogram.types import Message, FSInputFile
from aiogram.filters import Command
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramBadRequest

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('teradownloader.log')
    ]
)
logger = logging.getLogger(__name__)

# TeraBox-specific URL regex
LINK_REGEX = re.compile(
    r"https?://[^\s]*?(?:terabox|teraboxapp|teraboxshare|nephobox|1024tera|1024terabox|freeterabox|terasharefile|terasharelink|mirrobox|momerybox|teraboxlink|teraboxurl)\.[^\s]+",
    re.IGNORECASE
)

# Configuration
BOT_TOKEN = "8008678561:AAH80tlSuc-tqEYb12eXMfUGfeo7Wz8qUEU"
API_ENDPOINT = "https://terabox.itxarshman.workers.dev/api"
SELF_HOSTED_API = "http://tgapi.arshman.space:8088"
BROADCAST_CHATS = [ -1002780909369, ]  # Add chat IDs here, e.g., [123456789, 987654321]

session = AiohttpSession(api=TelegramAPIServer.from_base(SELF_HOSTED_API))
bot = Bot(token=BOT_TOKEN, session=session)
dp = Dispatcher()
router = Router()
sem = asyncio.Semaphore(50)  # 50 concurrent connections

async def get_links(source_url: str):
    logger.info(f"Requesting links for URL: {source_url}")
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(f"{API_ENDPOINT}?url={source_url}") as resp:
                if resp.status == 200:
                    logger.info(f"Successfully retrieved links for {source_url}")
                    return await resp.json()
                logger.error(f"API request failed for {source_url}, status: {resp.status}")
        except Exception as e:
            logger.error(f"Error fetching links for {source_url}: {str(e)}")
    return None

async def download_file(dl_url: str, filename: str, progress_msg: Message, chat_id: int, size_mb: float, attempt: int = 0):
    path = tempfile.NamedTemporaryFile(delete=False).name
    downloaded = 0
    last_update = 0
    last_downloaded = 0
    logger.info(f"Starting download of {filename} from {dl_url} (attempt {attempt + 1})")
    try:
        async with sem:
            async with aiohttp.ClientSession() as session:
                async with session.get(dl_url) as resp:
                    if resp.status != 200:
                        logger.error(f"Download failed for {filename}, status: {resp.status}")
                        return False, None
                    content_length = int(resp.headers.get('Content-Length', 0))
                    total_mb = content_length / (1024 * 1024) if content_length else size_mb
                    async for chunk in resp.content.iter_chunked(5 * 1024 * 1024):  # 5MB chunks
                        with open(path, 'ab') as f:
                            f.write(chunk)
                        downloaded += len(chunk)
                        current_time = time.time()
                        if content_length:
                            if (downloaded - last_downloaded > 10 * 1024 * 1024 or current_time - last_update > 5):
                                percent = (downloaded / content_length) * 100
                                new_text = f"Downloading {filename}: {downloaded // (1024 * 1024)}/{int(total_mb)} MB ({percent:.0f}%)"
                                try:
                                    await bot.edit_message_text(
                                        chat_id=chat_id,
                                        message_id=progress_msg.message_id,
                                        text=new_text
                                    )
                                    last_update = current_time
                                    last_downloaded = downloaded
                                except TelegramBadRequest as e:
                                    if "message is not modified" in str(e):
                                        logger.debug(f"Skipped update for {filename}: message not modified")
                                    else:
                                        logger.error(f"Error updating progress for {filename}: {str(e)}")
                        else:
                            if current_time - last_update > 5:
                                percent = (downloaded / (size_mb * 1024 * 1024)) * 100
                                new_text = f"Downloading {filename}: {downloaded // (1024 * 1024)}/{int(size_mb)} MB ({percent:.0f}%)"
                                try:
                                    await bot.edit_message_text(
                                        chat_id=chat_id,
                                        message_id=progress_msg.message_id,
                                        text=new_text
                                    )
                                    last_update = current_time
                                except TelegramBadRequest as e:
                                    if "message is not modified" in str(e):
                                        logger.debug(f"Skipped update for {filename}: message not modified")
                                    else:
                                        logger.error(f"Error updating progress for {filename}: {str(e)}")
                    logger.info(f"Download completed for {filename}")
    except Exception as e:
        logger.error(f"Download error for {filename}: {str(e)}")
        if os.path.exists(path):
            os.unlink(path)
        if attempt < 2:  # Up to 3 attempts with backoff
            backoff = 2 ** attempt  # 1s, 2s, 4s
            logger.info(f"Retrying download for {filename} after {backoff}s")
            await asyncio.sleep(backoff)
            return await download_file(dl_url, filename, progress_msg, chat_id, size_mb, attempt + 1)
        return False, None
    return True, path

async def broadcast_video(file_path: str, video_name: str, chat_id: int):
    if not BROADCAST_CHATS:
        logger.warning("No broadcast chats configured")
        return
    if not video_name.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.webm')):
        logger.info(f"Skipping broadcast for non-video file: {video_name}")
        return
    broadcast_count = 0
    for bc_chat_id in BROADCAST_CHATS:
        try:
            input_file = FSInputFile(file_path, filename=video_name)
            await bot.send_video(
                chat_id=bc_chat_id,
                video=input_file,
                supports_streaming=True
            )
            logger.info(f"📤 Broadcasted {video_name} to chat {bc_chat_id}")
            broadcast_count += 1
        except Exception as e:
            logger.error(f"❌ Broadcast failed for chat {bc_chat_id}: {str(e)[:100]}")
    if broadcast_count > 0:
        logger.info(f"✅ Broadcast complete: {broadcast_count}/{len(BROADCAST_CHATS)} chats")

async def process_file(link: dict, source_url: str, chat_id: int):
    name = link.get("name", "unknown")
    size_mb = link.get("size_mb", 0)
    size_gb = size_mb / 1024
    logger.info(f"Processing file: {name}, size: {size_mb} MB")
    if size_gb > 2:
        logger.warning(f"File {name} size {size_gb:.2f} GB exceeds 2 GB limit")
        await bot.send_message(chat_id, f"File {name} is {size_gb:.2f} GB, exceeding the 2 GB limit. Skipping.")
        return
    progress_msg = await bot.send_message(chat_id, f"Processing {name} ({size_mb:.2f} MB)...")
    file_path = None
    new_link = None
    async with sem:
        try:
            for attempt in range(4):
                if attempt == 0:
                    dl_url = link["original_url"]
                    label = "proxied primary"
                elif attempt == 1:
                    dl_url = link["direct_url"]
                    label = "direct fallback"
                elif attempt == 2:
                    logger.info(f"Refreshing links for {name}")
                    await bot.edit_message_text(chat_id=chat_id, message_id=progress_msg.message_id, text=f"Refreshing links for {name}...")
                    new_resp = await get_links(source_url)
                    if not new_resp or "links" not in new_resp:
                        logger.error(f"Failed to refresh links for {name}")
                        await bot.edit_message_text(chat_id=chat_id, message_id=progress_msg.message_id, text=f"Failed to refresh links for {name}.")
                        break
                    new_link = next((l for l in new_resp["links"] if l.get("name") == name), None)
                    if not new_link:
                        logger.error(f"File {name} not found in refreshed links")
                        await bot.edit_message_text(chat_id=chat_id, message_id=progress_msg.message_id, text=f"File {name} not found in refreshed links.")
                        break
                    dl_url = new_link["original_url"]
                    label = "new proxied"
                elif attempt == 3:
                    if not new_link:
                        break
                    dl_url = new_link["direct_url"]
                    label = "new direct"
                else:
                    break
                logger.info(f"Attempting {label} download for {name}")
                await bot.edit_message_text(chat_id=chat_id, message_id=progress_msg.message_id, text=f"Attempting {label} download for {name}...")
                success, file_path = await download_file(dl_url, name, progress_msg, chat_id, size_mb)
                if success:
                    break
                logger.warning(f"{label.capitalize()} failed for {name}, retrying...")
                await bot.edit_message_text(chat_id=chat_id, message_id=progress_msg.message_id, text=f"{label.capitalize()} failed for {name}, retrying...")
            if not file_path:
                logger.error(f"File {name} failed to download after all retries")
                await bot.edit_message_text(chat_id=chat_id, message_id=progress_msg.message_id, text=f"File {name} failed to download after all retries.")
                return
            logger.info(f"Uploading {name} to Telegram")
            await bot.edit_message_text(chat_id=chat_id, message_id=progress_msg.message_id, text=f"Downloaded {name}, uploading...")
            input_file = FSInputFile(file_path, filename=name)
            if name.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.webm')):
                await bot.send_video(chat_id, input_file, supports_streaming=True)
                logger.info(f"Sent {name} as video to chat {chat_id}")
            else:
                await bot.send_document(chat_id, input_file)
                logger.info(f"Sent {name} as document to chat {chat_id}")
            await bot.edit_message_text(chat_id=chat_id, message_id=progress_msg.message_id, text=f"Uploaded {name} successfully!")
            # Broadcast video to other chats
            await broadcast_video(file_path, name, chat_id)
        except Exception as e:
            logger.error(f"Error processing {name}: {str(e)}")
            await bot.edit_message_text(chat_id=chat_id, message_id=progress_msg.message_id, text=f"Error processing {name}: {str(e)}")
        finally:
            if file_path and os.path.exists(file_path):
                logger.debug(f"Cleaning up temporary file: {file_path}")
                os.unlink(file_path)

async def process_url(source_url: str, chat_id: int):
    logger.info(f"Processing URL: {source_url}")
    response = await get_links(source_url)
    if not response or "links" not in response:
        logger.error(f"Failed to retrieve links for {source_url}")
        await bot.send_message(chat_id, f"Failed to retrieve links for {source_url}.")
        return
    for link in response["links"]:
        asyncio.create_task(process_file(link, source_url, chat_id))

@router.message()
async def handle_message(message: Message):
    text = (message.text or message.caption or "")
    urls = LINK_REGEX.findall(text)
    if not urls:
        logger.debug("No valid TeraBox URLs found in message")
        return
    chat_id = message.chat.id
    for url in urls:
        url = url.rstrip('.,!?')  # Clean trailing punctuation
        logger.info(f"Found URL in message: {url}")
        asyncio.create_task(process_url(url, chat_id))

@router.message(Command("start"))
async def start(message: Message):
    logger.info(f"Start command received from chat ID: {message.chat.id}")
    await message.answer("Send me TeraBox links to download videos.")

dp.include_router(router)

if __name__ == "__main__":
    logger.info("Starting TeraDownloader bot")
    asyncio.run(dp.start_polling(bot))
