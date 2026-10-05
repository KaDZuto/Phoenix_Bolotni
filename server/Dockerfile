# Phoenix_Bolotni — серверный распознаватель (Задача 7, td3.md раздел «Развёртывание»).
#
# Контекст сборки — корень репозитория, а не `server/`: образу нужен и код
# сервера, и репозиторий модели (`inference.py`, `model_config.json`,
# `models/bird_model.onnx`, `species/ru_birds_whitelist.json`), который
# подключается как внешняя библиотека, а не копируется в `server/`.
#
#   docker build -f server/Dockerfile -t phoenix-server .

# --- стадия зависимостей -------------------------------------------------------
# Слой со сборкой тяжёлых пакетов кэшируется отдельно от кода: правка сервера
# не должна заставляет заново качать torch и onnxruntime (это минуты и сотни МБ).
FROM python:3.11-slim AS deps

# libsndfile нужен soundfile (декодирование чанков), ffmpeg — для mp3/m4a.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libsndfile1 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
COPY server/requirements.txt /app/server-requirements.txt

RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir -r server-requirements.txt


# --- стадия приложения ---------------------------------------------------------
FROM python:3.11-slim AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends libsndfile1 ffmpeg curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 phoenix

WORKDIR /app

COPY --from=deps /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin

# Код сервера и код модели в одной плоскости: `server/config.py` ищет
# `model_config.json` на два уровня выше `server/`.
COPY server/ /app/server/
COPY inference.py audio_utils.py model_config.json /app/
COPY models/ /app/models/
COPY species/ /app/species/

# Каталог для отладочного аудио и локальной SQLite-БД. Владельцем — непривилегированный
# пользователь: процесс не должен иметь прав на запись в код и конфигурацию.
RUN mkdir -p /app/server/data /app/server/debug_audio \
    && chown -R phoenix:phoenix /app/server/data /app/server/debug_audio

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PHOENIX_MODEL_CONFIG=/app/model_config.json \
    PHOENIX_CLASSES=/app/models/classes.json \
    PHOENIX_WHITELIST=/app/species/ru_birds_whitelist.json \
    PHOENIX_MODEL=/app/models/bird_model.onnx

USER phoenix

EXPOSE 8000

# Проверка живости в самом контейнере: `curl` стоит сюда специально для этого.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

# Команда по умолчанию — API. Воркер (`python -m server.queue_worker`) и мониторинг
# разрывов запускаются отдельными сервисами через `command:` в docker-compose.
CMD ["uvicorn", "server.ingest:app", "--host", "0.0.0.0", "--port", "8000"]