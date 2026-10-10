<p align="left">
  <img src="https://raw.githubusercontent.com/dudedankdave/docker-reskate-server/main/assets/icon.png" width="128" height="128" alt="ReSkate server icon">
</p>

# ReSkate - Dedicated Server - Docker 

[Docker Hub](https://hub.docker.com/r/dudedankdave/reskate-server) · [GitHub](https://github.com/dudedankdave/docker-reskate-server) · [ReSkate releases](https://github.com/Dingo-Shenanigans/ReSkate/releases)

Docker image for the [ReSkate](https://github.com/Dingo-Shenanigans/ReSkate) dedicated server:

- **Configured with environment variables.** They are written to `/data/ReSkateServer.json` on every start (older configs are moved to the 1.1.7 layout first). Unset or empty variables leave the existing value alone, so changes made in-game or from the console survive restarts.
- **Multi-server support:** run several servers on one host, each with its own data volume.
- **Discord webhook:** forwards the console and announces new ReSkate releases.
- **Healthcheck** that reports `healthy` once the server is up on its map.

> **The server must run the same ReSkate version as the players.** If it falls behind, it can vanish from the in-game list and join codes time out, even though the container is `healthy`. See [Keeping up to date](#keeping-up-to-date).

---

## Contents

[Quick start](#quick-start) · [About](#about) · [Configuration](#configuration) · [Discord webhook](#discord-webhook) · [Multi-server support](#multi-server-support) · [Slim image](#slim-image) · [Keeping up to date](#keeping-up-to-date) · [Networking](#networking) · [Troubleshooting](#troubleshooting) · [Building](#building)

---
<br/>

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
<br/>

1. Copy `example.env` to `common.env` (shared settings) and `server1.env` (name, map, ports).
2. `docker compose up -d`
3. `docker logs reskate-server-1` shows the **join code** once the server is up.

<br/>

| Task | Command |
|---|---|
| Console | `docker attach reskate-server-1` (detach with Ctrl-P Ctrl-Q) |
| Status | `docker ps` (`healthy` once the log reports `... is up on <map>`) |
| Update | `docker compose pull && docker compose up -d` |
| Pin a version | `RESKATE_VERSION=2.0.2 docker compose up -d` (default `latest`) |

<br/>

Image tags match the [ReSkate release](https://github.com/Dingo-Shenanigans/ReSkate/releases): `2.0.2`, `2.0`, `latest`, plus the smaller [`slim`](#slim-image) variant. The healthcheck turns `unhealthy` if the last startup event is a stuck Steam sign-in or a `Config problem`.

Without `STEAM_TOKEN` every restart gives the server a new Steam ID and **join code**. Either way a restart disconnects the players.

<br/>

---

## About


### Tags

| Tag | What it is | Size on disk (download) |
|---|---|---|
| `latest` | Newest ReSkate release, default image (Debian slim) | 172 MB (64 MB) |
| `2.0.2`, `2.0` | That ReSkate release, pinned (`2.0` follows the newest `2.0.x`; `1.1` stays on 1.1.8) | 172 MB (64 MB) |
| `slim` | Newest ReSkate release, [slim (distroless) image](#slim-image) | 87 MB (34 MB) |
| `2.0.2-slim` | That ReSkate release, pinned, slim | 87 MB (34 MB) |

The version in a tag is the ReSkate release of the server inside. It has to match the players' game, see [Keeping up to date](#keeping-up-to-date). Older tags stay available.

<br/>


### Dependencies

| | Default (`latest`, `2.0.2`) | `slim` |
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

<br/>


### curl

The server reads the ReSkate team's global ban list from `api.reskate.dev` at startup and every ten minutes. It does that by running one fixed command through `sh`:

```
curl --silent --show-error --fail --max-time 15 --max-filesize N --proto =https --user-agent ReSkateServer/1 <url>
```

- **Default image:** ships real `curl`, nothing to do.
- **Slim image:** ships a small stand-in for exactly that command, built into `/app/reskate`. If a ReSkate release changes the command, the stand-in refuses the unknown option and needs an update.
- **Without `curl`:** the log says `The global ban list could not be read` and only the server's own bans apply.

<br/>

---

## Configuration

Put settings in env files (`example.env` lists all of them). Values containing `#` or `'` must be double-quoted. A later `env_file` wins over an earlier one. Unset or empty variables leave the existing value alone. Discord variables: see [Discord webhook](#discord-webhook).

<br/>


### Custom maps and mods

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


&nbsp;

&nbsp;

**Manual: copy the mod folder yourself.**

Custom maps are mod folders in `/data/Mods/<folder>`. The server only reads each mod's `reskate-levels.json`, but **players need the same map mod installed** to join.

1. Copy the map's mod folder from the game's `Mods` folder into the server's volume:

   ```bash
   docker cp <mod-folder> reskate-server-1:/data/Mods/
   ```

2. Set `MAP` to the `displayName` from the mod's `reskate-levels.json`, e.g. `MAP=Skate2Map`.
3. Restart the server: `docker compose restart reskate-1`

Built-in maps need no mod: `San Vansterdam`, `Isle of Grom`, `Super Ultra Mega Resort`, `Stadium 1`. Every server has its own `/data` volume, so copy the mod into each server that should use it.

<br/>


### Per server

| Variable | Values | Description |
|---|---|---|
| `SERVER_NAME` | text, 1-64 chars | Name in the server browser. Only ASCII letters, numbers, spaces and `- _ / [ ] ( )`. Anything else is cleaned automatically (accents removed, `' . # %` dropped, others become `-`), with a `[config]` log line. |
| `MAP` | text | Map everyone skates: `San Vansterdam`, `Isle of Grom`, `Super Ultra Mega Resort`, `Stadium 1`, or the `displayName` of a custom map mod (`reskate-levels.json`). |
| `MAP_POOL` | map list, `off` | Maps players may vote for and the rotation goes through, in order, e.g. `San Vansterdam,Isle of Grom,Skate2Map`. Empty = every map; admins can still pick any. `off` empties it. Pins the list (in-game changes are replaced on restart). Unknown names stop the server (`Config problem`). |
| `MAP_ROTATION_MINUTES` | minutes, `0` = off | Minutes on each map before the server moves to the next one in `MAP_POOL`. Players get a minute's warning; the clock waits while nobody is on and starts over whenever the map changes (by a vote or an admin too). |
| `PORT` | number | Game port. Only used with `USE_STEAM_RELAY=false`. |
| `QUERY_PORT` | number | Server browser / A2S queries. |
| `STEAM_TOKEN` | token, `off` | Steam game server token: keeps the Steam ID across restarts (not always the join code). One per server from steamcommunity.com/dev/managegameservers (App ID 3354750) and keep it private. Empty = anonymous sign-in, a new Steam ID on every start. `off` clears it. |


### Access

| Variable | Values | Description |
|---|---|---|
| `SERVER_PASSWORD` | text, `off` | Join password. Empty = anyone can join; `off` clears an existing password. |
| `LISTED` | `true` / `false` | `false` hides the server from the browser; players then need the join code. |
| `MAX_PLAYERS` | 1-249 | Player limit. |
| `ADMINS` | SteamID64 list | Comma-separated admins who may change settings in-game. Merged. |
| `BANS` | `id[:name]` list | Comma-separated players who can never join. Merged into `/data/data/bans.json`. |
| `GLOBAL_BANS` | `true` / `false` | Turn away players the ReSkate team banned. Default `true`. |
| `WELCOME_MESSAGE` | text, `off` | Chat line sent to each player as they join. `off` clears it. |
| `CHAT_COLOR` | `#RRGGBB` | Colour of the server's "Server" badge and name in chat (default `#8E5CFF`). |
| `CHAT_TEXT_COLOR` | `#RRGGBB` | Colour of the text of the server's chat lines (default `#D9C8FF`). Pick one that reads on a dark background. |
| `RESERVED` | SteamID64 list | Players with a reserved slot (merged). They and admins can join a full server, on top of `MAX_PLAYERS`. |


### Gameplay

| Variable | Values | Description |
|---|---|---|
| `OBJECT_PLACEMENT` | `everyone` `admins` `nobody` | Who can build and place objects. |
| `OBJECT_LIMIT` | 0-1024 | Objects each player may have placed (default `100`), `0` = no limit. Admins are never limited. A player placing more than twice the limit plus 100 in a minute has their objects deleted. |
| `OBJECT_SCALING` | `true` / `false` | Let players place objects bigger or smaller than their own size. Default `true`; admins can always resize. |
| `BONE_SCALE_LIMIT` | 0-8 | How far mods may scale skater bones (big heads) for others. Default `2` (`1` also evens out height), `0` = no limit. |
| `NOCLIP` | `true` / `false` | Let players use noclip (and tp). Default `true`, admins always can. |
| `NO_BAIL` | `true` / `false` | Let players use No Bail. Default `true`, admins always can. |
| `BOOSTS` | `true` / `false` | Let players use the forward and up boosts. Default `true`, admins always can. |
| `ENFORCE_TUNING` | `true` / `false` | Players skate with the game's own physics tuning, not edited copies. Default `true`. |
| `PARTIES` | `true` / `false` | Let players form parties. Default `true`. |
| `PARTY_SIZE` | 2-8 | Most players in one party. Default `8`. |
| `ANNOUNCE_THROWDOWNS` | `true` / `false` | Tell everyone in chat when a throwdown drop is placed. Default `true`. |
| `ANNOUNCE_KICKS` | `true` / `false` | Tell everyone in chat when an admin kicks or bans a player ("Server: Name was kicked."). ReSkate itself only tells the admin. Done by the image, not the server: it types `say` into the server console, which `docker attach` still reaches. Default `true`. |
| `AFK_KICK_MINUTES` | 0-1440 | Remove a player who has been away (not moving, speaking, chatting or editing objects) this many minutes, after a warning a minute before. Default `0` = never; admins are never removed. |
| `SYNC_EFFECTS` | `true` / `false` | Let players see each other's skater effects (sparks, dust, costume and board trails). `false` saves a little traffic on busy servers. Default `true`. |
| `ACTIVITY_LOG` | `true` / `false` | Log what players do: throwdowns, joins, objects placed or removed, load times. Default `true`. |


### Anti-cheat

| Variable | Values | Description |
|---|---|---|
| `SPEED_CHECK` | `off` `warn` `kick` | Catch players whose game runs faster than normal (speedhack). `warn` (default) takes them out of throwdowns and coop challenges and tells admins. |
| `SCORE_CHECK` | `off` `warn` `kick` | Catch players whose mods change trick scoring or handling. `warn` (default) takes them out of throwdowns and coop challenges. |
| `SCORE_ALLOW` | hex list | Comma-separated scoring fingerprints (16 hex digits) accepted like the game's own, for servers running a scoring mod everyone installs. Merged with the existing list. |


### Voice

| Variable | Values | Description |
|---|---|---|
| `VOICE_CHAT` | `true` / `false` | Allow voice chat. |
| `VOICE_RANGE` | 50-1000 | How far proximity voice reaches, in metres. |


### Network

| Variable | Values | Description |
|---|---|---|
| `USE_STEAM_RELAY` | `true` / `false` | `false`: players connect straight to `PORT` (UDP, must be open) for lower ping, falling back to Steam's relays. Default `true` (relays only). |
| `SEND_RATE` | 128-16384 | Most the server sends one player, KB/s (default `900`). About 1100 is what reaches a player through Steam's relays: set higher, packets are lost and resent. |
| `CROWD_BUDGET` | number, `0` = no limit | Most position updates per second one player is sent (default `600`). Only matters in crowds (about 30 players): the farthest drop to 10 and 5 updates a second. |
| `PACK_MS` | 0-50 | ms a message may wait to share a packet (default `10`, `0` = send at once). |
| `FINGER_DISTANCE` | metres | Beyond this a player's fingers aren't sent (default `25`, `0` = always). |
| `THREADS` | 0-64 | Threads that build each player's updates (default `0` = one per processor but one, up to 8; `1` = single thread). The log says how many on start. |
| `STEAM_DEBUG` | `true` / `false` | Log Steam networking details (connection problems). Verbose. |
| `DISTANCE_FULL_RATE_RETURN` | metres | Players closer than this are back at the full update rate. |
| `DISTANCE_HALF_RATE_START` | metres | Players farther than this update at half rate. |
| `DISTANCE_HALF_RATE_RETURN` | metres | Players closer than this are back at half rate. |
| `DISTANCE_LOW_RATE_START` | metres | Players farther than this update at the low rate. |


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
| `VOTE_<MAP\|KICK\|TIME_OF_DAY>_SECONDS` | seconds, `0` | That vote's own run time. `0` (default) = `VOTE_SECONDS`. |
| `VOTE_<MAP\|KICK\|TIME_OF_DAY>_COOLDOWN_SECONDS` | seconds, `0` | That vote's own cooldown. `0` (default) = `VOTE_COOLDOWN_SECONDS`. |
| `VOTE_<MAP\|KICK\|TIME_OF_DAY>_MIN_PLAYERS` | 1-249 | Players who must be on before anyone can start that vote. Default `1`. |
| `VOTE_STARTER_YES` | `true` / `false` | Whoever starts a vote has voted yes. Default `true`. |
| `VOTES_CUSTOM` | JSON list, `off` | Votes of your own, each running a server command when it passes, e.g. `[{"name": "restart", "description": "Reload the map", "command": "map {map}", "percent": 60}]`. `{map}` = current map, `{arg}` = the choice picked from `"choices"`. Players use `/vote restart`, `/vote list`. Up to 16, names 1-16 of `a-z 0-9 - _`. Each takes `enabled`, `percent`, `seconds`, `cooldown_seconds`, `min_players`. Pins the list; `off` clears it. Single-quote the value in env files. |
| `POLLS` | `off` `admins` `everyone` | Who may ask everyone a question (`/poll Next map? \| Grom \| Stadium`, answers with `/1`, `/2`...). Default `admins`. |
| `POLL_SECONDS` | seconds | How long a poll runs. Default `60`. |


### Announcements

| Variable | Values | Description |
|---|---|---|
| `ANNOUNCEMENTS` | `\|`-separated lines, `off` | Chat lines the server posts in turn while players are on, e.g. `Join the Discord: discord.gg/xyz\|Be nice`. Pins the list (console `announcements add` is replaced on restart); `off` clears it. |
| `ANNOUNCEMENT_INTERVAL_MINUTES` | minutes, `0` = off | Minutes between announcements. Default `0`. |
| `ANNOUNCEMENT_CARD` | `true` / `false` | Also show each announcement as a card at the top of every player's screen. Default `true`. |


### Parks and layers

| Variable | Values | Description |
|---|---|---|
| `PARK_CONSTRUCTION` | park id | Layout of the construction park lot, e.g. `skatepark_01`, or `empty`. |
| `PARK_HISTORIC` | park id | Layout of the historic park lot, e.g. `megapark_05`, or `empty`. |
| `PARK_FINANCIAL` | park id | Layout of the financial park lot, e.g. `flumppark_08`, or `empty`. |
| `WORLD_LAYER_SYNC` | `true` / `false` | Force the `LAYERS` below on every player. |
| `LAYERS` | `key=on\|off\|default` list | World layers, comma-separated, e.g. `key=on,other=off`. `default` removes the setting. |


### Mods

| Variable | Values | Description |
|---|---|---|
| `MODS` | package list | Thunderstore packages to install into `/data/Mods` on start: `Owner-Name`, `Owner-Name-1.2.3` or a package URL, comma or space separated. See [Custom maps and mods](#custom-maps-and-mods). |
| `MODS_UPDATE` | `true` / `false` | `false` keeps installed versions even when a newer one exists (pinned entries and missing packages are still installed). Default `true`. |


### Updates

| Variable | Values | Description |
|---|---|---|
| `AUTO_UPDATE` | `true` / `false` | ReSkate 1.1.4+ can replace its own binary. The image passes `--no-update` unless this is `true`: a self-updated binary is lost on recreate, so pull a new image instead. The slim image cannot self-update (no `tar`). |

<br/>

### Data

`/data` (a volume) holds `ReSkateServer.json`, `ReSkateServer.log`, `data/bans.json`, `Mods/` and `world-layers.json`.

<br/>

---

## Discord webhook

Two webhooks per server, so admins and players can have their own channels. Either one works alone.

- **Admin feed** (`DISCORD_WEBHOOK_ADMIN`): everything the server prints, as code blocks: joins with Steam IDs, leaves with reasons, `[admin]`, `[chat]`, `[objects]`, `[join]` problems, throwdowns, config problems and the start-up lines including the **join code**. `docker attach` keeps working. It also gets the **update messages**: **UPDATE AVAILABLE** per new ReSkate release (checked every 3 hours, and again when the Docker Hub image is published), and for [Thunderstore mods](#custom-maps-and-mods) **MOD UPDATE AVAILABLE** per new version (hourly) plus **MOD INSTALLED** / **MOD UPDATED** after a start. Only update messages can mention anyone.
- **User feed** (`DISCORD_WEBHOOK_USER`): only what players should see, cleaned up: joins (`Name joined, 3/100 players`), leaves (`Name left`), throwdown lines and the "is up on" line. No Steam IDs, no leave reasons, no chat, no admin commands, no join code.

<br/>

| Variable | Meaning |
|---|---|
| `DISCORD_WEBHOOK_ADMIN` | Webhook URL for the admin feed (Discord: channel settings, Integrations, Webhooks). Unset = no admin feed and no update messages. Older name: `DISCORD_WEBHOOK`. |
| `DISCORD_WEBHOOK_USER` | Webhook URL for the user feed. Unset = no user feed. |
| `DISCORD_MENTION_IDS` | Comma-separated Discord user ids to mention in the update messages of the admin feed, e.g. `123456789012345678,234567890123456789`. |
| `DISCORD_CONSOLE` | `false` = no console lines in the admin feed, only the update messages (default `true`). The user feed is not affected. |
| `DISCORD_USERNAME` | Name shown on the posts. Default is `SERVER_NAME` without any `discord...` word, which Discord rejects in webhook names. |

<br/>

- Player names and chat can never ping anyone: all console posts disable mentions.
- Each server sends its own update messages. If several servers share one admin webhook, set `DISCORD_MENTION_IDS` on one of them only.
- The admin feed contains the **join code**, Steam IDs and chat. Use a channel only people you trust can read. The user feed is safe for a public channel.
- Failures (bad URL, rate limits) never affect the server; they are written to `/data/DiscordWebhook.log`.

<br/>

---

## Slim image

`dudedankdave/reskate-server:slim` (and `<version>-slim`, e.g. `2.0.2-slim`) is the same server in a distroless image: **87 MB instead of 172 MB**, with no shell, package manager, Python or curl (see [About](#about)). Same environment variables, Discord webhook and `/data` layout, so just change the tag:

```yaml
    image: dudedankdave/reskate-server:slim
```

<br/>

- One static Go binary (`/app/reskate`) replaces the entrypoint, Discord sidecar and healthcheck.
- Source: [`slim/`](slim/). Build from the repo root: `docker build -f slim/Dockerfile --build-arg VERSION=2.0.2 -t dudedankdave/reskate-server:slim .`
- `latest` and the plain tags stay the default image. The Discord update message points at the plain tag, so run `docker compose pull` on a slim setup once its `slim` tag is refreshed.

<br/>

---

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

<br/>

---

## Keeping up to date

ReSkate releases often and the game client updates itself, so the server has to follow.

- The server binary is baked into the image. ReSkate 1.1.4+ can update itself, but a self-updated binary is lost when the container is recreated, so this image keeps that off (see `AUTO_UPDATE`). Updating means a new image.
- **Update:** `docker compose pull && docker compose up -d` (join codes can change). If the new tag is not on Docker Hub yet, build it yourself, see [Building](#building).
- **Get told about releases:** set `DISCORD_WEBHOOK`, see [Discord webhook](#discord-webhook).
- **Without Discord:** `check-update.sh [container]` compares the running image with the latest ReSkate release (exit code 10 = update available). `NOTIFY_WEBHOOK` sends a POST once per release. Cron: `7 */3 * * * /path/to/check-update.sh reskate-server-1`.

<br/>

---

## Networking

- Players join through the **Steam relay** on a random ephemeral UDP port. `PORT` is never bound; `QUERY_PORT` serves the server browser.
- Only `QUERY_PORT` needs to be reachable from the internet. Open it for UDP.
- With a **stateless firewall**, also allow replies from the Steam relay: UDP from source ports 27000-27200 to destination ports 32768-65535. Without that rule, joins time out.
- `network_mode: host` is required, the relay breaks behind bridge NAT.

<br/>

---

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

<br/>

---

## Building

The server binaries are proprietary and not part of this repo. Download `ReSkateServer-Linux-<version>.tar.gz` from a [ReSkate release](https://github.com/Dingo-Shenanigans/ReSkate/releases) and copy `ReSkateServer`, `libsteam_api.so`, `libtier0_s.so`, `libvstdlib_s.so` and `steamclient.so` into `./Server/`, then:

```bash
docker build --build-arg VERSION=2.0.2 -t dudedankdave/reskate-server:2.0.2 .
```

<br/>

Use the release version as `VERSION`, and tag the image `<major>.<minor>` and `latest` as well.

| File | Purpose |
|---|---|
| `Dockerfile` | Image build (Debian slim, tini, curl, minimal Python) |
| `entrypoint.py` | Env vars to `ReSkateServer.json`, starts the Discord sidecar, then the server |
| `notifier.py` | Discord console forwarding and update announcements |
| `healthcheck.py` | Reports healthy once the log shows the server is up |
| `check-update.sh` | Host-side release check |
| `hub-readme.py` | README for Docker Hub (env tables replaced by a link, 25 KB limit) |
| `slim/` | Source and Dockerfile of the slim (distroless) image |
| `example.env`, `docker-compose.yml` | Starting point for your own setup |
