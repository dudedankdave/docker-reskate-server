<p align="left">
  <img src="https://raw.githubusercontent.com/dudedankdave/docker-reskate-server/main/assets/icon.png" width="128" height="128" alt="ReSkate server icon">
</p>

# ReSkate - Dedicated Server - Docker 

[Docker Hub](https://hub.docker.com/r/dudedankdave/reskate-server) · [GitHub](https://github.com/dudedankdave/docker-reskate-server) · [ReSkate releases](https://github.com/Dingo-Shenanigans/ReSkate/releases)

Docker image for the [ReSkate](https://github.com/Dingo-Shenanigans/ReSkate) dedicated server:

- **Configured with environment variables.** They are written to `/data/ReSkateServer.json` on every start. Unset or empty variables leave the existing value alone, so changes made in-game or from the console survive restarts.
- **Multi-server support:** run several servers on one host, each with its own data volume.
- **Discord webhook:** forwards the console and announces new ReSkate releases.
- **Healthcheck** that reports `healthy` once the server is up on its map.

> **The server must run the same ReSkate version as the players.** If it falls behind, it can vanish from the in-game list and join codes time out, even though the container is `healthy`. See [Keeping up to date](#keeping-up-to-date).

---

## Contents

[Quick start](#quick-start) · [About](#about) · [Configuration](#configuration) · [Discord webhook](#discord-webhook) · [Multi-server support](#multi-server-support) · [Slim image](#slim-image) · [Keeping up to date](#keeping-up-to-date) · [Networking](#networking) · [Troubleshooting](#troubleshooting) · [Building](#building)

---

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

Image tags match the [ReSkate release](https://github.com/Dingo-Shenanigans/ReSkate/releases): `1.1.1`, `1.1`, `latest`, plus the smaller [`slim`](#slim-image) variant. The healthcheck turns `unhealthy` if the last startup event is a stuck Steam sign-in or a `Config problem`.

Every restart gives the server a new Steam ID and **a new join code**, and disconnects the players.

---

## About


### Tags

| Tag | What it is | Size on disk (download) |
|---|---|---|
| `latest` | Newest ReSkate release, default image (Debian slim) | 172 MB (64 MB) |
| `1.1.1`, `1.1` | That ReSkate release, pinned (`1.1` follows the newest `1.1.x`) | 172 MB (64 MB) |
| `slim` | Newest ReSkate release, [slim (distroless) image](#slim-image) | 87 MB (34 MB) |
| `1.1.1-slim` | That ReSkate release, pinned, slim | 87 MB (34 MB) |

The version in a tag is the ReSkate release of the server inside the image. It has to match the players' game, see [Keeping up to date](#keeping-up-to-date). Older tags stay available as published.


### Dependencies

| | Default (`latest`, `1.1.1`) | `slim` |
|---|---|---|
| Base image | `debian:trixie-slim` | `gcr.io/distroless/cc-debian13` |
| ReSkate server | Native Linux x86_64 build from the release (no Wine), with the Steam libraries from the same archive (`libsteam_api.so`, `steamclient.so`, `libtier0_s.so`, `libvstdlib_s.so`) | same |
| Entrypoint, Discord sidecar, healthcheck | Python 3 scripts (`python3-minimal`) | One static Go binary (`/app/reskate`) |
| Init | `tini` | `tini` |
| `curl` | Real `curl` | Small stand-in inside the Go binary, see below |
| Shell | bash | Static busybox, `sh` only |
| TLS (OpenSSL, CA certificates) | `libssl3`, `ca-certificates` | Included in the base image |
| Package manager | apt | None |
| Runs as | uid 1000 (`reskate`) | uid 1000 |
| Debugging | `docker exec -it <container> bash` | `docker exec -it <container> busybox sh` |


### curl

The server reads the ReSkate team's global ban list from `api.reskate.dev` at startup and every ten minutes. It does that by running one fixed command through `sh`:

```
curl --silent --show-error --fail --max-time 15 --max-filesize N --proto =https --user-agent ReSkateServer/1 <url>
```

- **Default image:** ships real `curl`, nothing to do.
- **Slim image:** ships a small stand-in for exactly that command, built into `/app/reskate`. If a future ReSkate release changes the command, the stand-in refuses the unknown option and needs an update.
- **Without `curl`:** the log says `The global ban list could not be read` and only the server's own bans apply.

---

## Configuration

Put settings in env files (`example.env` lists all of them with example values). Values containing `#` or `'` must be double-quoted. A later `env_file` wins over an earlier one. Unset or empty variables leave the existing value alone. The Discord variables are in [Discord webhook](#discord-webhook).


### Custom maps and mods

Custom maps are mod folders in `/data/Mods/<folder>`. The server only reads each mod's `reskate-levels.json`, but **players need the same map mod installed** to join.

1. Copy the map's mod folder from the game's `Mods` folder into the server's volume:

   ```bash
   docker cp <mod-folder> reskate-server-1:/data/Mods/
   ```

2. Set `MAP` to the `displayName` from the mod's `reskate-levels.json`, e.g. `MAP=Skate2Map`.
3. Restart the server: `docker compose restart reskate-1`

Built-in maps need no mod: `San Vansterdam`, `Isle of Grom`, `Super Ultra Mega Resort`, `Stadium 1`. Every server has its own `/data` volume, so copy the mod into each server that should use it.


### Per server

| Variable | Values | Description |
|---|---|---|
| `SERVER_NAME` | text, 1-64 chars | Name in the server browser. Longer names cause a restart loop. |
| `MAP` | text | Map everyone skates: `San Vansterdam`, `Isle of Grom`, `Super Ultra Mega Resort`, `Stadium 1`, or the `displayName` of a custom map mod (`reskate-levels.json`). |
| `PORT` | number | Game port. Never bound, players join via the Steam relay. |
| `QUERY_PORT` | number | Server browser / A2S queries. |


### Access

| Variable | Values | Description |
|---|---|---|
| `SERVER_PASSWORD` | text, `off` | Join password. Empty = anyone can join; `off` clears an existing password. |
| `LISTED` | `true` / `false` | `false` hides the server from the browser; players then need the join code. |
| `MAX_PLAYERS` | 1-249 | Player limit. |
| `ADMINS` | SteamID64 list | Comma-separated admins who may change settings in-game. Merged with the admins already in the config. |
| `BANS` | `id[:name]` list | Comma-separated players who can never join. Merged with existing bans. |
| `WELCOME_MESSAGE` | text, `off` | Chat line sent to each player as they join. `off` clears it. |


### Gameplay

| Variable | Values | Description |
|---|---|---|
| `TPS` | `20` `30` `60` `120` | Network updates per second. |
| `OBJECT_PLACEMENT` | `everyone` `admins` `nobody` | Who can build and place objects. |
| `NOCLIP` | `true` / `false` | Let players use noclip (and tp). Default `true`, admins always can. |
| `NO_BAIL` | `true` / `false` | Let players use No Bail. Default `true`, admins always can. |
| `BOOSTS` | `true` / `false` | Let players use the forward and up boosts. Default `true`, admins always can. |
| `ENFORCE_TUNING` | `true` / `false` | Players skate with the game's own physics tuning, not edited copies. Default `true`. |
| `PARTIES` | `true` / `false` | Let players form parties. Default `true`. |
| `PARTY_SIZE` | 2-8 | Most players in one party. Default `8`. |
| `ANNOUNCE_THROWDOWNS` | `true` / `false` | Tell everyone in chat when a throwdown drop is placed. Default `true`. |
| `ACTIVITY_LOG` | `true` / `false` | Log what players do: throwdowns, joins, objects placed or removed, load times. Default `true`. |


### Anti-cheat

| Variable | Values | Description |
|---|---|---|
| `SPEED_CHECK` | `off` `warn` `kick` | Catch players whose game runs faster than normal (speedhack). `warn` (default) takes them out of throwdowns and coop challenges and tells admins. |
| `SCORE_CHECK` | `off` `warn` `kick` | Catch players whose mods change trick scoring or skater handling. `warn` (default) takes them out of throwdowns and coop challenges. |
| `SCORE_ALLOW` | hex list | Comma-separated scoring fingerprints (16 hex digits) accepted like the game's own, for servers running a scoring mod everyone installs. Merged with the existing list. |


### Voice

| Variable | Values | Description |
|---|---|---|
| `VOICE_CHAT` | `true` / `false` | Allow voice chat. |
| `VOICE_RANGE` | 50-1000 | How far proximity voice reaches, in metres. |
| `DISTANCE_FULL_RATE_RETURN` | metres | Voice update distance: players closer than this are back at the full update rate. |
| `DISTANCE_HALF_RATE_START` | metres | Voice update distance: players farther than this update at half rate. |
| `DISTANCE_HALF_RATE_RETURN` | metres | Voice update distance: players closer than this are back at half rate. |
| `DISTANCE_LOW_RATE_START` | metres | Voice update distance: players farther than this update at the low rate. |


### Voting

| Variable | Values | Description |
|---|---|---|
| `VOTE_MAP_ENABLED` | `true` / `false` | Players can vote for a map change (`/vote map <map>`). Off until turned on. |
| `VOTE_MAP_PERCENT` | number | Share of connected players whose yes passes a map vote. |
| `VOTE_KICK_ENABLED` | `true` / `false` | Players can vote to kick someone (`/vote kick <player>`). Admins cannot be vote-kicked. |
| `VOTE_KICK_PERCENT` | number | Share of connected players whose yes passes a kick vote. |
| `VOTE_TIME_OF_DAY_ENABLED` | `true` / `false` | Players can vote on the time of day (`/vote tod <time>`). Needs `WORLD_LAYER_SYNC`. |
| `VOTE_TIME_OF_DAY_PERCENT` | number | Share of connected players whose yes passes a time-of-day vote. |
| `VOTE_SECONDS` | seconds | How long a vote runs. Default `30`. |
| `VOTE_COOLDOWN_SECONDS` | seconds | How long a player waits before starting another vote. Default `60`. |


### Parks and layers

| Variable | Values | Description |
|---|---|---|
| `PARK_CONSTRUCTION` | park id | Layout of the construction park lot, e.g. `skatepark_01`, or `empty`. |
| `PARK_HISTORIC` | park id | Layout of the historic park lot, e.g. `megapark_05`, or `empty`. |
| `PARK_FINANCIAL` | park id | Layout of the financial park lot, e.g. `flumppark_08`, or `empty`. |
| `WORLD_LAYER_SYNC` | `true` / `false` | Force the `LAYERS` below on every player. |
| `LAYERS` | `key=on\|off\|default` list | World layers, comma-separated, e.g. `key=on,other=off`. `default` removes the setting. |


### Updates

| Variable | Values | Description |
|---|---|---|
| `AUTO_UPDATE` | `true` / `false` | Only sets a key in `ReSkateServer.json`. The server cannot update itself on Linux, see [Keeping up to date](#keeping-up-to-date). |


### Data

`/data` (a volume) holds `ReSkateServer.json`, `ReSkateServer.log`, `Mods/` and `world-layers.json`.

---

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

---

## Slim image

`dudedankdave/reskate-server:slim` (and `<version>-slim`, e.g. `1.1.1-slim`) is the same server in a distroless image: **87 MB instead of 172 MB**, with no shell, package manager, Python or curl (see [About](#about) for what is inside each tag). Same environment variables, same Discord webhook, same `/data` layout, so switching is just changing the tag:

```yaml
    image: dudedankdave/reskate-server:slim
```

- One static Go binary (`/app/reskate`) replaces the entrypoint, Discord sidecar and healthcheck.
- The global ban list needs `curl`: the slim image ships a small stand-in, see [About](#curl).
- There is no bash. For debugging use `docker exec -it <container> busybox sh`.
- The source is in [`slim/`](slim/). Build it from the repo root: `docker build -f slim/Dockerfile --build-arg VERSION=1.1.1 -t dudedankdave/reskate-server:slim .`
- `latest` and the plain version tags stay the default image. The Discord update message points at the plain tag, so run `docker compose pull` on a slim setup once the `slim` tag has been refreshed for the new release.

---

## Multi-server support

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

---

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

---

## Networking

- Players join through the **Steam relay** on a random ephemeral UDP port. `PORT` is never bound; `QUERY_PORT` serves the server browser.
- Only `QUERY_PORT` needs to be reachable from the internet. Open it for UDP.
- With a **stateless firewall**, also allow replies from the Steam relay: UDP from source ports 27000-27200 to destination ports 32768-65535. Without that rule, joins time out.
- `network_mode: host` is required, the relay breaks behind bridge NAT.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Not in the server list, join code times out, container `healthy`, other players can still join | The server runs an older ReSkate version than your game. Compare the [latest release](https://github.com/Dingo-Shenanigans/ReSkate/releases) with the image tag, then [update](#keeping-up-to-date). |
| Joins time out for everyone | Stateless firewall dropping the relay replies, see [Networking](#networking). |
| Restart loop, log says `Config problem` | `SERVER_NAME` over 64 characters, or an unquoted `#` / `'` in an env value. |
| Container `unhealthy` | Stuck Steam sign-in or a config problem, check `docker logs`. |
| Custom map not found | `MAP` must be the `displayName` from the mod's `reskate-levels.json`, and the mod folder must be in `/data/Mods/`. |
| Settings don't change | Empty variables are ignored. Use `off` to clear `SERVER_PASSWORD` / `WELCOME_MESSAGE`. |

---

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
| `slim/` | Source and Dockerfile of the slim (distroless) image |
| `example.env`, `docker-compose.yml` | Starting point for your own setup |
