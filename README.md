# ReSkate dedicated server

[Docker Hub](https://hub.docker.com/r/dudedankdave/reskate-server) · [GitHub](https://github.com/dudedankdave/docker-reskate-server) · [ReSkate releases](https://github.com/Dingo-Shenanigans/ReSkate/releases)

Docker image for the ReSkate dedicated server (the native Linux build, no Wine). Settings are passed as environment variables and written to `/data/ReSkateServer.json` on every start. Unset or empty variables leave the existing value alone.

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

Copy `example.env` to `common.env` (shared) and `server1.env` (name, map, ports), then `docker compose up -d`.

- Console: `docker attach reskate-server-1` (detach with Ctrl-P Ctrl-Q)
- Update: `docker compose pull && docker compose up -d`
- Pin a version: `RESKATE_VERSION=1.1.1 docker compose up -d` (default is `latest`). Tags: `1.1.1`, `1.1`, `latest`, ... (the tag matches the [ReSkate release](https://github.com/Dingo-Shenanigans/ReSkate/releases)).
- Health: `docker ps` shows `healthy` once the log reports `... is up on <map>`. It turns `unhealthy` if the last startup event is a stuck Steam sign-in or a `Config problem`.
- Several servers: see below.
- Keep the server on the same ReSkate version as your players, see [Keeping up to date](#keeping-up-to-date).

## Keeping up to date

**The server must run the same ReSkate version as the players' game.** ReSkate releases often and the client updates itself. If the server falls behind, it can disappear from the in-game server list and join codes time out, even though the container is `healthy` and registered on Steam. Other players who have not updated yet can still join, which makes this easy to miss.

- The server binary is baked into the image. `AUTO_UPDATE` only sets a key in `ReSkateServer.json`; it does not update the container.
- To update: `docker compose pull && docker compose up -d`. This restarts the servers, which disconnects players and changes the Steam ID and join code.
- If the new tag is not on Docker Hub yet, build it yourself (see [Building](#building)).
- With `DISCORD_WEBHOOK` set, the image announces new releases itself, see [Discord webhook](#discord-webhook).
- `check-update.sh` (host-side alternative) compares the running image's version with the latest ReSkate GitHub release and tells you whether `docker compose pull` is enough. Run it from cron to get notified:

  ```bash
  # every 3 hours; set NOTIFY_WEBHOOK to a URL to get a POST once per new release
  7 */3 * * * /path/to/check-update.sh reskate-server-1 >> /var/log/reskate-update-check.log 2>&1
  ```

## Discord webhook

Set `DISCORD_WEBHOOK` (per server, in `serverN.env`) to get the server in Discord:

- **Console:** every line the server prints (joins, leaves, admin commands, throwdowns, ...) is posted as a code block, batched every few seconds. `docker attach` keeps working.
- **Updates:** when a new ReSkate release is out, the server posts **UPDATE AVAILABLE** once per release (the image checks GitHub every 3 hours), and again when the matching Docker Hub image is published. Only this message can mention anyone.

| Variable | Meaning |
|---|---|
| `DISCORD_WEBHOOK` | Webhook URL (Discord: channel settings, Integrations, Webhooks). Unset = off. |
| `DISCORD_MENTION_IDS` | Comma-separated Discord user ids to mention in the update message, e.g. `123456789012345678,234567890123456789`. |
| `DISCORD_CONSOLE` | `false` = only post update messages, no console output (default `true`). |
| `DISCORD_USERNAME` | Name shown for the posts. Default is `SERVER_NAME` without any `discord...` word, which Discord rejects in webhook names. |

Notes:
- Player names and chat can never ping: console posts disable all mentions.
- Each server sends its own update message. If several servers share one webhook, set `DISCORD_MENTION_IDS` on one of them only.
- The console includes the **join code**, so post it to a channel only people you trust can read.
- Failures (bad URL, rate limits) never affect the server; they are written to `/data/DiscordWebhook.log`.
- The last announced version is kept in `/data/.discord-update-notified`.

## Running multiple servers

Run one container per server from the same image. Each server needs its **own** data volume, its **own** `PORT`/`QUERY_PORT`, and its own name and map. Everything else can be shared.

1. Put shared settings in `common.env`, and only the differences in `server1.env`, `server2.env`, ...:

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

2. Add a service and a volume per server (see `docker-compose.yml`, where the second server is a ready-to-uncomment block). With `env_file: [common.env, serverN.env]` the later file wins, so per-server values override shared ones.

3. Start them: `docker compose up -d`. Manage one with `docker compose restart reskate-2`, attach with `docker attach reskate-server-2`.

Notes:
- All servers use `network_mode: host`, so ports must be unique across the host. Pick a block per server (e.g. 12400/12401, 12410/12411, 12420/12421).
- Only `QUERY_PORT` needs to be reachable for the server browser; players connect through the Steam relay (see Notes above).
- Give each server its own `/data` volume. Sharing one volume makes the servers overwrite each other's `ReSkateServer.json`, mods and logs.
- Custom map mods go into each server's own volume: `docker cp <mod-folder> reskate-server-2:/data/Mods/` then restart that server.

## Notes

- `/data` holds `ReSkateServer.json`, `ReSkateServer.log`, `Mods/` and `world-layers.json`. Custom maps go in `/data/Mods/<folder>`; set `MAP` to the displayName from the mod's `reskate-levels.json`.
- Players join through the Steam relay on a random ephemeral UDP port. `PORT` is never bound; `QUERY_PORT` serves the server browser. If you have a stateless firewall, allow UDP from source ports 27000-27200 to 32768-65535.
- `SERVER_NAME` max 64 characters; longer causes a restart loop. Quote values containing `#` or `'`.
- `PASSWORD`/`WELCOME_MESSAGE`: set to `off` to clear.

## Environment variables

`SERVER_NAME`, `MAP`, `PORT`, `QUERY_PORT`, `MAX_PLAYERS`, `SERVER_PASSWORD`, `WELCOME_MESSAGE`, `LISTED`, `AUTO_UPDATE`, `ADMINS`, `BANS`, `TPS` (20/30/60/120), `NOCLIP`, `NO_BAIL`, `BOOSTS`, `ENFORCE_TUNING`, `OBJECT_PLACEMENT` (everyone/admins/nobody), `PARTIES`, `PARTY_SIZE`, `ANNOUNCE_THROWDOWNS`, `ACTIVITY_LOG`, `SPEED_CHECK`/`SCORE_CHECK` (off/warn/kick), `SCORE_ALLOW`, `VOICE_CHAT`, `VOICE_RANGE`, `DISTANCE_*`, `VOTE_*`, `PARK_CONSTRUCTION`/`PARK_HISTORIC`/`PARK_FINANCIAL`, `WORLD_LAYER_SYNC`, `LAYERS`. See `example.env` for defaults. Discord: `DISCORD_WEBHOOK`, `DISCORD_MENTION_IDS`, `DISCORD_CONSOLE`, `DISCORD_USERNAME` (see [Discord webhook](#discord-webhook)).

## Building

The server binaries are proprietary and not part of this repo. Put the dedicated-server files in `./Server/` (`ReSkateServer`, `libsteam_api.so`, `libtier0_s.so`, `libvstdlib_s.so`, `steamclient.so`), then:

```bash
docker build --build-arg VERSION=1.1.1 -t dudedankdave/reskate-server:1.1.1 .
```

Get the files from the Linux server download of a [ReSkate release](https://github.com/Dingo-Shenanigans/ReSkate/releases) (`ReSkateServer-Linux-<version>.tar.gz`). Use the release version as `VERSION`, then tag it `<major>.<minor>` and `latest` as well.
