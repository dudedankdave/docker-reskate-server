# tools: one stack, one container per service, all from the base image

`docker compose -p tools up -d --build` in this folder runs: `rollover`, `status` (Discord cards),
`bot` (Discord bot) and `leaderboard-backup`. Each is its own container of the base image `dudedankdave/reskate-server` (full image, the
`-slim` one has no Python) started with `RESKATE_MODE=rollover|status|bot|backup`; `server` is the game server and the default. Each is its own container, so updating one leaves the others alone.

The leaderboard database is not a service: the game servers write it themselves (SQLite in
`/shared`). `leaderboard-backup` is its container: an hourly online backup into `/root/reskate/backup`
(168 files per database, oldest overwritten). `/shared` is mounted read-only there.

## Switching over on the host
1. Start with `DRY_RUN=true`, check the logs.
2. Stop the old `reskate-rollover`, `reskate-status` and `reskate-bot` first (same container names,
   and two rollovers would fight), then set `DRY_RUN=false` and recreate `rollover`.
The `status` volume is the existing `reskate-status`, so the same Discord message keeps being edited.
No game server is restarted by any of this.

The tools run the version of the scripts that is in the image tag (`RESKATE_TAG`, default `latest`);
to update one, pull and recreate just that service. The first image with these modes is the next release.
