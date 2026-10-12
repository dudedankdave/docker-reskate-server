# The helper services in one container (see README.md). Build from the repository root:
#   docker build -f stack/Dockerfile -t reskate-tools:local .
FROM python:3.13-alpine
RUN pip install --no-cache-dir "discord.py>=2.4,<3"
COPY rollover/rollover.py rollover/status.py bot/bot.py stack/launcher.py /app/
COPY rollover/rollover.py rollover/status.py app/leaderboard.py /app/lib/
COPY rollover/servers.example.json /config/servers.json
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python3", "-I", "/app/launcher.py"]
