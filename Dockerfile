
FROM ubuntu:24.04
WORKDIR /app

ENV DEBIAN_FRONTEND=noninteractive

# Install Python 3.12, FFmpeg 6.1.1 (Ubuntu 24.04 official), and other deps
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    python3.12 \
    python3.12-dev \
    python3-pip \
    ffmpeg \
    git && \
    rm -rf /var/lib/apt/lists/*

# Make python3.12 the default python/python3
RUN ln -sf /usr/bin/python3.12 /usr/bin/python && \
    ln -sf /usr/bin/python3.12 /usr/bin/python3

# requirements.lock is the frozen resolution of requirements.txt (transitive
# dependencies included), so every rebuild installs the exact same set.
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock --break-system-packages

COPY . .

# Web UI (optional, configurable through WEBUI_PORT)
EXPOSE 8080

# Reports unhealthy when the panel stops answering /healthz (the bot can be
# "running" with a dead web app). Skipped automatically when WEBUI_ENABLED=false.
HEALTHCHECK --interval=30s --timeout=6s --start-period=45s --retries=3 \
    CMD python tools/healthcheck.py || exit 1

CMD ["python", "playify.py"]
