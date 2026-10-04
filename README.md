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
- Several servers: see below.

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

`SERVER_NAME`, `MAP`, `PORT`, `QUERY_PORT`, `MAX_PLAYERS`, `SERVER_PASSWORD`, `WELCOME_MESSAGE`, `LISTED`, `AUTO_UPDATE`, `ADMINS`, `BANS`, `TPS` (20/30/60/120), `NOCLIP`, `NO_BAIL`, `BOOSTS`, `ENFORCE_TUNING`, `OBJECT_PLACEMENT` (everyone/admins/nobody), `PARTIES`, `PARTY_SIZE`, `ANNOUNCE_THROWDOWNS`, `ACTIVITY_LOG`, `SPEED_CHECK`/`SCORE_CHECK` (off/warn/kick), `SCORE_ALLOW`, `VOICE_CHAT`, `VOICE_RANGE`, `DISTANCE_*`, `VOTE_*`, `PARK_CONSTRUCTION`/`PARK_HISTORIC`/`PARK_FINANCIAL`, `WORLD_LAYER_SYNC`, `LAYERS`. See `example.env` for defaults.

## Building

The server binaries are proprietary and not part of this repo. Put the dedicated-server files in `./Server/` (`ReSkateServer`, `libsteam_api.so`, `libtier0_s.so`, `libvstdlib_s.so`, `steamclient.so`), then:

```bash
docker build -t dudedankdave/reskate-server:dev .
```
