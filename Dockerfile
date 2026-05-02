FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sync.py bot.py ./

ENV DATA_DIR=/data
ENV PYTHONUNBUFFERED=1
CMD ["python", "bot.py"]
