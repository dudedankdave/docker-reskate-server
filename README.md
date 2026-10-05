# ReSkate - Dedicated Server - Docker 

[Docker Hub](https://hub.docker.com/r/dudedankdave/reskate-server) · [GitHub](https://github.com/dudedankdave/docker-reskate-server) · [ReSkate releases](https://github.com/Dingo-Shenanigans/ReSkate/releases)

Docker image for the [ReSkate](https://github.com/Dingo-Shenanigans/ReSkate) dedicated server:

- **Configured with environment variables.** They are written to `/data/ReSkateServer.json` on every start. Unset or empty variables leave the existing value alone, so changes made in-game or from the console survive restarts.
- **Several servers on one host**, each with its own data volume.
- **Discord webhook:** forwards the console and announces new ReSkate releases.
- **Healthcheck** that reports `healthy` once the server is up on its map.

> **The server must run the same ReSkate version as the players.** If it falls behind, it can vanish from the in-game list and join codes time out, even though the container is `healthy`. See [Keeping up to date](#keeping-up-to-date).

## Contents

[Quick start](#quick-start) · [Configuration](#configuration) · [Discord webhook](#discord-webhook) · [Several servers](#several-servers) · [Keeping up to date](#keeping-up-to-date) · [Networking](#networking) · [Troubleshooting](#troubleshooting) · [Building](#building)

## Quick start

```yaml
services:
  reskate-1:
    image: dudedankdave/reskate-server:latest
    container_name: reskate-server-1
    restart: unless-stopped
    stdin_open: true
    tty: true
    network_mode: host   # Steam relay breaks behind bridge NAT
    env_file: [common.env, server1.env]
    volumes:
      - reskate-data:/data
volumes:
  reskate-data:
```

1. Copy `example.env` to `common.env` (shared settings) and `server1.env` (name, map, ports).
2. `docker compose up -d`
3. `docker logs reskate-server-1` shows the **join code** once the server is up.

| Task | Command |
|---|---|
| Console | `docker attach reskate-server-1` (detach with Ctrl-P Ctrl-Q) |
| Status | `docker ps` (`healthy` once the log reports `... is up on <map>`) |
| Update | `docker compose pull && docker compose up -d` |
| Pin a version | `RESKATE_VERSION=1.1.1 docker compose up -d` (default `latest`) |

Image tags match the [ReSkate release](https://github.com/Dingo-Shenanigans/ReSkate/releases): `1.1.1`, `1.1`, `latest`. The healthcheck turns `unhealthy` if the last startup event is a stuck Steam sign-in or a `Config problem`.

Every restart gives the server a new Steam ID and **a new join code**, and disconnects the players.

## Configuration

Put settings in env files (`example.env` lists all of them with example values). Values containing `#` or `'` must be double-quoted. A later `env_file` wins over an earlier one.

### Per server

| Variable | Meaning |
|---|---|
| `SERVER_NAME` | Name in the server browser, max 64 characters (longer causes a restart loop). |
| `MAP` | Map name; for custom maps the `displayName` from the mod's `reskate-levels.json`. |
| `PORT` | Game port. Never bound, players join via the Steam relay. |
| `QUERY_PORT` | Server browser / A2S queries. |

### Access

| Variable | Meaning |
|---|---|
| `SERVER_PASSWORD` | Join password. `off` clears an existing one. |
| `LISTED` | `false` hides the server from the browser; players then need the join code. |
| `MAX_PLAYERS` | Player limit. |
| `ADMINS` | Comma-separated SteamID64s, merged with the admins already in the config. |
| `BANS` | `id[:name],id[:name]`, merged with existing bans. |
| `WELCOME_MESSAGE` | Chat message for joining players. `off` clears it. |

### Gameplay

| Variable | Values |
|---|---|
| `TPS` | `20`, `30`, `60`, `120` |
| `NOCLIP`, `NO_BAIL`, `BOOSTS`, `ENFORCE_TUNING` | `true` / `false` |
| `OBJECT_PLACEMENT` | `everyone`, `admins`, `nobody` |
| `PARTIES`, `PARTY_SIZE` | parties on/off, party size |
| `ANNOUNCE_THROWDOWNS`, `ACTIVITY_LOG` | `true` / `false` |
| `PARK_CONSTRUCTION`, `PARK_HISTORIC`, `PARK_FINANCIAL` | park ids |
| `WORLD_LAYER_SYNC`, `LAYERS` | layer sync; `LAYERS=key=on,other=off` (`on`, `off`, `default`) |

### Anti-cheat, voice, voting

| Variable | Values |
|---|---|
| `SPEED_CHECK`, `SCORE_CHECK` | `off`, `warn`, `kick` |
| `SCORE_ALLOW` | Comma-separated list, merged with the existing one. |
| `VOICE_CHAT`, `VOICE_RANGE` | voice on/off, range |
| `DISTANCE_FULL_RATE_RETURN`, `DISTANCE_HALF_RATE_START`, `DISTANCE_HALF_RATE_RETURN`, `DISTANCE_LOW_RATE_START` | voice distance falloff |
| `VOTE_MAP_*`, `VOTE_KICK_*`, `VOTE_TIME_OF_DAY_*` | `_ENABLED` and `_PERCENT` per vote |
| `VOTE_SECONDS`, `VOTE_COOLDOWN_SECONDS` | vote length and cooldown |

`AUTO_UPDATE` only sets a key in `ReSkateServer.json`. The server cannot update itself on Linux, see [Keeping up to date](#keeping-up-to-date).

### Data

`/data` (a volume) holds `ReSkateServer.json`, `ReSkateServer.log`, `Mods/` and `world-layers.json`. Custom maps are mod folders in `/data/Mods/<folder>`:

```bash
docker cp <mod-folder> reskate-server-1:/data/Mods/
docker compose restart reskate-1
```

## Discord webhook

Set `DISCORD_WEBHOOK` (per server, in `serverN.env`) and the server talks to Discord:

- **Console:** every line the server prints (joins, leaves, admin commands, throwdowns, ...) is posted as a code block, batched every few seconds. `docker attach` keeps working.
- **Updates:** when a new ReSkate release is out, the server posts **UPDATE AVAILABLE** once per release (checked every 3 hours), and again when the matching Docker Hub image is published. Only this message can mention anyone.

| Variable | Meaning |
|---|---|
| `DISCORD_WEBHOOK` | Webhook URL (Discord: channel settings, Integrations, Webhooks). Unset = off. |
| `DISCORD_MENTION_IDS` | Comma-separated Discord user ids to mention in the update message, e.g. `123456789012345678,234567890123456789`. |
| `DISCORD_CONSOLE` | `false` = only update messages, no console output (default `true`). |
| `DISCORD_USERNAME` | Name shown on the posts. Default is `SERVER_NAME` without any `discord...` word, which Discord rejects in webhook names. |

- Player names and chat can never ping anyone: console posts disable all mentions.
- Each server sends its own update message. If several servers share one webhook, set `DISCORD_MENTION_IDS` on one of them only.
- The console includes the **join code**. Use a channel only people you trust can read.
- Failures (bad URL, rate limits) never affect the server; they are written to `/data/DiscordWebhook.log`.
- The last announced version is kept in `/data/.discord-update-notified`.

## Several servers

Run one container per server from the same image. Each needs its **own** data volume, its **own** `PORT` / `QUERY_PORT`, and its own name and map. Everything else can be shared.

1. Shared settings go in `common.env`, only the differences in `server1.env`, `server2.env`, ...:

   ```env
   # server1.env
   SERVER_NAME="My Servers #1 | Street"
   MAP=Industrial Zone
   PORT=12400
   QUERY_PORT=12401
   ```
   ```env
   # server2.env
   SERVER_NAME="My Servers #2 | Skate 2"
   MAP=Skate2Map
   PORT=12410
   QUERY_PORT=12411
   ```

2. Add a service and a volume per server. `docker-compose.yml` contains a ready-to-uncomment second server.
3. `docker compose up -d`. Restart one with `docker compose restart reskate-2`, attach with `docker attach reskate-server-2`.

- All servers use `network_mode: host`, so ports must be unique on the host. Use one block per server, e.g. 12400/12401, 12410/12411, 12420/12421.
- Sharing one `/data` volume makes the servers overwrite each other's config, mods and logs.

## Keeping up to date

ReSkate releases often and the game client updates itself, so the server has to follow.

- The server binary is baked into the image and **cannot update itself on Linux**. Updating means a new image.
- **Update:** `docker compose pull && docker compose up -d`. This restarts the servers and changes the join codes.
- If the new tag is not on Docker Hub yet, build it yourself, see [Building](#building).
- **Get told about releases:** set `DISCORD_WEBHOOK`, see [Discord webhook](#discord-webhook).
- **Without Discord:** `check-update.sh [container]` compares the running image with the latest ReSkate release and says whether `docker compose pull` is enough (exit code 10 = update available). Set `NOTIFY_WEBHOOK` to get a POST once per release, or run it from cron:

  ```bash
  7 */3 * * * /path/to/check-update.sh reskate-server-1 >> /var/log/reskate-update-check.log 2>&1
  ```

## Networking

- Players join through the **Steam relay** on a random ephemeral UDP port. `PORT` is never bound; `QUERY_PORT` serves the server browser.
- Only `QUERY_PORT` needs to be reachable from the internet. Open it for UDP.
- With a **stateless firewall**, also allow replies from the Steam relay: UDP from source ports 27000-27200 to destination ports 32768-65535. Without that rule, joins time out.
- `network_mode: host` is required, the relay breaks behind bridge NAT.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Not in the server list, join code times out, container `healthy`, other players can still join | The server runs an older ReSkate version than your game. Compare the [latest release](https://github.com/Dingo-Shenanigans/ReSkate/releases) with the image tag, then [update](#keeping-up-to-date). |
| Joins time out for everyone | Stateless firewall dropping the relay replies, see [Networking](#networking). |
| Restart loop, log says `Config problem` | `SERVER_NAME` over 64 characters, or an unquoted `#` / `'` in an env value. |
| Container `unhealthy` | Stuck Steam sign-in or a config problem, check `docker logs`. |
| Custom map not found | `MAP` must be the `displayName` from the mod's `reskate-levels.json`, and the mod folder must be in `/data/Mods/`. |
| Settings don't change | Empty variables are ignored. Use `off` to clear `SERVER_PASSWORD` / `WELCOME_MESSAGE`. |

The image includes `curl`, which the server needs to read the global ban list.

## Building

The server binaries are proprietary and not part of this repo. Download `ReSkateServer-Linux-<version>.tar.gz` from a [ReSkate release](https://github.com/Dingo-Shenanigans/ReSkate/releases) and copy `ReSkateServer`, `libsteam_api.so`, `libtier0_s.so`, `libvstdlib_s.so` and `steamclient.so` into `./Server/`, then:

```bash
docker build --build-arg VERSION=1.1.1 -t dudedankdave/reskate-server:1.1.1 .
```

Use the release version as `VERSION`, and tag the image `<major>.<minor>` and `latest` as well.

| File | Purpose |
|---|---|
| `Dockerfile` | Image build (Debian slim, tini, curl, minimal Python) |
| `entrypoint.py` | Env vars to `ReSkateServer.json`, starts the Discord sidecar, then the server |
| `notifier.py` | Discord console forwarding and update announcements |
| `healthcheck.py` | Reports healthy once the log shows the server is up |
| `check-update.sh` | Host-side release check |
| `example.env`, `docker-compose.yml` | Starting point for your own setup |
