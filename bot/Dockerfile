# The Discord bot (see README.md). Build from the repository root:
#   docker build -f bot/Dockerfile -t reskate-bot:local .
FROM python:3.13-alpine
RUN pip install --no-cache-dir "discord.py>=2.4,<3"
COPY bot/bot.py /app/bot.py
COPY bot/console.py bot/feeds.py bot/coach.py bot/posts.py /app/lib/
COPY rollover/rollover.py rollover/status.py app/leaderboard.py /app/lib/
COPY rollover/servers.example.json /config/servers.json
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python3", "-I", "/app/bot.py"]
