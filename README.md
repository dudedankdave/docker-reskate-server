# ReSkate dedicated server

Docker image for the ReSkate dedicated server. Settings are passed as environment variables and written to `/data/ReSkateServer.json` on every start. Unset or empty variables leave the existing value alone.

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
- Several servers: one service per server, each with its own volume, ports and `serverN.env`.

## Notes

- `/data` holds `ReSkateServer.json`, `ReSkateServer.log`, `Mods/` and `world-layers.json`. Custom maps go in `/data/Mods/<folder>`; set `MAP` to the displayName from the mod's `reskate-levels.json`.
- Players join through the Steam relay on a random ephemeral UDP port. `PORT` is never bound; `QUERY_PORT` serves the server browser. If you have a stateless firewall, allow UDP from source ports 27000-27200 to 32768-65535.
- `SERVER_NAME` max 64 characters; longer causes a restart loop. Quote values containing `#` or `'`.
- `PASSWORD`/`WELCOME_MESSAGE`: set to `off` to clear.

## Environment variables

`SERVER_NAME`, `MAP`, `PORT`, `QUERY_PORT`, `MAX_PLAYERS`, `SERVER_PASSWORD`, `WELCOME_MESSAGE`, `LISTED`, `AUTO_UPDATE`, `ADMINS`, `BANS`, `TPS` (20/30/60/120), `NOCLIP`, `NO_BAIL`, `BOOSTS`, `ENFORCE_TUNING`, `OBJECT_PLACEMENT` (everyone/admins/nobody), `PARTIES`, `PARTY_SIZE`, `ANNOUNCE_THROWDOWNS`, `ACTIVITY_LOG`, `SPEED_CHECK`/`SCORE_CHECK` (off/warn/kick), `SCORE_ALLOW`, `VOICE_CHAT`, `VOICE_RANGE`, `DISTANCE_*`, `VOTE_*`, `PARK_CONSTRUCTION`/`PARK_HISTORIC`/`PARK_FINANCIAL`, `WORLD_LAYER_SYNC`, `LAYERS`. See `example.env` for defaults.
