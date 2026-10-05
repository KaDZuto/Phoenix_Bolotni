FROM python:3.11-slim

# Install system dependencies including ffmpeg
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy dependency requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY collector_bot/ collector_bot/
COPY collector_webapp/ collector_webapp/
COPY species/ species/
COPY scripts/ scripts/
COPY model_config.json .

# Create directory for SQLite DB and temporary audio processing
RUN mkdir -p /app/tmp && chmod 777 /app/tmp

EXPOSE 8080

CMD ["python", "-m", "collector_bot.main"]
