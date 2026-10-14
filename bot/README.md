# Discord bot

A bot of its own (container `reskate-bot`) next to the rollover and status services. It never restarts
a server; restarts and updates come in a later phase and will only run on empty servers.

* **Status cards** - the same cards as `rollover/status.py`, as a message of the bot in
  `BOT_STATUS_CHANNEL`, edited in place every minute. The message id is kept in the `state` volume.
  (Switch over by emptying `STATUS_WEBHOOK` of `reskate-status` once the bot's cards look right.)
* **Slash commands** (everyone): `/servers`, `/players [server]`, `/top [board]`, `/rank <player> [board]`,
  `/boards`, `/player <player>`, `/stats`. They read the shared leaderboard database and the server logs.

* **Feeds** (phase 2, replaces the webhooks scope by scope): per server in `bot.json` the channels `public`
  (joins, leaves, finished throwdowns, map changes, server up; cleaned up like the public webhook, so no Steam IDs
  or leave reasons), `chat`, `admin` (kicks, bans, anti-cheat, warnings,
  console use) and `log` (every line but chat). A scope without a channel is skipped. Nothing from before the
  bot started is replayed. The webhooks keep running until you remove them from the servers.
* **Console** (admins only): `/say`, `/announce`, `/msg`, `/kick`, `/console` per server, and optionally a
  `console` channel per server whose messages are typed into the console (`"console_chat": true`, needs the
  Message Content intent in the developer portal). Who may: members with a role in `admin_roles`. The bot types
  through the bridge (or the Docker attach API on the host's socket), no change to the game image. `quit`, `stop`, `restart`,
  `shutdown`, `update` and `reload` are refused: restarts are only done on an empty server. Every console use is
  posted to the server's `admin` channel.

## Setup
1. discord.com/developers/applications > New Application > Bot > Reset Token (copy it once).
   No privileged intents are needed in phase 1.
2. Invite: OAuth2 > URL Generator, scopes `bot` and `applications.commands`, permissions View Channels,
   Send Messages, Embed Links, Read Message History.
3. On the host it runs as `bot` in the tools stack ([`stack/`](../stack/README.md)):
   `DISCORD_TOKEN=... BRIDGE_TOKEN=... docker compose -p tools up -d bot`, with `BOT_STATUS_CHANNEL` set in
   `stack/docker-compose.yml` (or `bot.json`, see `bot.example.json`, mounted at `/config/bot.json`).
   The token only lives in the host's environment, never in the repository.
4. With `BRIDGE_URL` / `BRIDGE_TOKEN` (as in the stack) it reaches the servers only through the bridge and
   needs no Docker socket; through the bridge the feeds also get the image's own messages (server up,
   crashes, updates, mods: `admin` and `updates-admin` to the `admin` channel, `public` to `public`).
   Without them it uses the Docker socket as before.

Without `DISCORD_TOKEN` the bot prints the cards it would post and exits (a dry run).

Settings: `BOT_GUILD` (commands appear at once in that server), `BOT_STATUS_CHANNEL`, `STATUS_INTERVAL`
(seconds, default 60), `LEADERBOARD_FILE` (default `/shared/leaderboard.db`), `BOT_CONFIG`, `BOT_STATE`.
The server list comes from `/config/servers.json`, like the rollover. The `/shared` mount (the `reskate-mods` volume) is read-write
only because SQLite's WAL mode needs to open its `-shm` file; the bot opens the database read-only.

## /team
`/team apply|profile|info|list|join|leave|cancel|invite|kick|owner|leader|set|disband|link` (needs the bridge). `/team profile` shows the team's card: logo, description, league, category, leaders, members and stats. Applications are posted into `teams_channel` with Accept/Deny buttons for `admin_roles` members; the decision goes to the bridge, which tells the applicant in game (else a Discord DM). `/team link <code>` ties a Discord account to a player (the code comes from `/team link` in game). `/players` shows who is on with their `[TAG]`, `/top`, `/rank` and the leaderboard channel show tags. See [Teams in the README](../README.md#teams).

## /schedule-add, /schedule-remove, /schedule-list
Admins only. Repeating announcements with an interval per line, kept in `/shared/announcements.json` and sent by the
announcer module (docs/ENVIRONMENT.md, "Scheduled announcements"): `/schedule-add server text every [offset] [chat]`
(`server` is a number or `all`), `/schedule-remove server number`, `/schedule-list [server]`. The bot needs `/shared`
read-write (`ANNOUNCEMENTS_FILE` for another path).

## Busy servers
A console command that is more than talking (not `say`, `announce`, `msg`, `poll`, `players` ...) on a server with more
players than `player_cutoff` (`bot.json` or `BOT_PLAYER_CUTOFF`, default 3, the restart cutoff) first shows a warning with
"Send anyway" / "Cancel" (60 s). `quit`, `stop`, `restart`, `update` stay refused on every server.

## /setting-set, /setting-reset, /setting-list
Admins only. Any documented setting (docs/ENVIRONMENT.md; not the container's identity: ports, Steam token, file
locations), for all servers (`/shared/settings/common.env`) or one (`server-<n>.env`): `/setting-set server key value`
(the key completes as you type), `/setting-reset server key`, `/setting-list [server]`. Passwords and other secrets go
to `secrets/` and are shown as `(secret)`. What a running server can take is typed into its console by the settings
module; the rest (`MAX_PLAYERS`, `MODS`, `MAP` ...) the rollover applies when the server is empty. A value for one
server wins over the one for all.

## /message-set, /message-reset, /message-list
Admins only. The join message (`WELCOME_MESSAGE`) and the Ranked/Unranked texts (`RANKED_MESSAGE`, `UNRANKED_MESSAGE`,
`UNRANKED_TUNING_MESSAGE`, `UNRANKED_SPEED_MESSAGE`), for all servers (`/shared/settings/common.env`) or one
(`server-<n>.env`): `/message-set server message text`. Only these keys are touched. The join text reaches a running
server through the settings module; the Ranked/Unranked texts through the leaderboard module (a server that still runs
its own leaderboard reads them at its next start). `SETTINGS_DIR` for another folder.

## /coach
Admins only. `/coach` asks for the server, then the action (announce, say, message a player, kick, poll,
who is online, any other console command, server info) and then the input in a form. Console actions
show what the server printed back and are posted to the server's `admin` channel. It uses the same
checks as `/say` etc.; nothing in it restarts or updates a server.

## Posts the bot makes by itself
* `leaderboards_channel`: one message per board and league (Ranked and Unranked: All Time, this Month, this Week, every running event), edited in place
  when the points change (looked at every `LEADERBOARD_CHECK` seconds, default 300).
* `updates_channel`: "ReSkate X is out" once per official ReSkate release. The first run only remembers the current version.

`/top`, `/rank`, `/boards`, `/player` and `/stats` take a `league` (Ranked by default, or Unranked, the database
`leaderboard-unranked.db` beside `leaderboard.db`).
