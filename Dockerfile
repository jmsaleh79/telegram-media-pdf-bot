FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg \
      libreoffice-writer \
      libreoffice-calc \
      fonts-noto-core \
      fonts-liberation \
      ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && fc-cache -f

WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --upgrade pip && python -m pip install -r requirements.txt
COPY . .

EXPOSE 10000
CMD ["sh", "-c", "uvicorn bot:api --host 0.0.0.0 --port ${PORT:-10000}"]
