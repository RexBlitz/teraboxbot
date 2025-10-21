# ===== Base Image =====
FROM python:3.11-slim

# ===== Install ALL build dependencies (including gperf!) =====
RUN apt-get update && apt-get install -y \
    git \
    cmake \
    g++ \
    make \
    libssl-dev \
    zlib1g-dev \
    gperf \
    && rm -rf /var/lib/apt/lists/*

# ===== Set Workdir =====
WORKDIR /app

# ===== Build telegram-bot-api from source =====
RUN git clone --recursive https://github.com/tdlib/telegram-bot-api.git /tmp/src \
    && cd /tmp/src \
    && git checkout v1.6.3 \
    && mkdir /tmp/build \
    && cd /tmp/build \
    && cmake -DCMAKE_BUILD_TYPE=Release /tmp/src \
    && cmake --build . --target install \
    && rm -rf /tmp/src /tmp/build

# ===== Install Python deps =====
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
