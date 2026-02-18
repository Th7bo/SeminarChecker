FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY check_seminars.py db.py ./

ENV PYTHONUNBUFFERED=1

# Run checker in a loop (default: every 60 min). Override with e.g.:
#   docker run ... -e DISCORD_WEBHOOK_URL=... -e CHECK_INTERVAL=30
ENV CHECK_INTERVAL=15
CMD ["sh", "-c", "exec python check_seminars.py --loop --log-level DEBUG --ping \"@everyone\" --interval \"${CHECK_INTERVAL}\""]
