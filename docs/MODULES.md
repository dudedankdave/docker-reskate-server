# Modules

Status: Phase A and Phase B (below) are built, and go to main together; the host steps follow the release
([Switching a stack over](#switching-a-stack-over)). Phase C comes in a later release.

**Rule: the slim image stays feature-equal to the default image.** Everything the server does is built in
both (`app/` and `slim/`), and `tests/test_config_parity.py` checks that both write the same config from the
same settings. Only the helper modes (`RESKATE_MODE=rollover`, `bot` ...) need the default image's Python;
they move to the tools image (B) anyway.

Goal: the game servers are restarted only when ReSkate or the server image itself changes. The
leaderboards, the Discord bot and the webhooks can be changed, added or removed while the servers run.

## Today

| Part | Lives in | Changing it means |
|---|---|---|
| Game server, config, updates, mods | image (`app/`, `slim/`) | new image, restart |
| Webhooks (all scopes) | `app/notifier.py`, `slim/notifier.go`, inside the server process | new image, restart |
| Leaderboard (points, DB, in-game `/top` `/rank`) | `app/leaderboard.py`, `slim/leaderboard.go`, inside the server process | new image, restart |
| Rollover, status, bridge, bot, backup | own containers (`RESKATE_MODE`), but from the 172 MB game server image | only that container |

The bot already reads the console from `docker logs` and types through `docker attach`, so it does not
need the server for that. What it cannot see are the messages the notifier makes itself (server up and
join code, crashes, update and mod messages): those only ever go to a webhook.

Also found while reading the repository:

* **Everything twice.** Leaderboard and webhooks exist in Python and in Go (`slim/leaderboard.go` 1409
  lines, `app/leaderboard.py` 862); every change is made and tested twice.
* **The tag only names the ReSkate version.** `release.yml` builds when a new ReSkate release is out (or
  by hand). A merge to `main` does not reach Docker Hub, and a manual rebuild replaces `2.0.7` with
  different content. `VERSION` (2.0.5) is read by nothing.
* **`/shared` and `/mods` are one volume** on the live host (`reskate-mods`): the leaderboard databases and
  `roles.json` sit in the map store. `stack/docker-compose.yml` uses `/root/reskate/shared` instead.
* **Secrets in the container env.** Webhook URLs, Steam tokens and passwords are readable by everyone who can
  open the container in Portainer.
* **Few tests.** Only `slim/leaderboard_test.go`; CI only builds the image. The log parsers (joins, leaves,
  scopes) have none.
* **Two ways to run the helpers.** The live host runs them from `/root/reskate-release/rollover` and
  `/root/reskate-bot-src/bot` (`reskate-bot:local`); `stack/docker-compose.yml` is unused.

## What the ReSkate server itself offers

From the upstream [Server/README.txt](https://github.com/Dingo-Shenanigans/ReSkate/blob/main/Server/README.txt),
[README-roles.txt](https://github.com/Dingo-Shenanigans/ReSkate/blob/main/Server/README-roles.txt) and
[README-linux.md](https://github.com/Dingo-Shenanigans/ReSkate/blob/main/Server/README-linux.md); this shapes the plan:

* **The console is the only interface.** No RCON or HTTP API; "the console (and scripts that talk to it)"
  is how the server is driven, and the log (`ReSkateServer.log`, stdout) is how it reports. So the bridge
  (logs in, `docker attach` out) is the right place for everything outside the server.
* **Nearly every setting changes at run time** from the console and is saved back to `ReSkateServer.json`:
  name, password, welcome, listed, voice, distances, placement, objects, object-scaling, effects, noclip,
  nobail, boosts, tuning, map-pool, rotation, votes, polls, announcements, afk-kick, speed-check,
  board-check, score-check, score-allow, parties, party-size, chat colours, rate, crowd, bone-scale,
  bone-reach. Only a few need a restart: `port`, `query_port`, `steam_token`, `max_players`,
  `use_steam_relay`, `pack_ms`, `threads`.
* **"Don't edit ReSkateServer.json by hand while the server runs"**: it writes its settings back. Live
  changes have to go through the console, never into the file.
* **Reload commands**: `roles reload` (data/roles.json), `words reload` (data/words_*.txt), `maps reload`
  (a map mod copied into Mods can be switched to at once), `park-mod reload`.
* **Maps keep themselves current**: `maps.latest_versions` (our `MAP_LATEST_VERSIONS`) writes Thunderstore's
  newest version into a map's manifest without downloading; players fetch it as they join; the map in play
  is left until nobody is on it.
* **Server votes and polls from the console**: `vote ...`, `poll ...`, `poll-run <command> | ...`, so the bot
  can start them.
* **Roles** (`player`, `vip`, `moderator`, `admin`, `muted`, up to 32, with ranks and permissions) decide who
  may do what in game; the bot's `admin_roles` are separate today.
* **Log lines with a tag** (`[parks]`, `[teleport]`, `[direct]`, `[network]`, activity log): the server's own
  style, which `[event]` lines should follow.

## Target

```
 server image (game only)          tools image (Python + discord.py, no game files)
 +----------------------+          +------------------------------------------------------------------+
 | game server          |  stdout  | bridge  ---> leaderboard ---> webhooks ---> bot      rollover     |
 | console + [event] ---+--------->| logs,        /shared/*.db     Discord        status,  backup       |
 |              stdin <-+----------| HTTP API <-- chat replies     per server     slash,                |
 +----------------------+  attach  |                                              console              |
                                   +------------------------------------------------------------------+
```

| Module | Image, mode | Talks to | Needs |
|---|---|---|---|
| **Game server** | `reskate-server` | stdout/stdin only | `/data`, `/mods`, `/shared` |
| **Bridge** | `reskate-tools`, `bridge` | Docker socket (logs, attach) | the only module with the socket besides the rollover |
| **Leaderboard** | `reskate-tools`, `leaderboard` | bridge | `/shared` (writes the databases) |
| **Webhooks** | `reskate-tools`, `webhooks` | bridge, Discord | `webhooks.json` and the secrets file |
| **Discord bot** | `reskate-tools`, `bot` | bridge, Discord | `/shared` read-only for the boards |
| **Rollover** | `reskate-tools`, `rollover` | Docker socket | unchanged |
| **Backup** | `reskate-tools`, `backup` | `/shared` read-only | unchanged |

`status` goes away (the bot has the cards).

### Single server vs. server stack
* **Single server**: one container as today. Webhooks and leaderboard run inside it from its env vars,
  nothing else to set up.
* **Server stack**: servers + `stack/docker-compose.yml` (rollover, bridge, leaderboard, webhooks, bot,
  backup). The servers hand webhooks and leaderboard over to the modules (below) and only run the game.

## Changes

### A. Leaderboard and webhooks once, as modules
The `leaderboard` and `webhooks` modules are Python, in the tools image: `app/leaderboard.py` already only
needs "a console line in" and "a console command out", the feeds already are line filters
(`public_line`, `chat_line`, ...). Both server images only print `[event]` lines.

Both server images keep their built-in webhooks and leaderboard (slim stays feature-equal), for single
servers and for every stack that does not hand them over. The modules are the one place for a stack's Discord
and leaderboard logic; the built-in parts stay as they are and are switched off per server at run time.

### B. A tools image
`dudedankdave/reskate-tools` (Python, discord.py, no game files) runs bridge, leaderboard, webhooks, bot,
rollover and backup. The server image only runs the server; `RESKATE_MODE` other than `server` keeps working
in it for one more release and logs that it moved. The tools image has its own version and releases, so
the modules can be updated without a new server image.

### C. Tags with a revision
Besides `2.0.7` (moving) every build gets a tag that never moves, `2.0.7-r<n>`, and every merge to `main`
that touches the image builds one, not only a new ReSkate release. `RESKATE_IMAGE_REVISION` in the image
and the `reskate.revision` label show what runs; the rollover rolls a new revision out like a new ReSkate
release (on empty servers, or a copy while players are on). `VERSION` goes.

### D. A volume of its own for /shared
`reskate-shared` at `/shared` (settings, secrets, databases, `roles.json`, `modules.json`), apart from the
map store at `/mods`. The rollover's `shared_volume` defaults to it, the backup reads only it.

### E. Secrets apart from settings
`/shared/settings/secrets/common.env` and `secrets/server-<n>.env` for `SERVER_PASSWORD`, webhook URLs and
bot tokens, read like the settings layers but never logged (the `[settings]` line names keys, not values),
readable by the servers' uid only. The rollover leaves keys the files set out of the containers it creates, so
secrets leave the container env over time. `STEAM_TOKEN` stays per container: each slot needs its own.

### F. Tests and lint in CI
Unit tests for the log parsers (bot feeds, bridge scopes, leaderboard), for env to `ReSkateServer.json` in
the entrypoint and for the settings layers; `go test` for `slim/`; `ruff` and `go vet`. They run on every
pull request next to the image build.

### H. Settings live, through the console
The settings layers (below) are applied at start as today, and while the server runs by a `settings`
step of the bridge: when `common.env` or `server-<n>.env` changes, it works out what changed and types the
matching console commands (`welcome ...`, `map-pool add ...`, `announcements add ...`, `score-check kick`,
...) into each affected server. The server saves them to its own file as it does for an admin. Only the
restart-only keys (`PORT`, `QUERY_PORT`, `STEAM_TOKEN`, `MAX_PLAYERS`, relay, `pack_ms`, `threads`) wait for
the rollover to recreate the server once it is empty.

### I. Roles, words and maps without restarts
* Shared `roles.json`: after a change (or a `role` command on one server) the bridge sends `roles reload` to
  every server sharing it; today the others only see it at their next start.
* `words_filtered.txt` / `words_blocked.txt` shared through `/shared` like `roles.json` (`WORDS_SCOPE`),
  with `words reload`.
* New maps from `MODS`: installed into the map store and linked into `Mods`, then `maps reload`, instead of
  a restart. Map versions follow `MAP_LATEST_VERSIONS`; the rollover's mod-update restart stays only for
  mods that are not maps.

### J. The kick announcer as a module
`app/announcer.py` (kick and ban lines in chat) only reads the log and types `say`: it moves to the tools
image with the leaderboard, the server keeps it for single servers (like A).

### G. One way to run the stack
`stack/docker-compose.yml` with the tools image replaces `rollover/docker-compose.yml`,
`bot/docker-compose.yml`, `bot/Dockerfile`, `rollover/Dockerfile` and `reskate-status`.

## Settings in a stack: shared or per server

Today every server carries all its settings as env vars, and the rollover clones them into each new
container. The six live servers repeat the same welcome message, announcements, admins, map pool and mod list;
changing one of them means changing every container.

In a stack the settings come in layers, each one overriding the one before:

| Layer | Where | For |
|---|---|---|
| 1. Image defaults | the image | |
| 2. Container env | `environment:` / `env_file:` | identity only in a stack: `PORT`, `QUERY_PORT`, `STEAM_TOKEN`, `RESKATE_SERVER` (the number); the files cannot set them. A single server keeps everything here, as today. |
| 3. Shared | `/shared/settings/common.env` | what every server in the stack has: welcome, announcements, admins, bans, voice, tuning ... |
| 3b. | `/shared/settings/secrets/common.env` | shared secrets: passwords, webhook URLs (E) |
| 4. Own | `/shared/settings/server-<n>.env` | what only this server has: name, map, map pool, mods, max players, ranked checks ... |
| 4b. | `/shared/settings/secrets/server-<n>.env` | its own secrets (E) |

* A key in `server-<n>.env` is that server's own value; a key that is not there is the shared one. So each
  setting is shared or own per server, without a list of which is which.
* `KEY+=value` appends to the list of the layers before (`MODS`, `MAP_POOL`, `ADMINS` with commas,
  `ANNOUNCEMENTS` with `|`, JSON lists like `COMMANDS`) instead of replacing it.
* Same `KEY=value` format as `example.env`, so existing env files can be moved over as they are.
* The entrypoint applies them at start (as it does with the env vars today), logs which keys came from which
  file (`[settings] server-4.env: MAP, MAX_PLAYERS`) and a digest of them.
* Phase A: when a server's files change (its digest), the rollover restarts it once it is empty. Phase B (H):
  the change is typed into its console instead, and only restart-only keys wait for an empty server.
* `RESKATE_MODE=split-settings` writes today's containers' env out as these files.
* `roles.json` (`ROLES_SCOPE`) and the leaderboard (`LEADERBOARD_SCOPE`) already follow the same idea:
  `shared` by default, `server` for an own copy.

The modules use the same pattern: `webhooks.json` and `bot.json` have `defaults` and per-server entries.

## The server's contract (the part that needs the one restart)

1. **Events on stdout.** Every message the notifier makes in the `admin`, `public`, `updates` and
   `updates-admin` scopes is also printed as one line, `[event] <scope> {"ping":false,"text":"..."}`, whether a
   webhook is set or not (the same bytes from both images). The console lines are already in the log, and the
   leaderboard's own posts come from its database. So the bridge sees everything a webhook would get.
2. **Hand-over at run time.** The server reads `/shared/modules.json` every minute, e.g.
   `{"leaderboard": "external", "webhooks": {"public": "external"}, "servers": {"6": {...}}}`. Parts:
   `leaderboard`, `announcer`, `webhooks` (whole or per scope). `external` switches its own part off; a missing
   file or `internal` keeps today's behaviour. Switching back and forth needs no restart and nothing counts twice.
3. **`/shared` on every server** (D; today missing on server 1).
4. **Settings layers and secrets** read by the entrypoint (above, E).
5. **Revision** in the image (C).

The Python image and `slim/` both get all five.

Decided: the `[event]` lines are always on, in the server's tagged style with the scope readable first; slim
keeps everything the default image has.

Also in Phase A: every ReSkate server setting from upstream `Server/README.txt` has an env var
(`BOARD_CHECK`, `GLOBAL_WORD_FILTER`, `WORDS_FILTERED`, `WORDS_BLOCKED`, `PARK_MODS`, `PARK_LIMIT_*` were missing),
slim shares `roles.json` like the default image (it did not), and `config` writes the config and exits.

## Order

**Phase A, before the next release to main** (the servers restart once with it), done:
1. `[event]` lines (`Hook.send` in `app/notifier.py`, the same spot in `slim/notifier.go`).
2. `/shared/modules.json` hand-over for the leaderboard and the webhooks.
3. Settings layers and `secrets.env` in the entrypoint (Python and Go); the rollover recreates empty servers
   whose restart-only settings changed.
4. Revision tags: `release.yml` builds on merges to `main`, `RESKATE_IMAGE_REVISION`, rollover rolls revisions out.
5. Bridge: parses `[event]` lines into events with their scope; `/v1/servers` with slot, map, players.
6. Tests and lint in CI (F), first for everything touched above.
7. Docs, `stack/docker-compose.yml` with `reskate-shared`, `split-settings` (today's container env out as
   `common.env`, `server-<n>.env` and `secrets/`). The rollover's `shared_volume` keeps its default (none): on a
   live host it is set to the volume the servers already share, so no server ends up with a second database.

**Phase B, the modules (no server restart needed to use them)**, done:
1. Tools image `dudedankdave/reskate-tools` (`stack/Dockerfile`, `modules/tools.py`) and the stack on it
   (`stack/docker-compose.yml`); `reskate-status` and the separate bot/rollover compose files and Dockerfiles are gone.
2. `leaderboard` module (`modules/mod_leaderboard.py`): `app/leaderboard.py` per server and slot, fed from the
   bridge, answering through it, posting the boards to the server's leaderboard (else public) webhooks.
3. `webhooks` module (`modules/mod_webhooks.py`): the server's own `WEBHOOK_URL_<n>` / `WEBHOOK_SCOPE_<n>` from
   its settings files (no separate `webhooks.json`: the same variables, in `secrets/`), the notifier's feeds.
4. Bot through the bridge (`bot/backend.py`, `BRIDGE_TOKEN` set): no Docker socket; its feeds also get the
   image's own messages.
5. `settings` module (H, `modules/mod_settings.py`): changes typed into running servers, `roles reload` when
   `/shared/roles.json` changes (I); the `announcer` module (J).
6. Bridge: events with slot, `/v1/servers/<n>/settings` (no secrets), `/v1/status`, module calls audit-only.

7. New `MODS` entries without a restart (I): the settings module types `mods add <new packages>` first (the
   server installs, links and runs `maps reload`), waits for "[mods] done", then sends the rest (map pool ...).
   A package that leaves `MODS` stays linked until the next restart. The rollover accepts the restart digest
   of older server images too (`settings.restart_digests`), so mixed images do not restart in a loop; roll
   the tools image (settings module, rollover) out before the server image.

Not (yet) done from H/I: `WORDS_FILTERED` / `WORDS_BLOCKED`, `COMMANDS`, `VOTES_CUSTOM`,
`PARK_MODS` and the network settings have no console command, so they wait for a restart when empty.

Limits of the modules:
* The leaderboard module serves servers with `LEADERBOARD_SCOPE=shared` (the database in `/shared`); one with
  its own board in `/data` keeps it inside. Hand a server over while it is empty: players already on count from
  their next join.
* The webhooks module needs the URLs in the settings files (`split-settings` puts them in `secrets/`). The
  titled answer block of an info command typed into the console stays an in-server feature.

**Phase C, a later release, announced beforehand:**
1. `RESKATE_MODE` other than `server` and `config` removed from the server image (B): the tools image has them.
2. The bot's Docker-socket backend removed (the bridge is the one way).

## What users have to change

**Single server (default image): nothing.** Env vars, webhooks and the built-in leaderboard keep working, the
older `DISCORD_WEBHOOK_*` / `LEADERBOARD_WEBHOOK` names too. New are the `[event]` lines in the log, the
revision tags and the new settings. Only values ReSkate does not take now stop the start with a clear message
instead of reaching the server: `PARTY_SIZE` outside 2-8, `MAP_LATEST_CHECK_MINUTES` below 5.

**Single server (slim image): nothing.** Same as the default image.

## Switching a stack over

**Server stack:**
1. Create `reskate-shared`, mount it at `/shared` on every server, move `leaderboard*.db` and `roles.json` over
   from the map volume (or keep using the volume the servers already share at `/shared`: give `reskate-shared`
   in `stack/docker-compose.yml` its `name:`).
2. `split-settings --write` writes `common.env`, `server-<n>.env` and `secrets/` from today's containers; check
   them. Give every server `RESKATE_SERVER` (the rollover does for the containers it creates).
3. Move the helpers to `stack/docker-compose.yml` (tools image), set `BRIDGE_TOKEN`.
4. Bot without the Docker socket: bridge URL and token; `bot.json` with `defaults` and per-server entries.
5. Start the stack (`stack/README.md`). The modules do nothing until `modules.json` hands a part over.
6. Per server, while it is empty, hand parts over in `/shared/modules.json` (`settings`, `webhooks` scope by
   scope, `leaderboard`, `announcer`) and check its channels and logs; back to `internal` (or remove the entry)
   at any time. Once the webhooks run outside, the `WEBHOOK_*` keys can leave the container env (the rollover
   leaves them out of new containers by itself).
7. Containers that start the helpers from the server image (`RESKATE_MODE=bot` etc.): switch to the tools image
   before Phase C.
