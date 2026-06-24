# Imagem única p/ os dois serviços Cloud Run (webhook e worker).
# Escolha o alvo via APP_MODULE: bot.webhook:app (padrão) ou bot.worker:app.
FROM python:3.12-slim

# Deps de sistema p/ mídia: ffmpeg (vp9/opus) + exiftool.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libimage-exiftool-perl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot/ ./bot/

ENV APP_MODULE=bot.webhook:app \
    PORT=8080 \
    PYTHONUNBUFFERED=1

# --workers 1: estado por-processo (igual sabiá/amora). Concorrência vem de threads/instâncias.
CMD exec uvicorn ${APP_MODULE} --host 0.0.0.0 --port ${PORT} --workers 1
