FROM python:3.12-slim

WORKDIR /app

# System deps for google-auth and audio processing
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ops/ ./ops/

# Log dir and scheduler db persist via volume mount — create the dir so it exists at startup
RUN mkdir -p /app/ops/log

CMD ["python", "ops/bot.py"]
