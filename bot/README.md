# Discord bot

A bot of its own (container `reskate-bot`) next to the rollover and status services. It never restarts
a server; restarts and updates come in a later phase and will only run on empty servers.

* **Status cards** - the same cards as `rollover/status.py`, as a message of the bot in
  `BOT_STATUS_CHANNEL`, edited in place every minute. The message id is kept in the `state` volume.
  (Switch over by emptying `STATUS_WEBHOOK` of `reskate-status` once the bot's cards look right.)
* **Slash commands** (everyone): `/servers`, `/players [server]`, `/top [board]`, `/rank <player> [board]`,
  `/boards`, `/player <player>`, `/stats`. They read the shared leaderboard database and the server logs.

* **Feeds** (phase 2, replaces the webhooks scope by scope): per server in `bot.json` the channels `public`
  (joins, leaves, finished throwdowns, map changes), `chat`, `admin` (kicks, bans, anti-cheat, warnings,
  console use) and `log` (every line but chat). A scope without a channel is skipped. Nothing from before the
  bot started is replayed. The webhooks keep running until you remove them from the servers.
* **Console** (admins only): `/say`, `/announce`, `/msg`, `/kick`, `/console` per server, and optionally a
  `console` channel per server whose messages are typed into the console (`"console_chat": true`, needs the
  Message Content intent in the developer portal). Who may: members with a role in `admin_roles`. The bot types
  through the Docker attach API on the host's socket, no change to the game image. `quit`, `stop`, `restart`,
  `shutdown`, `update` and `reload` are refused: restarts are only done on an empty server. Every console use is
  posted to the server's `admin` channel.

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
The server list comes from `/config/servers.json`, like the rollover. The `/shared` mount (the `reskate-mods` volume) is read-write
only because SQLite's WAL mode needs to open its `-shm` file; the bot opens the database read-only.

## /coach
Admins only. `/coach` asks for the server, then the action (announce, say, message a player, kick, poll,
who is online, any other console command, server info) and then the input in a form. Console actions
show what the server printed back and are posted to the server's `admin` channel. It uses the same
checks as `/say` etc.; nothing in it restarts or updates a server.
