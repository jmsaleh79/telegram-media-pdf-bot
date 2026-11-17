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
      curl \
      unzip \
    && rm -rf /var/lib/apt/lists/* \
    && fc-cache -f

ENV DENO_INSTALL=/usr/local
RUN curl -fsSL https://deno.land/install.sh -o /tmp/deno-install.sh \
    && sh /tmp/deno-install.sh \
    && rm /tmp/deno-install.sh

WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --upgrade pip \
    && python -m pip install --upgrade -r requirements.txt \
    && yt-dlp --version \
    && deno --version
COPY . .

EXPOSE 10000
CMD ["sh", "-c", "uvicorn bot:api --host 0.0.0.0 --port ${PORT:-10000}"]
