FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY check_seminars.py db.py ./

ENV PYTHONUNBUFFERED=1

# Bot stays online and runs the check every CHECK_INTERVAL minutes (default: 60)
ENV CHECK_INTERVAL=15
ENV DISCORD_PING="@everyone"
ENV LOG_LEVEL="DEBUG"
CMD ["python", "check_seminars.py"]
