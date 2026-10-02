# Video agent web page: FastAPI + uvicorn, no video tools. Build context is the repository root (see .railway/railway.ts),
# because the page shares worker/storage.py with the worker. Base image pinned by digest, the same one as the worker.
FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e

RUN useradd --create-home --uid 10001 app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONNOUSERSITE=1
WORKDIR /app

COPY web/requirements.txt /app/web/requirements.txt
RUN pip install --no-cache-dir -r /app/web/requirements.txt

COPY worker/storage.py /app/worker/storage.py
COPY worker/config.json /app/worker/config.json
COPY worker/models.py /app/worker/models.py
COPY web /app/web
RUN chown -R root:root /app && chmod -R a-w /app
USER app
WORKDIR /app/web
CMD ["python", "app.py"]
