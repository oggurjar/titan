# ── Base: Python 3.11 slim ───────────────────────────────────────────────────
FROM python:3.11-slim

# Install Node.js 20 (needed for pika_check.js / mineflayer)
RUN apt-get update && apt-get install -y curl && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Node deps
COPY package.json package-lock.json ./
RUN npm ci --omit=dev

# Copy rest of app
COPY . .

# Render injects PORT; default to 10000 (Render's expected port)
ENV PORT=10000
ENV RENDER=true

# Data directory — mount a persistent disk at /data in Render dashboard
ENV DATA_DIR=/data

EXPOSE $PORT

CMD gunicorn app:app \
    --workers 2 \
    --threads 4 \
    --bind 0.0.0.0:$PORT \
    --timeout 120 \
    --access-logfile - \
    --error-logfile -
