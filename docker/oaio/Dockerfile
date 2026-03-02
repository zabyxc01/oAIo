FROM python:3.10-slim

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl gcc libsndfile1 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt aiofiles

# App code
COPY backend/ ./backend/
COPY frontend/ ./frontend/
COPY config/ ./config/

# Templates dir (created if not mounted)
RUN mkdir -p /app/templates

COPY docker/oaio/start.sh /start.sh
RUN chmod +x /start.sh

EXPOSE 9000 8002

HEALTHCHECK --interval=30s --timeout=15s --retries=3 \
  CMD curl -f http://localhost:9000/vram || exit 1

CMD ["/start.sh"]
