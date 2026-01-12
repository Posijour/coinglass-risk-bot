FROM python:3.11-slim

# Чтобы логи сразу выводились, а не копились до конца времён
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Системные зависимости для aiohttp
RUN apt-get update && apt-get install -y \
    gcc \
    build-essential \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Сначала зависимости, чтобы кеш работал нормально
COPY requirements.txt .

RUN pip install --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Потом код
COPY . .

CMD ["python", "bot.py"]
