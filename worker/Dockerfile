# Video agent worker: ffmpeg + Whisper (CPU) + HyperFrames (headless Chrome) + the Python pipeline.
# Build context is the repository root (Railway: root directory "/", RAILWAY_DOCKERFILE_PATH=worker/Dockerfile).
# Base images are pinned by digest; update them deliberately.

FROM node:22-bookworm-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c AS node

FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e

# Node 22 from the official image (exact same build as the pinned digest above)
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && node --version && npm --version

# ffmpeg, and the libraries headless Chrome needs
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg ca-certificates fonts-liberation \
      libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
      libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 libpango-1.0-0 libcairo2 libdbus-1-3 libexpat1 \
      libx11-6 libxcb1 libxext6 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 app
ENV HOME=/home/app \
    HF_HOME=/app/hf-cache \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONNOUSERSITE=1 \
    DO_NOT_TRACK=1 HF_CLI_TELEMETRY_DISABLED=1 HYPERFRAMES_NO_TELEMETRY=1 \
    NPM_CONFIG_UPDATE_NOTIFIER=false HF_HUB_DISABLE_TELEMETRY=1 \
    WORK_ROOT=/tmp/va-jobs \
    HYPERFRAMES_DIR=/app/hyperframes
WORKDIR /app

COPY worker/requirements.txt /app/worker/requirements.txt
RUN pip install --no-cache-dir -r /app/worker/requirements.txt

# Pinned HyperFrames and its pinned Chrome, installed as the app user
COPY worker/hyperframes/package.json worker/hyperframes/package-lock.json /app/hyperframes/
RUN mkdir -p /app/hf-cache /tmp/va-jobs && chown -R app:app /app /tmp/va-jobs
USER app
ENV NO_COLOR=1 FORCE_COLOR=0
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
# The build fails here unless the pinned Chrome really exists and runs.
RUN cd /app/hyperframes && npm ci --no-audit --no-fund \
 && npx hyperframes browser ensure \
 && CHROME="$(npx hyperframes browser path | tail -n 1)" \
 && test -x "$CHROME" \
 && "$CHROME" --version \
 && echo "REAL_CHROME=\"$CHROME\"" > /tmp/real-chrome.env \
 && cat /tmp/real-chrome.env

# Whisper model baked into the image; no downloads at run time
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('medium.en', device='cpu', compute_type='int8')"

USER root
# The model and the HyperFrames install become read-only: the Python process that holds the keys loads the model,
# so nothing the worker runs (ffmpeg, Chrome) may be able to replace it.
RUN chown -R root:root /app/hf-cache /app/hyperframes && chmod -R a-w /app/hf-cache /app/hyperframes
COPY worker/docker /app/docker
RUN mv /tmp/real-chrome.env /app/docker/real-chrome.env \
 && chmod 0755 /app/docker/chrome-offline.sh && chmod 0644 /app/docker/real-chrome.env
COPY worker /app/worker
COPY db /app/db
RUN chown -R root:root /app/worker /app/db /app/docker && chmod -R a-w /app/worker /app/db /app/docker
USER app

ENV HYPERFRAMES_BROWSER_PATH=/app/docker/chrome-offline.sh \
    HYPERFRAMES_FONT_CACHE_DIR=/tmp/hf-fonts \
    NPM_CONFIG_CACHE=/tmp/npm-cache \
    HF_HUB_OFFLINE=1 \
    MIGRATIONS_DIR=/app/db/migrations
WORKDIR /app/worker
CMD ["python", "jobs.py"]
