# ===== Multi-stage Build =====
FROM python:3.11-slim as bot-base

# ===== Install System Dependencies =====
RUN apt-get update && apt-get install -y \
    git \
    cmake \
    g++ \
    make \
    zlib1g-dev \
    libssl-dev \
    gperf \
    wget \
    curl \
    && rm -rf /var/lib/apt/lists/*

# ===== Build Telegram Bot API Server =====
WORKDIR /tmp
RUN git clone --recursive https://github.com/tdlib/telegram-bot-api.git && \
    cd telegram-bot-api && \
    mkdir build && \
    cd build && \
    cmake -DCMAKE_BUILD_TYPE=Release .. && \
    cmake --build . --target install && \
    cd ../.. && \
    rm -rf telegram-bot-api

# ===== Setup Bot Environment =====
WORKDIR /app

# ===== Copy Requirements & Install =====
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ===== Copy Bot Code =====
COPY main.py .

# ===== Create Startup Script =====
RUN echo '#!/bin/bash\n\
set -e\n\
\n\
# Start Telegram Bot API Server in background\n\
echo "🚀 Starting local Telegram Bot API server..."\n\
telegram-bot-api --api-id=20984573 --api-hash=9f694b45564ad23675aa3a01ffa9b7ca --local 2>&1 | tee /tmp/api.log &\n\
API_PID=$!\n\
\n\
# Wait for API server to be ready\n\
echo "⏳ Waiting for API server to start..."\n\
sleep 5\n\
\n\
# Check if API server is running\n\
if ! kill -0 $API_PID 2>/dev/null; then\n\
    echo "❌ Failed to start Telegram Bot API server"\n\
    cat /tmp/api.log\n\
    exit 1\n\
fi\n\
\n\
echo "✅ Telegram Bot API server started (PID: $API_PID)"\n\
echo "🤖 Starting bot..."\n\
\n\
# Start the bot\n\
python3 main.py\n\
' > /app/start.sh && chmod +x /app/start.sh

# ===== Expose Ports =====
EXPOSE 8081 8082

# ===== Run Script =====
CMD ["/app/start.sh"]
