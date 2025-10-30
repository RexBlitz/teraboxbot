import os
import re
import logging
import requests
import asyncio
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from urllib.parse import urlparse

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration
BOT_TOKEN = "8366499465:AAE72m_WzZ-sb9aJJ4YGv4KKMIXLjSafijA"
TERABOX_API_BASE = "https://terabox-worker.robinkumarshakya103.workers.dev"
SELF_HOSTED_TG_API = "http://tgapi.arshman.space:8088"

# TeraBox-specific URL regex
LINK_REGEX = re.compile(
    r"https?://[^\s]*?(?:terabox|teraboxapp|teraboxshare|nephobox|1024tera|teraboxurl|1024terabox|freeterabox|terasharefile|terasharelink|mirrobox|momerybox|teraboxlink|teraboxurl)\.[^\s]+",
    re.IGNORECASE
)

class TeraboxDownloaderBot:
    def __init__(self):
        self.bot_token = BOT_TOKEN
        self.terabox_api = TERABOX_API_BASE
        self.tg_api_url = SELF_HOSTED_TG_API
        self.link_regex = LINK_REGEX
        
    def is_valid_terabox_url(self, url: str) -> bool:
        """Check if the URL is a valid Terabox share URL using regex"""
        return bool(self.link_regex.match(url.strip()))

    async def get_download_info(self, share_url: str) -> dict:
        """Get download information from Terabox API"""
        try:
            api_url = f"{self.terabox_api}/api"
            params = {"url": share_url}
            
            async with asyncio.get_event_loop().run_in_executor(
                None, lambda: requests.get(api_url, params=params, timeout=30)
            ) as response:
                
                if response.status_code == 200:
                    data = response.json()
                    if data.get("success"):
                        return data
                    else:
                        return {"error": "API returned unsuccessful response"}
                else:
                    return {"error": f"API request failed with status {response.status_code}"}
                    
        except requests.exceptions.Timeout:
            return {"error": "Request timeout - please try again later"}
        except requests.exceptions.RequestException as e:
            return {"error": f"Network error: {str(e)}"}
        except Exception as e:
            return {"error": f"Unexpected error: {str(e)}"}

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        welcome_text = """
🤖 **Terabox Video Downloader Bot**

Send me a Terabox share link and I'll download the video for you!

**Supported Domains:**
• terabox.com
• teraboxapp.com
• teraboxshare.com
• nephobox.com
• 1024tera.com
• teraboxurl.com
• 1024terabox.com
• freeterabox.com
• terasharefile.com
• terasharelink.com
• mirrobox.com
• momerybox.com
• teraboxlink.com
• teraboxurl.com

**How to use:**
1. Copy a Terabox share link
2. Paste it here
3. I'll process and send you the video

**Example link format:**
`https://terabox.com/s/1bNLoEdlmOuyZcofBcnFdow`

Made with ❤️ using Terabox API
        """
        await update.message.reply_text(welcome_text, parse_mode='Markdown')

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command"""
        help_text = """
🆘 **Help Guide**

**How to download videos:**
1. Find a Terabox video you want to download
2. Copy the share link (usually looks like: `https://terabox.com/s/...`)
3. Paste the link in this chat
4. Wait for processing
5. Download your video!

**Supported Domains:**
I support all major Terabox domains including:
terabox.com, teraboxapp.com, 1024terabox.com, freeterabox.com, and 10+ more!

**Common issues:**
• Make sure the link is a valid Terabox share link
• Some videos might be too large for Telegram
• If download fails, try again later

**Commands:**
/start - Start the bot
/help - Show this help message

**Note:** This bot uses a third-party API to download videos from Terabox.
        """
        await update.message.reply_text(help_text, parse_mode='Markdown')

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle incoming messages"""
        user_message = update.message.text.strip()
        
        # Check if message contains a URL using regex
        if not self.link_regex.search(user_message):
            await update.message.reply_text(
                "❌ Please send a valid Terabox share URL.\n\n"
                "I support links from:\n"
                "• terabox.com\n• 1024terabox.com\n• freeterabox.com\n"
                "• teraboxapp.com\n• and 10+ other Terabox domains\n\n"
                "**Example:** `https://terabox.com/s/1bNLoEdlmOuyZcofBcnFdow`",
                parse_mode='Markdown'
            )
            return

        # Extract the URL from message
        url_match = self.link_regex.search(user_message)
        if not url_match:
            await update.message.reply_text("❌ Could not extract valid URL from your message.")
            return

        share_url = url_match.group(0)

        # Send processing message
        processing_msg = await update.message.reply_text(
            "⏳ Processing your request... This may take a few moments."
        )

        try:
            # Get download information from API
            download_info = await self.get_download_info(share_url)
            
            if "error" in download_info:
                await processing_msg.edit_text(
                    f"❌ Error: {download_info['error']}\n\nPlease try again later."
                )
                return

            files = download_info.get("files", [])
            if not files:
                await processing_msg.edit_text("❌ No downloadable files found in the provided link.")
                return

            # Process all files found
            await self.process_files(update, processing_msg, files, download_info.get("credits", {}))

        except Exception as e:
            logger.error(f"Error processing request: {str(e)}")
            await processing_msg.edit_text(
                "❌ An error occurred while processing your request. Please try again later."
            )

    async def process_files(self, update: Update, processing_msg, files: list, credits: dict):
        """Process all files from the API response"""
        try:
            if len(files) == 1:
                # Single file - process directly
                file_info = files[0]
                await self.send_file_info(update, processing_msg, file_info, credits)
            else:
                # Multiple files - show list
                await self.send_files_list(update, processing_msg, files, credits)
                
        except Exception as e:
            logger.error(f"Error processing files: {str(e)}")
            await processing_msg.edit_text("❌ Error processing files.")

    async def send_file_info(self, update: Update, processing_msg, file_info: dict, credits: dict):
        """Send information and download links for a single file"""
        file_name = file_info.get("file_name", "Unknown")
        file_size = file_info.get("size", "Unknown")
        download_url = file_info.get("download_url")
        streaming_url = file_info.get("streaming_url")
        original_url = file_info.get("original_download_url")

        if not download_url:
            await processing_msg.edit_text("❌ No download URL found for this file.")
            return

        # Prepare download text with credits
        credits_text = ""
        if credits:
            dev = credits.get("developer", "")
            telegram = credits.get("telegram", "")
            if dev and telegram:
                credits_text = f"\n\n*Credits:* API by [{dev}]({telegram})"

        download_text = f"""
🎥 **Download Ready!**

📁 **File:** `{file_name}`
💾 **Size:** `{file_size}`

🔗 **Download Links:**
• [Direct Download]({download_url})
• [Stream Online]({streaming_url or download_url})
{f"• [Original URL]({original_url})" if original_url else ""}

**Instructions:**
1. Click the link above to download
2. Or copy the URL and open in your browser

⚠️ **Note:** For very large files, direct download is recommended.
{credits_text}
        """
        
        await processing_msg.edit_text(
            download_text,
            parse_mode='Markdown',
            disable_web_page_preview=True
        )

    async def send_files_list(self, update: Update, processing_msg, files: list, credits: dict):
        """Send list when multiple files are found"""
        files_list = []
        for i, file_info in enumerate(files[:10], 1):  # Limit to first 10 files
            file_name = file_info.get("file_name", "Unknown")
            file_size = file_info.get("size", "Unknown")
            files_list.append(f"`{i}.` **{file_name}** - `{file_size}`")

        files_text = "\n".join(files_list)
        
        credits_text = ""
        if credits:
            dev = credits.get("developer", "")
            telegram = credits.get("telegram", "")
            if dev and telegram:
                credits_text = f"\n\n*Credits:* API by [{dev}]({telegram})"

        list_text = f"""
📁 **Multiple Files Found** ({len(files)} files)

{files_text}

🔧 *Currently I can only process the first file. Support for multiple files coming soon!*

{credits_text}
        """
        
        await processing_msg.edit_text(
            list_text,
            parse_mode='Markdown'
        )
        
        # Process first file
        if files:
            await self.send_file_info(update, processing_msg, files[0], credits)

    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle errors"""
        logger.error(f"Update {update} caused error: {context.error}")
        
        if update and update.message:
            await update.message.reply_text(
                "❌ An unexpected error occurred. Please try again later."
            )

    def run(self):
        """Start the bot"""
        # Create application with custom API server
        application = Application.builder().token(self.bot_token).base_url(
            self.tg_api_url
        ).build()

        # Add handlers
        application.add_handler(CommandHandler("start", self.start_command))
        application.add_handler(CommandHandler("help", self.help_command))
        application.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND, self.handle_message
        ))
        
        # Add error handler
        application.add_error_handler(self.error_handler)

        # Start the bot
        logger.info("Bot is starting...")
        application.run_polling(allowed_updates=Update.ALL_TYPES)

def main():
    """Main function"""
    bot = TeraboxDownloaderBot()
    bot.run()

if __name__ == "__main__":
    main()
