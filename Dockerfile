FROM python:3.11-slim

WORKDIR /app

# Установка системных утилит, включая FFmpeg
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Таймауты и число повторов pip для стабильной сборки на любых серверах
ENV PIP_DEFAULT_TIMEOUT=120 PIP_RETRIES=10 PYTHONUNBUFFERED=1

COPY requirements.txt .

# Установка зависимостей Python
RUN pip install --no-cache-dir --upgrade pip && \
    (pip install --no-cache-dir -r requirements.txt || \
     (sleep 5 && pip install --no-cache-dir -r requirements.txt))

# Копируем код приложения
COPY . .

# Создаем директорию для временных файлов
RUN mkdir -p /app/temp && chmod 777 /app/temp

# Запуск бота
CMD ["python", "bot.py"]
