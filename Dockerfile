FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Copy requirement file and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all project files
COPY . .

# Expose Flask port
EXPOSE 8080

# Define environment variables (can override when deploying)
ENV PORT=8080
ENV BOT_TOKEN="YOUR_TELEGRAM_BOT_TOKEN"

# Command to run bot and Flask server
CMD ["python3", "bot.py"]
