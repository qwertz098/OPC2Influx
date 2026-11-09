# --- Build stage: collect every requirement as a wheel. The compiler is only
# used if some package has no binary wheel for the target platform, and it
# never reaches the final image.
FROM python:3.12-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libssl-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

# --- Runtime stage ---
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
# Bind-mount the wheels instead of copying them, so they add no image layer
RUN --mount=type=bind,from=builder,source=/wheels,target=/wheels \
    pip install --no-cache-dir --no-index --find-links /wheels -r requirements.txt \
    && pip uninstall -y pip

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

# MALLOC_ARENA_MAX: glibc otherwise keeps a malloc arena per thread, which costs
# RAM on small devices for no gain here
ENV APP_CONFIG_DIR=/config \
    APP_DATA_DIR=/data \
    APP_HOST=0.0.0.0 \
    APP_PORT=8000 \
    PYTHONPATH=/app \
    MALLOC_ARENA_MAX=2

VOLUME ["/config", "/data"]

# Release shown on the settings page; the release workflow passes the tag
ARG APP_RELEASE=dev
ENV APP_RELEASE=${APP_RELEASE}

# exec: uvicorn replaces the shell and receives the stop signal itself
CMD ["sh", "-c", "exec uvicorn app.main:app --host ${APP_HOST} --port ${APP_PORT}"]
