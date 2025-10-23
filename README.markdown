# TeraBox Downloader Bot

A Telegram bot for downloading and broadcasting videos from TeraBox links. It processes video files (e.g., .mp4, .mkv, .avi, .mov, .webm) from TeraBox URLs, sends them to users as replies, and supports broadcasting to configured channels. The bot includes admin settings and error notifications for failed downloads.

## Features
- **Download Videos**: Processes TeraBox URLs to download video files (up to 2 GB).
- **Reply to Messages**: Sends downloaded videos as replies to the original link message.
- **Error Notifications**: Informs users of errors (e.g., invalid links, non-video files, or download failures).
- **Concurrent Processing**: Handles up to 50 links concurrently using `asyncio.Semaphore`, queuing the rest.
- **Admin Controls**: Admins can toggle broadcasting for admin links or channel posts and set broadcast chat IDs.
- **Channel Support**: Detects TeraBox links in configured channels and broadcasts videos if enabled.
- **MongoDB Integration**: Stores configuration and broadcast history.
- **Logging**: Logs activities and errors to console and `teradownloader.log`.

## Prerequisites
- **Python**: 3.10 or higher
- **Dependencies**:
  - `aiogram` (3.x)
  - `aiohttp`
  - `motor`
- **MongoDB**: A MongoDB instance (e.g., MongoDB Atlas) for storing configuration and broadcast data.
- **Telegram Bot**: A Telegram bot token from [BotFather](https://t.me/BotFather).

## Installation

1. **Clone the Repository**:
   ```bash
   git clone <repository-url>
   cd teradownloader
   ```

2. **Set Up a Virtual Environment** (recommended):
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```

3. **Install Dependencies**:
   ```bash
   pip install aiogram aiohttp motor
   ```

4. **Configure Environment**:
   - Update `BOT_TOKEN` in `teradownloader.py` with your Telegram bot token.
   - Update `MONGO_URI` in `teradownloader.py` with your MongoDB connection string.
   - Optionally, modify `API_ENDPOINT` and `SELF_HOSTED_API` if using custom APIs.

5. **Run the Bot**:
   ```bash
   python3 teradownloader.py
   ```

## Usage

1. **Start the Bot**:
   - Send `/start` to the bot in Telegram to receive a welcome message.
   - Admins see additional options for `/settings`.

2. **Send TeraBox Links**:
   - Send a TeraBox URL (e.g., `https://www.terabox.com/sharing/...`) in a private chat or configured channel.
   - The bot processes video files, sends progress updates, and replies with the downloaded video.
   - Errors (e.g., invalid links, non-video files, or download failures) are reported in the chat.

3. **Admin Commands**:
   - `/settings`: Access admin settings (requires admin password, default: `11223344`).
   - Toggle admin or channel broadcasting and set broadcast chat IDs via the inline keyboard.

4. **Channel Broadcasting**:
   - Add the bot to a Telegram channel and enable `channel_broadcast_enabled` in settings.
   - The bot processes TeraBox links in channel posts and broadcasts videos to configured chat IDs.

## Configuration

- **MongoDB Collections**:
  - `config`: Stores global settings (e.g., broadcast settings, admin password).
  - `broadcasted`: Tracks broadcasted videos to prevent duplicates.
  - `admins`: Stores admin user details.
- **Default Config**:
  ```python
  DEFAULT_CONFIG = {
      "_id": "global",
      "admin_broadcast_enabled": False,
      "channel_broadcast_enabled": False,
      "broadcast_chats": [-1002780909369],
      "admin_password": "11223344"
  }
  ```

- **Modify Config**:
  - Use `/settings` to update broadcast settings or chat IDs.
  - Update `DEFAULT_CONFIG` in the code for custom defaults.

## Limitations
- **File Size**: Videos larger than 2 GB are skipped with an error message.
- **File Types**: Only `.mp4`, `.mkv`, `.avi`, `.mov`, and `.webm` files are processed.
- **Concurrency**: Processes up to 50 links concurrently, queuing additional links.
- **API Dependency**: Relies on external TeraBox API (`https://terabox.itxarshman.workers.dev/api`).

## Troubleshooting
- **ImportError for `Router`**:
  - Ensure `aiogram` version is 3.x:
    ```bash
    pip install --upgrade aiogram
    ```
- **MongoDB Connection Issues**:
  - Verify `MONGO_URI` and ensure your MongoDB instance is accessible.
- **Bot Not Responding**:
  - Check logs in `teradownloader.log` for errors.
  - Ensure `BOT_TOKEN` is valid and the bot is not blocked.
- **Links Not Processing**:
  - Confirm links match the TeraBox URL pattern in `LINK_REGEX`.
  - Check API endpoint availability.

## Contributing
- Submit issues or pull requests to the repository.
- Ensure code changes are compatible with `aiogram` 3.x and tested with Python 3.10+.

## License
MIT License. See [LICENSE](LICENSE) for details.