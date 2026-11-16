FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY bot.py main.py db.py webapp.py tgauth.py secretbox.py migrate_to_postgres.py config.env ./
COPY webapp ./webapp
# PORT задаёт хостинг; WEBHOOK_BASE_URL включает режим вебхука
ENV PORT=8080
EXPOSE 8080
CMD ["python", "bot.py"]
