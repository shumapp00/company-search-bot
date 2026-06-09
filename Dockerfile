FROM python:3.11-slim

WORKDIR /app

# Копируем ВСЕ файлы проекта
COPY . /app

# Устанавливаем зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Запускаем бота
CMD ["python", "bot.py"]
