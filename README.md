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

[Quick start](#quick-start) · [Configuration](#configuration) · [Discord webhook](#discord-webhook) · [Multi-server support](#multi-server-support) · [Slim image](#slim-image) · [Keeping up to date](#keeping-up-to-date) · [Networking](#networking) · [Troubleshooting](#troubleshooting) · [Building](#building)

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

## Configuration

Put settings in env files (`example.env` lists all of them with example values). Values containing `#` or `'` must be double-quoted. A later `env_file` wins over an earlier one. Unset or empty variables leave the existing value alone. The Discord variables are in [Discord webhook](#discord-webhook).

&nbsp;

### Per server

- **`SERVER_NAME`** (text, 1-64 chars): Name in the server browser. Longer names cause a restart loop.

&nbsp;

- **`MAP`** (text): Map everyone skates: `San Vansterdam`, `Isle of Grom`, `Super Ultra Mega Resort`, `Stadium 1`, or the `displayName` of a custom map mod (`reskate-levels.json`).

&nbsp;

- **`PORT`** (number): Game port. Never bound, players join via the Steam relay.

&nbsp;

- **`QUERY_PORT`** (number): Server browser / A2S queries.

&nbsp;

### Access

- **`SERVER_PASSWORD`** (text, `off`): Join password. Empty = anyone can join; `off` clears an existing password.

&nbsp;

- **`LISTED`** (`true` / `false`): `false` hides the server from the browser; players then need the join code.

&nbsp;

- **`MAX_PLAYERS`** (1-249): Player limit.

&nbsp;

- **`ADMINS`** (SteamID64 list): Comma-separated admins who may change settings in-game. Merged with the admins already in the config.

&nbsp;

- **`BANS`** (`id[:name]` list): Comma-separated players who can never join. Merged with existing bans.

&nbsp;

- **`WELCOME_MESSAGE`** (text, `off`): Chat line sent to each player as they join. `off` clears it.

&nbsp;

### Gameplay

- **`TPS`** (`20` `30` `60` `120`): Network updates per second.

&nbsp;

- **`OBJECT_PLACEMENT`** (`everyone` `admins` `nobody`): Who can build and place objects.

&nbsp;

- **`NOCLIP`** (`true` / `false`): Let players use noclip (and tp). Default `true`, admins always can.

&nbsp;

- **`NO_BAIL`** (`true` / `false`): Let players use No Bail. Default `true`, admins always can.

&nbsp;

- **`BOOSTS`** (`true` / `false`): Let players use the forward and up boosts. Default `true`, admins always can.

&nbsp;

- **`ENFORCE_TUNING`** (`true` / `false`): Players skate with the game's own physics tuning, not edited copies. Default `true`.

&nbsp;

- **`PARTIES`** (`true` / `false`): Let players form parties. Default `true`.

&nbsp;

- **`PARTY_SIZE`** (2-8): Most players in one party. Default `8`.

&nbsp;

- **`ANNOUNCE_THROWDOWNS`** (`true` / `false`): Tell everyone in chat when a throwdown drop is placed. Default `true`.

&nbsp;

- **`ACTIVITY_LOG`** (`true` / `false`): Log what players do: throwdowns, joins, objects placed or removed, load times. Default `true`.

&nbsp;

### Anti-cheat

- **`SPEED_CHECK`** (`off` `warn` `kick`): Catch players whose game runs faster than normal (speedhack). `warn` (default) takes them out of throwdowns and coop challenges and tells admins.

&nbsp;

- **`SCORE_CHECK`** (`off` `warn` `kick`): Catch players whose mods change trick scoring or skater handling. `warn` (default) takes them out of throwdowns and coop challenges.

&nbsp;

- **`SCORE_ALLOW`** (hex list): Comma-separated scoring fingerprints (16 hex digits) accepted like the game's own, for servers running a scoring mod everyone installs. Merged with the existing list.

&nbsp;

### Voice

- **`VOICE_CHAT`** (`true` / `false`): Allow voice chat.

&nbsp;

- **`VOICE_RANGE`** (50-1000): How far proximity voice reaches, in metres.

&nbsp;

- **`DISTANCE_FULL_RATE_RETURN`** (metres): Voice update distance: players closer than this are back at the full update rate.

&nbsp;

- **`DISTANCE_HALF_RATE_START`** (metres): Voice update distance: players farther than this update at half rate.

&nbsp;

- **`DISTANCE_HALF_RATE_RETURN`** (metres): Voice update distance: players closer than this are back at half rate.

&nbsp;

- **`DISTANCE_LOW_RATE_START`** (metres): Voice update distance: players farther than this update at the low rate.

&nbsp;

### Voting

- **`VOTE_MAP_ENABLED`** (`true` / `false`): Players can vote for a map change (`/vote map <map>`). Off until turned on.

&nbsp;

- **`VOTE_MAP_PERCENT`** (number): Share of connected players whose yes passes a map vote.

&nbsp;

- **`VOTE_KICK_ENABLED`** (`true` / `false`): Players can vote to kick someone (`/vote kick <player>`). Admins cannot be vote-kicked.

&nbsp;

- **`VOTE_KICK_PERCENT`** (number): Share of connected players whose yes passes a kick vote.

&nbsp;

- **`VOTE_TIME_OF_DAY_ENABLED`** (`true` / `false`): Players can vote on the time of day (`/vote tod <time>`). Needs `WORLD_LAYER_SYNC`.

&nbsp;

- **`VOTE_TIME_OF_DAY_PERCENT`** (number): Share of connected players whose yes passes a time-of-day vote.

&nbsp;

- **`VOTE_SECONDS`** (seconds): How long a vote runs. Default `30`.

&nbsp;

- **`VOTE_COOLDOWN_SECONDS`** (seconds): How long a player waits before starting another vote. Default `60`.

&nbsp;

### Parks and layers

- **`PARK_CONSTRUCTION`** (park id): Layout of the construction park lot, e.g. `skatepark_01`, or `empty`.

&nbsp;

- **`PARK_HISTORIC`** (park id): Layout of the historic park lot, e.g. `megapark_05`, or `empty`.

&nbsp;

- **`PARK_FINANCIAL`** (park id): Layout of the financial park lot, e.g. `flumppark_08`, or `empty`.

&nbsp;

- **`WORLD_LAYER_SYNC`** (`true` / `false`): Force the `LAYERS` below on every player.

&nbsp;

- **`LAYERS`** (`key=on|off|default` list): World layers, comma-separated, e.g. `key=on,other=off`. `default` removes the setting.

&nbsp;

### Updates

- **`AUTO_UPDATE`** (`true` / `false`): Only sets a key in `ReSkateServer.json`. The server cannot update itself on Linux, see [Keeping up to date](#keeping-up-to-date).

&nbsp;

### Data

`/data` (a volume) holds `ReSkateServer.json`, `ReSkateServer.log`, `Mods/` and `world-layers.json`. Custom maps are mod folders in `/data/Mods/<folder>`:

```bash
docker cp <mod-folder> reskate-server-1:/data/Mods/
docker compose restart reskate-1
```

---

## Discord webhook

Set `DISCORD_WEBHOOK` (per server, in `serverN.env`) and the server talks to Discord:

- **Console:** every line the server prints (joins, leaves, admin commands, throwdowns, ...) is posted as a code block, batched every few seconds. `docker attach` keeps working.
- **Updates:** when a new ReSkate release is out, the server posts **UPDATE AVAILABLE** once per release (checked every 3 hours), and again when the matching Docker Hub image is published. Only this message can mention anyone.

&nbsp;

**Variables**

&nbsp;

- **`DISCORD_WEBHOOK`**: Webhook URL (Discord: channel settings, Integrations, Webhooks). Unset = off.

&nbsp;

- **`DISCORD_MENTION_IDS`**: Comma-separated Discord user ids to mention in the update message, e.g. `123456789012345678,234567890123456789`.

&nbsp;

- **`DISCORD_CONSOLE`**: `false` = only update messages, no console output (default `true`).

&nbsp;

- **`DISCORD_USERNAME`**: Name shown on the posts. Default is `SERVER_NAME` without any `discord...` word, which Discord rejects in webhook names.

&nbsp;

**Notes**

- Player names and chat can never ping anyone: console posts disable all mentions.
- Each server sends its own update message. If several servers share one webhook, set `DISCORD_MENTION_IDS` on one of them only.
- The console includes the **join code**. Use a channel only people you trust can read.
- Failures (bad URL, rate limits) never affect the server; they are written to `/data/DiscordWebhook.log`.
- The last announced version is kept in `/data/.discord-update-notified`.

---

## Slim image

`dudedankdave/reskate-server:slim` (and `<version>-slim`, e.g. `1.1.1-slim`) is the same server in a distroless image: **87 MB instead of 172 MB**, with no shell, package manager, Python or curl. Same environment variables, same Discord webhook, same `/data` layout, so switching is just changing the tag:

```yaml
    image: dudedankdave/reskate-server:slim
```

- One static Go binary (`/app/reskate`) replaces the entrypoint, Discord sidecar and healthcheck.
- The server runs exactly one `curl` command to read the global ban list. The slim image ships a small stand-in for that command. If a future ReSkate release changes it, the log says `The global ban list could not be read` and the stand-in needs an update. The default image has real curl and is not affected.
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

The image includes `curl`, which the server needs to read the global ban list.

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
