<p align="left">
  <img src="https://raw.githubusercontent.com/dudedankdave/docker-reskate-server/main/assets/icon.png" width="128" height="128" alt="ReSkate server icon">
</p>

# ReSkate - Dedicated Server - Docker

[Docker Hub](https://hub.docker.com/r/dudedankdave/reskate-server) · [GitHub](https://github.com/dudedankdave/docker-reskate-server) · [ReSkate releases](https://github.com/Dingo-Shenanigans/ReSkate/releases)

Docker image for the [ReSkate](https://github.com/Dingo-Shenanigans/ReSkate) dedicated server:

- **Configured with environment variables**, all listed in [docs/ENVIRONMENT.md](docs/ENVIRONMENT.md). Changes made in-game or from the console survive restarts.
- **Multi-server support:** several servers on one host, each with its own data volume.
- **Updates:** pinned to the image, or installed automatically (instant, countdown, scheduled or approved in Discord).
- **Discord webhooks:** essentials, full console log, a public player feed, in-game chat and a leaderboard.
- **Thunderstore mods and custom maps**, installed on start.
- **Healthcheck** that reports `healthy` once the server is up on its map.

> **The server must run the same ReSkate version as the players.** If it falls behind, it can vanish from the in-game list and join codes time out, even though the container is `healthy`. See [Keeping up to date](#keeping-up-to-date).

**Contents:** [Quick start](#quick-start) · [Tags](#tags) · [Configuration](#configuration) · [Custom maps and mods](#custom-maps-and-mods) · [Discord webhook](#discord-webhook) · [Ranked and leaderboard](#ranked-and-leaderboard) · [Multi-server support](#multi-server-support) · [Keeping up to date](#keeping-up-to-date) · [Networking](#networking) · [Troubleshooting](#troubleshooting) · [Slim image](#slim-image) · [Building](#building)

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

1. Copy [`example.env`](example.env) to `common.env` (shared settings) and `server1.env` (name, map, ports).
2. `docker compose up -d`
3. `docker logs reskate-server-1` shows the **join code** once the server is up.

| Task | Command |
|---|---|
| Console | `docker attach reskate-server-1` (detach with Ctrl-P Ctrl-Q) |
| Status | `docker ps` (`healthy` once the log reports `... is up on <map>`) |
| Update | `docker compose pull && docker compose up -d` |
| Pin a version | `RESKATE_VERSION=2.0.5 docker compose up -d` (default `latest`) |

Without `STEAM_TOKEN` every restart gives the server a new Steam ID and **join code**. Either way a restart disconnects the players.

## Tags

| Tag | What it is | Size on disk (download) |
|---|---|---|
| `latest` | Newest ReSkate release, default image (Debian slim) | 172 MB (64 MB) |
| `2.0.5`, `2.0` | That ReSkate release, pinned (`2.0` follows the newest `2.0.x`; `1.1` stays on 1.1.8) | 172 MB (64 MB) |
| `slim` | Newest ReSkate release, [slim (distroless) image](#slim-image) | 87 MB (34 MB) |
| `2.0.5-slim` | That ReSkate release, pinned, slim | 87 MB (34 MB) |

The version in a tag is the [ReSkate release](https://github.com/Dingo-Shenanigans/ReSkate/releases) of the server inside and has to match the players' game. Older tags stay available. ReSkate 2.0.3 changed the multiplayer protocol: 2.0.2 and 2.0.3+ can't join each other. 2.0.4 and 2.0.5 ship the same server as 2.0.3.

## Configuration

Put settings in env files; **[docs/ENVIRONMENT.md](docs/ENVIRONMENT.md) lists every variable** (per server, access, gameplay, anti-cheat, voice, network, voting, announcements, parks and layers, mods, updates, Discord, leaderboard), and [`example.env`](example.env) is a ready-made starting point.

- Variables are written to `/data/ReSkateServer.json` on every start. Unset or empty ones leave the existing value alone; `off` clears a value where supported.
- Values containing `#` or `'` must be double-quoted. A later `env_file` wins over an earlier one.
- `/data` (a volume) holds `ReSkateServer.json`, `ReSkateServer.log`, `data/bans.json`, `Mods/` and `world-layers.json`.

## Custom maps and mods

**Automatic: download from Thunderstore.** List packages in `MODS` (per server, in `serverN.env`) and the server installs them into `/data/Mods` on every start:

```env
MODS=zeex64-Full_Skate_3_Map,brassy-Skate2Map
```

- **Formats:** `Owner-Name` (latest version, kept up to date), `Owner-Name-1.2.3` (pinned), or a package page URL such as `https://thunderstore.io/c/reskate/p/Owner/Name/`. Separate entries with commas, spaces or new lines.
- **Map name:** the log prints the maps of each installed package, e.g. `[mods] brassy-Skate2Map 1.0.0: installed, maps: Skate2Map (use as MAP)`. Put that name into `MAP`.
- **Nothing is downloaded when the installed version is current.** Maps are large (`Skate2Map` is 908 MB): the first start can take minutes, and `docker ps` may show `unhealthy` meanwhile. An unpinned package is never replaced by an older version, even if Thunderstore's "latest" lags behind.
- **Dependencies** listed in a package's `manifest.json` are installed too, at the version they ask for.
- **Your own folders are safe:** a mod folder you copied in by hand is never overwritten, and is adopted without a download when its `manifest.json` already has the wanted version. Only folders the installer created are updated.
- **Problems never stop the server:** if Thunderstore is unreachable or a package is invalid, it is logged as `[mods] ...` (see `docker logs`) and the server starts with what is installed.
- **Map renames:** an update can rename a map (`Skate2Map` 1.0.5 became `New San Vanelona`) and the server then refuses to start. A `[maps] WARNING` log line lists the installed names. Pin the version (`Owner-Name-1.2.3`) if a map must never change.
- **Update messages in Discord:** the admin feed announces each new version of an unpinned mod (checked hourly) and what the installer did at start, see [Discord webhook](#discord-webhook). Nothing is installed while the server runs: restart it (unless `MODS_UPDATE=false`). Pinned entries are not checked.
- **Refresh without a restart:** `docker exec reskate-server-1 /app/reskate mods` (slim image) or `docker exec reskate-server-1 python3 /app/mods.py` (default image). Restart the server afterwards so a changed map loads.
- **Safety:** packages are downloaded over HTTPS from Thunderstore only. A zip with paths outside `/data/Mods`, links, or more than 8 GiB unpacked is refused. Still, only list packages you trust: players get the same files.

**Manual: copy the mod folder yourself.** Custom maps are mod folders in `/data/Mods/<folder>`. The server only reads each mod's `reskate-levels.json`, but **players need the same map mod installed** to join.

1. Copy the map's mod folder from the game's `Mods` folder into the server's volume:

   ```bash
   docker cp <mod-folder> reskate-server-1:/data/Mods/
   ```

2. Set `MAP` to the `displayName` from the mod's `reskate-levels.json`, e.g. `MAP=Skate2Map`.
3. Restart the server: `docker compose restart reskate-1`

Built-in maps need no mod: `San Vansterdam`, `Isle of Grom`, `Super Ultra Mega Resort`, `Stadium 1`. Every server has its own `/data` volume, so copy the mod into each server that should use it.

## Discord webhook

Any number of webhooks, each set with a pair `WEBHOOK_URL_<n>` / `WEBHOOK_SCOPE_<n>` (the suffix can be anything). The scope says what the webhook gets, several comma-separated:

- **`admin`** (or `essentials`): what an admin has to know. The "is up on" line and the **join code**, config problems, warnings and errors, server stops and crashes, **UPDATE AVAILABLE**, countdowns, **UPDATED** / **UPDATE FAILED** / **ROLLED BACK**, and mod messages (**MOD UPDATE AVAILABLE**, **MOD INSTALLED**, **MOD UPDATED**). The only scope that pings `DISCORD_MENTION_IDS`.
- **`log`** (or `console`): the whole console as code blocks except the in-game chat: joins with Steam IDs, leaves with reasons, `[admin]`, `[objects]`, `[join]`, throwdowns. Add `chat` to the same webhook to have the chat there too. The answer to an info command typed in the console (`status`, `players`, `net`, `bans`, `maps`, `votes`, `help`, ...) is posted as its own block titled **Console: `<command>`**.
- **`public`**: for players, cleaned up: joins (`Name joined, 3/100 players`), leaves (`Name left`), throwdowns, the "is up on" line and update notices ("Restarting in 5 min to update ReSkate to 2.0.5"). No Steam IDs, chat, admin commands or join code.
- **`chat`**: only the in-game chat, as `[12:34:56] Name: message`.
- **`leaderboard`**: the [leaderboard](#ranked-and-leaderboard). Without one, it goes to the `public` webhooks.

```yaml
      WEBHOOK_URL_1: https://discord.com/api/webhooks/<id>/<token>
      WEBHOOK_SCOPE_1: admin,log
      WEBHOOK_URL_2: https://discord.com/api/webhooks/<id>/<token>
      WEBHOOK_SCOPE_2: chat
      WEBHOOK_URL_3: https://discord.com/api/webhooks/<id>/<token>
      WEBHOOK_SCOPE_3: public,leaderboard
```

`DISCORD_MENTION_IDS`, `DISCORD_USERNAME` and the older variable names: see [Discord in ENVIRONMENT.md](docs/ENVIRONMENT.md#discord).

- Player names and chat can never ping anyone: console posts disable mentions.
- If several servers share one admin webhook, set `DISCORD_MENTION_IDS` on one of them only.
- The admin and log scopes contain the **join code**; the log also has Steam IDs and chat. The public scope is safe for a public channel.
- Failures (bad URL, rate limits) never affect the server; they are written to `/data/DiscordWebhook.log`.

## Ranked and leaderboard

`LEADERBOARD=true` turns on ranked play and a points leaderboard, built only on what the server logs:

- **Ranked:** a player is ranked when their game reports no mods that change scoring or physics (the server's `SCORE_CHECK`, default `warn`) and they have not been caught with a sped-up game this session (`SPEED_CHECK`). While the server lets players skate with their own physics tuning (`ENFORCE_TUNING=false`, or `tuning-enforce off` in the console), nobody is ranked: the server cannot see whose tuning is edited. About 15 s after joining, every player gets a DM saying whether they are ranked, and another one when that changes.
- **Points:** from finished throwdowns with at least two players who did not quit. Jam and Spot Battle by place (`LEADERBOARD_POINTS`, default `10,6,4,2`: 1st 10, 2nd 6, 3rd 4, everyone after 2); S.K.A.T.E. gives the last value to everyone who finished, because the log has no winner. Unranked players keep their place but get no points. Free skating earns nothing: the server never sees those tricks. Needs `ACTIVITY_LOG` on (the default).
- **Posting:** every `LEADERBOARD_INTERVAL` minutes the top 3 are announced on screen and the top `LEADERBOARD_SIZE` posted in chat (only while players are on), and the top 10 go to the `leaderboard` webhooks (else `public`, see [Discord webhook](#discord-webhook)) when the board changed since the last post.

Settings (`LEADERBOARD_*`, shared board across servers, DM texts): see [Leaderboard in ENVIRONMENT.md](docs/ENVIRONMENT.md#leaderboard).

## Multi-server support

Run one container per server from the same image. Each needs its **own** data volume, its **own** `PORT` / `QUERY_PORT`, and its own name and map. Everything else can be shared.

1. Shared settings go in `common.env`, only the differences in `server1.env`, `server2.env`, ...:

   ```env
   # server1.env
   SERVER_NAME="My Servers 1 - Street"
   MAP=Industrial Zone
   PORT=12400
   QUERY_PORT=12401
   ```
   ```env
   # server2.env
   SERVER_NAME="My Servers 2 - Skate 2"
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

- The server binary is baked into the image. ReSkate 1.1.4+ can update itself, but a self-updated binary is lost when the container is recreated, so the image keeps it off and does it itself: `UPDATE_MODE=auto` installs releases into `/data/server` (see [Updates](docs/ENVIRONMENT.md#updates)). With the default `pinned`, updating means a new image.
- **Update:** `docker compose pull && docker compose up -d` (join codes can change). If the new tag is not on Docker Hub yet, build it yourself, see [Building](#building).
- **Automatic updates:** `UPDATE_MODE=auto`, see [Updates](docs/ENVIRONMENT.md#updates).
- **Get told about releases:** set a webhook with `WEBHOOK_SCOPE_<n>=admin`, see [Discord webhook](#discord-webhook).
- **Without Discord:** `check-update.sh [container]` compares the running image with the latest ReSkate release (exit code 10 = update available). `NOTIFY_WEBHOOK` sends a POST once per release. Cron: `7 */3 * * * /path/to/check-update.sh reskate-server-1`.

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
| Restart loop, log says `Config problem` | Read the rest of the line. Usually an env value the server rejects, e.g. an unquoted `#` / `'` in an env file (double-quote those). An invalid `SERVER_NAME` is cleaned automatically (`[config]` log line). |
| Container `unhealthy` | Stuck Steam sign-in or a config problem, check `docker logs`. |
| Restart loop, `Config problem: map ... is not a known map` or `map_pool` | `MAP` or a `MAP_POOL` name is neither built in nor the `displayName` of an installed mod. The `[maps] WARNING` log line lists the installed names; a mod update can rename its map. Check `[mods]` lines if a mod failed to install. |
| `MODS` entry not installed | Look for `[mods]` lines in `docker logs <container>`. Names are `Owner-Name` as shown on Thunderstore, and `/data` needs free space (maps can be 1 GB or more). |
| Settings don't change | Empty variables are ignored. Use `off` to clear `SERVER_PASSWORD` / `WELCOME_MESSAGE`. |

## Slim image

`dudedankdave/reskate-server:slim` (and `<version>-slim`, e.g. `2.0.5-slim`) is the same server in a distroless image: **87 MB instead of 172 MB**, with no shell, package manager, Python or curl (see the table below). Same environment variables, Discord webhook and `/data` layout, so just change the tag:

```yaml
    image: dudedankdave/reskate-server:slim
```

- One static Go binary (`/app/reskate`) replaces the entrypoint, Discord sidecar and healthcheck.
- Source: [`slim/`](slim/). Build from the repo root: `docker build -f slim/Dockerfile --build-arg VERSION=2.0.5 -t dudedankdave/reskate-server:slim .`
- `latest` and the plain tags stay the default image. The Discord update message points at the plain tag, so run `docker compose pull` on a slim setup once its `slim` tag is refreshed.

| | Default (`latest`, `2.0.5`) | `slim` |
|---|---|---|
| Base image | `debian:trixie-slim` | `gcr.io/distroless/cc-debian13` |
| ReSkate server | Native Linux x86_64 build from the release (no Wine), with the Steam libraries from the same archive (`libsteam_api.so`, `steamclient.so`, `libtier0_s.so`, `libvstdlib_s.so`) | same |
| Thunderstore download and unzip | `curl` + `unzip` | Built into the Go binary |
| Entrypoint, Discord sidecar, healthcheck | Python 3 scripts (`python3-minimal`) | One static Go binary (`/app/reskate`) |
| Init | `tini` | `tini` |
| `curl` | Real `curl` | Small stand-in inside the Go binary, see below |
| Shell | bash | Static busybox, `sh` only |
| TLS (OpenSSL, CA certificates) | `libssl3`, `ca-certificates` | Included in the base image |
| Package manager | apt | None |
| Debugging | `docker exec -it <container> bash` | `docker exec -it <container> busybox sh` |

**curl:** the server reads the ReSkate team's global ban list from `api.reskate.dev` at startup and every ten minutes by running one fixed command through `sh`:

```
curl --silent --show-error --fail --max-time 15 --max-filesize N --proto =https --user-agent ReSkateServer/1 <url>
```

The default image ships real `curl`. The slim image ships a stand-in for exactly that command inside `/app/reskate`; if a ReSkate release changes the command, the stand-in refuses the unknown option and needs an update. Without `curl` the log says `The global ban list could not be read` and only the server's own bans apply.

## Building

The server binaries are proprietary and not part of this repo. Download `ReSkateServer-Linux-<version>.tar.gz` from a [ReSkate release](https://github.com/Dingo-Shenanigans/ReSkate/releases) and copy `ReSkateServer`, `libsteam_api.so`, `libtier0_s.so`, `libvstdlib_s.so` and `steamclient.so` into `./Server/`, then:

```bash
docker build --build-arg VERSION=2.0.5 -t dudedankdave/reskate-server:2.0.5 .
```

Use the release version as `VERSION`, and tag the image `<major>.<minor>` and `latest` as well.

**Automatic:** `.github/workflows/release.yml` checks for a new ReSkate release every 30 min and, when Docker Hub has no tag for it yet, builds both images from `main` and pushes `<ver>`, `<major>.<minor>`, `latest`, `<ver>-slim` and `slim` (repository secrets `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN`). It can also be started by hand (Actions, *Release image*, *Run workflow*), with *push* off for a test build.

| File | Purpose |
|---|---|
| `Dockerfile` | Image build (Debian slim, tini, curl, minimal Python) |
| `entrypoint.py` | Env vars to `ReSkateServer.json`, starts the Discord sidecar, then the server |
| `notifier.py` | Discord feeds and the update policy |
| `updater.py` | `/data/server`, downloading and verifying releases |
| `supervisor.py` | Runs the server as a child: console relay, restart after an update, rollback |
| `leaderboard.py` | Ranked DMs, throwdown points and the leaderboard posts |
| `healthcheck.py` | Reports healthy once the log shows the server is up |
| `check-update.sh` | Host-side release check |
| `.github/workflows/release.yml` | Builds and pushes the images for each new ReSkate release |
| `rollover/` | Host-side service: starts a copy on a new ReSkate release while players are on, stops the old server once empty |
| `hub-readme.py` | README for Docker Hub (relative links made absolute, 25 KB limit) |
| `docs/ENVIRONMENT.md` | All environment variables |
| `slim/` | Source and Dockerfile of the slim (distroless) image |
| `example.env`, `docker-compose.yml` | Starting point for your own setup |
