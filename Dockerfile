FROM python:3.12-slim

WORKDIR /app

# Build dependencies for cryptography (used by asyncua security modes)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libssl-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY templates/ ./templates/
COPY static/ ./static/
COPY scripts/ ./scripts/
COPY README.md .

# Download vendored JS libraries (HTMX, ECharts) for offline operation
RUN python scripts/download_vendor.py

# Config and data directories are Docker volume mount points
RUN mkdir -p /config /data

EXPOSE 8000

ENV APP_CONFIG_DIR=/config \
    APP_DATA_DIR=/data \
    APP_HOST=0.0.0.0 \
    APP_PORT=8000 \
    PYTHONPATH=/app

VOLUME ["/config", "/data"]

CMD ["sh", "-c", "uvicorn app.main:app --host ${APP_HOST} --port ${APP_PORT}"]
