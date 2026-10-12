# Modules (proposal)

Goal: the game servers are restarted only when ReSkate or the server image itself changes. The
leaderboards, the Discord bot and the webhooks can be changed, added or removed while the servers run.

## Today

| Part | Lives in | Changing it means |
|---|---|---|
| Game server, config, updates, mods | image (`app/`, `slim/`) | new image, restart |
| Webhooks (all scopes) | `app/notifier.py`, `slim/notifier.go`, inside the server process | new image, restart |
| Leaderboard (points, DB, in-game `/top` `/rank`) | `app/leaderboard.py`, `slim/leaderboard.go`, inside the server process | new image, restart |
| Rollover, status, bridge, bot | own containers (`RESKATE_MODE`) | only that container |

The bot already reads the console from `docker logs` and types through `docker attach`, so it does not
need the server for that. What it cannot see are the messages the notifier makes itself (server up and
join code, crashes, update and mod messages): those only ever go to a webhook.

## Target

```
                      +--------------------------- one image, RESKATE_MODE=... ----------------------------+
 game server(s)       |  bridge              leaderboard          webhooks            bot           rollover |
 stdout: console  --->|  follows logs,  ---> events in,      ---> events in,    ---> events in,              |
 + [event] lines      |  HTTP API            /shared/*.db         Discord webhooks    status, slash,          |
 stdin: console   <---|  actions out   <---  chat replies         per server          console, /coach        |
                      +----------------------------------------------------------------------------------------+
```

| Module | Mode | Talks to | Needs |
|---|---|---|---|
| **Game server** | `server` | stdout/stdin only | `/data`, `/mods`, `/shared` |
| **Bridge** | `bridge` | Docker socket (logs, attach) | the only module with the socket besides the rollover |
| **Leaderboard** | `leaderboard` | bridge | `/shared` (writes the databases) |
| **Webhooks** | `webhooks` | bridge, Discord | a config file with the webhook URLs per server and scope |
| **Discord bot** | `bot` | bridge, Discord | `/shared` read-only for the boards |
| **Rollover** | `rollover` | Docker socket | unchanged |

`status` goes away (the bot has the cards).

### Single server vs. server stack
* **Single server**: one container as today. Webhooks and leaderboard run inside it from its env vars,
  nothing else to set up.
* **Server stack**: servers + `stack/docker-compose.yml` (rollover, bridge, leaderboard, webhooks, bot).
  The servers hand webhooks and leaderboard over to the modules (below) and only run the game.

## Settings in a stack: shared or per server

Today every server carries all its settings as env vars, and the rollover clones them into each new
container. The six live servers repeat the same welcome message, announcements, admins, map pool and mod list;
changing one of them means changing every container.

In a stack the settings come in layers, each one overriding the one before:

| Layer | Where | For |
|---|---|---|
| 1. Image defaults | the image | |
| 2. Container env | `environment:` / `env_file:` | identity only in a stack: `PORT`, `QUERY_PORT`, `STEAM_TOKEN`, `RESKATE_SERVER` (the number). A single server keeps everything here, as today. |
| 3. Shared | `/shared/settings/common.env` | what every server in the stack has: welcome, announcements, admins, bans, voice, tuning ... |
| 4. Own | `/shared/settings/server-<n>.env` | what only this server has: name, map, map pool, mods, max players, ranked checks ... |

* A key in `server-<n>.env` is that server's own value; a key that is not there is the shared one. So each
  setting is shared or own per server, without a list of which is which.
* `KEY+=value` in the own file appends to a shared list (`MODS`, `MAP_POOL`, `ADMINS`, `ANNOUNCEMENTS`,
  ...) instead of replacing it.
* Same `KEY=value` format as `example.env`, so existing env files can be moved over as they are.
* The entrypoint applies them at start (as it does with the env vars today) and logs where each value
  came from (`[settings] MAP from server-4.env`).
* The rollover notices a changed settings file and recreates the affected servers once they are empty,
  the way it already does for mod updates. No forced restart, nobody kicked.
* `roles.json` (`ROLES_SCOPE`) and the leaderboard (`LEADERBOARD_SCOPE`) already follow the same idea:
  `shared` by default, `server` for an own copy.

The modules use the same pattern: `webhooks.json` and `bot.json` have `defaults` and per-server entries.

## The server's contract (the part that needs the one restart)

1. **Events on stdout.** Every message the notifier makes is also printed as one line
   `[event] {"scope": "admin|public|updates|updates-admin|leaderboard", "kind": "...", "text": "...", "ping": false}`,
   whether a webhook is set or not. The console lines are already in the log. So the bridge sees everything
   a webhook would get.
2. **Hand-over at run time.** The server reads `/shared/modules.json` every minute, e.g.
   `{"leaderboard": "external", "webhooks": "external"}`. `external` switches its own part off; a missing
   file or `internal` keeps today's behaviour. Switching back and forth needs no restart and nothing counts twice.
3. **`/shared` on every server** (the rollover's `shared_volume`, today missing on server 1).
4. **Settings layers**: `/shared/settings/common.env` and `server-<n>.env` read by the entrypoint (above).

Both the Python image and `slim/` get the same three.

## Order

**Phase A, before the next release to main** (the servers restart once with it):
1. `[event]` lines (`Hook.send` in `app/notifier.py`, the same spot in `slim/notifier.go`).
2. `/shared/modules.json` hand-over for the leaderboard and the webhooks.
3. Bridge: parses `[event]` lines into events with their scope; `/v1/servers` with slot, map, players.
4. Settings layers in the entrypoint (Python and Go); the rollover recreates empty servers whose settings changed.
5. Docs, `stack/docker-compose.yml`, rollover default `shared_volume`, a tool that writes today's container env
   out as `common.env` (keys equal on every server) and `server-<n>.env` (the rest).

**Phase B, after the release, no server restart:**
1. `leaderboard` module: `app/leaderboard.py` as it is (it already only needs "a console line in" and
   "a console command out"), fed from the bridge, answering through bridge actions.
2. `webhooks` module: the notifier's feeds (`public_line`, `chat_line`, ...) fed from the bridge.
3. Bot only through the bridge (no Docker socket).
4. Per server, flip `/shared/modules.json` to `external`, check, remove the `WEBHOOK_*` env vars at the next
   regular restart.
