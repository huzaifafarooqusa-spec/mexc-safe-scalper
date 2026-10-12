# Always-on deployment (recommended over GitHub cron for real 5m scalping):
#   docker build -t mexc-scalper .
#   docker run -d --restart=always -e BOT_TOKEN=... -e CHAT_ID=... -v scalper-data:/data mexc-scalper
FROM python:3.12-slim
WORKDIR /app
COPY *.py ./
ENV STATE_FILE=/data/state.json PYTHONUNBUFFERED=1
VOLUME /data
CMD ["python", "scalper.py", "--loop"]
