# Discord bot

A bot of its own (container `reskate-bot`) next to the rollover and status services. Phase 1 is
read-only: it restarts nothing and changes nothing.

* **Status cards** - the same cards as `rollover/status.py`, as a message of the bot in
  `BOT_STATUS_CHANNEL`, edited in place every minute. The message id is kept in the `state` volume.
  (Switch over by emptying `STATUS_WEBHOOK` of `reskate-status` once the bot's cards look right.)
* **Slash commands** (everyone): `/servers`, `/players [server]`, `/top [board]`, `/rank <player> [board]`,
  `/boards`, `/player <player>`, `/stats`. They read the shared leaderboard database and the server logs.

## Setup
1. discord.com/developers/applications > New Application > Bot > Reset Token (copy it once).
   No privileged intents are needed in phase 1.
2. Invite: OAuth2 > URL Generator, scopes `bot` and `applications.commands`, permissions View Channels,
   Send Messages, Embed Links, Read Message History.
3. On the host, from this folder: `DISCORD_TOKEN=... docker compose up -d --build`
   with `BOT_STATUS_CHANNEL` set in `docker-compose.yml` (or `bot.json`, see `bot.example.json`).
   The token only lives in the host's environment, never in the repository.

Without `DISCORD_TOKEN` the bot prints the cards it would post and exits (a dry run).

Settings: `BOT_GUILD` (commands appear at once in that server), `BOT_STATUS_CHANNEL`, `STATUS_INTERVAL`
(seconds, default 60), `LEADERBOARD_FILE` (default `/shared/leaderboard.db`), `BOT_CONFIG`, `BOT_STATE`.
The server list comes from `/config/servers.json`, like the rollover. The `/shared` mount is read-write
only because SQLite's WAL mode needs to open its `-shm` file; the bot opens the database read-only.
