# tools: one stack, one container per service

`docker compose -p tools up -d --build` in this folder runs: `rollover`, `status` (Discord cards),
`bot` (Discord bot) and `leaderboard-backup`. Each is its own container, so updating one leaves the others alone.

The leaderboard database is not a service: the game servers write it themselves (SQLite in
`/shared`). `leaderboard-backup` is its container: an hourly online backup into `/root/reskate/backup`
(168 files per database, oldest overwritten). `/shared` is mounted read-only there.

## Switching over on the host
1. Start with `DRY_RUN=true`, check the logs.
2. Stop the old `reskate-rollover`, `reskate-status` and `reskate-bot` first (same container names,
   and two rollovers would fight), then set `DRY_RUN=false` and recreate `rollover`.
The `status` volume is the existing `reskate-status`, so the same Discord message keeps being edited.
No game server is restarted by any of this.
