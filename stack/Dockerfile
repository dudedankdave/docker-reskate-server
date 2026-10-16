# syntax=docker/dockerfile:1
#
# The tools image, dudedankdave/reskate-tools: everything that runs next to the game servers (bridge,
# rollover, Discord bot, leaderboard, webhooks, settings, announcer, backup), one per container by
# RESKATE_MODE (see modules/tools.py). No game files; the servers run dudedankdave/reskate-server.
# Build from the repository root: docker build -f stack/Dockerfile -t dudedankdave/reskate-tools .
FROM python:3.13-alpine
ARG REVISION=dev
LABEL org.opencontainers.image.title="reskate-tools" \
      org.opencontainers.image.revision="$REVISION" \
      org.opencontainers.image.source="https://github.com/dudedankdave/docker-reskate-server"
RUN pip install --no-cache-dir "discord.py>=2.4,<3" \
 && adduser -D -u 1000 reskate
# The helpers, flat in /app; the image's server-side code they share in /app/lib
COPY modules/*.py bot/*.py rollover/rollover.py rollover/status.py rollover/bridge.py \
     stack/dbbackup.py stack/split_settings.py /app/
COPY app/leaderboard.py app/teams.py app/settings.py app/handover.py app/notifier.py app/updater.py app/announcer.py /app/lib/
COPY rollover/servers.example.json /config/servers.json
COPY bot/bot.example.json /config/bot.example.json
ENV PYTHONUNBUFFERED=1 RESKATE_TOOLS_REVISION=$REVISION
WORKDIR /app
# root for the Docker socket (bridge, rollover); the others run as uid 1000 (compose: user), like the
# servers, so what they write to /shared stays the servers' too.
ENTRYPOINT ["python3", "-I", "/app/tools.py"]
