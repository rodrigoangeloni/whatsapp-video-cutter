FROM python:3.10-slim

LABEL org.opencontainers.image.title="WhatsApp Video Cutter" \
      org.opencontainers.image.version="1.0.0" \
      org.opencontainers.image.description="Cortador y compresor visual de videos para WhatsApp (< 180 MB)"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY whatsapp_cutter.py /app/whatsapp_cutter.py
COPY assets /app/assets

ENV WA_HOST=0.0.0.0 \
    WA_PORT=8765 \
    WA_VIDEOS_ROOT=/videos \
    WA_DEFAULT_FOLDER=. \
    WA_IN_DOCKER=1 \
    PYTHONUNBUFFERED=1

EXPOSE 8765

VOLUME ["/videos"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import os, urllib.request; port = os.environ.get('WA_PORT', '8765'); urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health', timeout=3)" || exit 1

CMD ["python", "whatsapp_cutter.py", "--no-browser"]
