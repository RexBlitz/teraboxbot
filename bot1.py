import os
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

class TeraboxDownloaderBot:
    def __init__(self):
        self.bot_token = BOT_TOKEN
        self.terabox_api = TERABOX_API_BASE
        self.tg_api_url = SELF_HOSTED_TG_API
        
    def is_valid_terabox_url(self, url: str) -> bool:
        """Check if the URL is a valid Terabox share URL"""
        try:
            parsed = urlparse(url)
            # Check common terabox domains
            valid_domains = [
                'terabox.com',
                '1024terabox.com',
                'www.terabox.com',
                'www.1024terabox.com'
            ]
            return any(domain in parsed.netloc for domain in valid_domains)
        except Exception:
            return False

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

**Supported domains:**
• terabox.com
• 1024terabox.com

**How to use:**
1. Copy a Terabox share link
2. Paste it here
3. I'll process and send you the video

**Example link format:**
`https://1024terabox.com/s/1bNLoEdlmOuyZcofBcnFdow`

Made with ❤️ using Terabox API
        """
        await update.message.reply_text(welcome_text, parse_mode='Markdown')

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command"""
        help_text = """
🆘 **Help Guide**

**How to download videos:**
1. Find a Terabox video you want to download
2. Copy the share link (usually looks like: `https://1024terabox.com/s/...`)
3. Paste the link in this chat
4. Wait for processing
5. Download your video!

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
        
        # Check if message contains a URL
        if not user_message.startswith(('http://', 'https://')):
            await update.message.reply_text(
                "❌ Please send a valid Terabox share URL starting with http:// or https://"
            )
            return

        # Validate Terabox URL
        if not self.is_valid_terabox_url(user_message):
            await update.message.reply_text(
                "❌ Invalid Terabox URL. Please provide a valid Terabox share link.\n\n"
                "Supported domains: terabox.com, 1024terabox.com"
            )
            return

        # Send processing message
        processing_msg = await update.message.reply_text(
            "⏳ Processing your request... This may take a few moments."
        )

        try:
            # Get download information from API
            download_info = await self.get_download_info(user_message)
            
            if "error" in download_info:
                await processing_msg.edit_text(
                    f"❌ Error: {download_info['error']}\n\nPlease try again later."
                )
                return

            files = download_info.get("files", [])
            if not files:
                await processing_msg.edit_text("❌ No downloadable files found in the provided link.")
                return

            # Process the first file (you can extend this to handle multiple files)
            file_info = files[0]
            file_name = file_info.get("file_name", "Unknown")
            file_size = file_info.get("size", "Unknown")
            download_url = file_info.get("download_url")
            streaming_url = file_info.get("streaming_url")

            if not download_url:
                await processing_msg.edit_text("❌ No download URL found for this file.")
                return

            # Send file information
            info_text = f"""
📁 **File Information:**
• **Name:** `{file_name}`
• **Size:** `{file_size}`
• **Status:** Processing download...

⏳ Please wait while I prepare your download...
            """
            await processing_msg.edit_text(info_text, parse_mode='Markdown')

            # Download and send the video
            await self.send_video(update, context, download_url, file_name, file_size)

        except Exception as e:
            logger.error(f"Error processing request: {str(e)}")
            await processing_msg.edit_text(
                "❌ An error occurred while processing your request. Please try again later."
            )

    async def send_video(self, update: Update, context: ContextTypes.DEFAULT_TYPE, 
                        download_url: str, file_name: str, file_size: str):
        """Download and send video to user"""
        try:
            # For large files, we'll send the download link directly
            # You can modify this to actually download and upload if files are small
            
            download_text = f"""
🎥 **Download Ready!**

📁 **File:** `{file_name}`
💾 **Size:** `{file_size}`

🔗 **Download Links:**
• [Direct Download]({download_url})
• [Stream Online]({download_url})

**Instructions:**
1. Click the link above to download
2. Or copy the URL and open in your browser

⚠️ **Note:** For very large files, direct download is recommended.
            """
            
            await update.message.reply_text(
                download_text,
                parse_mode='Markdown',
                disable_web_page_preview=False
            )

        except Exception as e:
            logger.error(f"Error sending video: {str(e)}")
            await update.message.reply_text(
                "❌ Failed to send video. The file might be too large or unavailable.\n\n"
                "Try downloading directly from the link above."
            )

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
