FROM python:3.11-slim

WORKDIR /app

# Сначала копируем requirements отдельно (для кэширования)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Потом копируем всё остальное
COPY . .

# Проверяем, что файлы на месте
RUN ls -la /app

# Запускаем бота
CMD ["python", "bot.py"]
