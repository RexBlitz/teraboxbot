# ===== Base Image =====
FROM python:3.11-slim

# ===== Install system dependencies =====
RUN apt-get update && apt-get install -y \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ===== Set Workdir =====
WORKDIR /app

# ===== Install Telegram Bot API Server Binary =====
RUN git clone --depth=1 --branch v1.6.3 https://github.com/tdlib/telegram-bot-api.git /tmp/telegram-bot-api-src \
    && mkdir -p /tmp/telegram-bot-api-build \
    && cd /tmp/telegram-bot-api-build \
    && cmake /tmp/telegram-bot-api-src \
    && make -j$(nproc) \
    && cp telegram-bot-api /usr/local/bin/ \
    && rm -rf /tmp/telegram-bot-api-src /tmp/telegram-bot-api-build


# ===== Copy Requirements & Install Python Deps =====
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ===== Copy Bot Code =====
COPY . .

# ===== Create startup script (HARDCODED — NO ENV) =====
RUN echo '#!/bin/bash\n\
# START TELEGRAM BOT API SERVER\n\
telegram-bot-api --api-id=20984573 --api-hash=9f694b45564ad23675aa3a01ffa9b7ca --http-port=8081 &\n\
sleep 5\n\
# START YOUR BOT\n\
python main.py\n\
' > start.sh && chmod +x start.sh

# ===== Run both services =====
CMD ["./start.sh"]
