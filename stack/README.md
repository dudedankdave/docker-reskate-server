# tools: rollover, status cards and Discord bot in one container

`reskate-tools` runs the three helper services as separate processes of one container
(`launcher.py` restarts a crashed one after 5 s without touching the others). `TOOLS=rollover,status`
leaves the bot out.

What does **not** move in: the leaderboard database. The game servers write it themselves
(`app/leaderboard.py`, SQLite in `/shared`); it is data, not a service. The tools container only
mounts the same folder, which the bot reads.

## Switching over on the host
1. `docker build` / `docker compose up -d --build` here with `DRY_RUN=true` first and check the logs.
2. To keep the existing status message, mount the old volume: `reskate-status:/state` (external) instead of `state`.
3. Stop `reskate-rollover`, `reskate-status`, `reskate-bot` before the tools container acts
   (two rollovers at once would fight), then set `DRY_RUN=false`.
No game server is restarted by any of this.

Trade-off: updating one tool restarts all three (the rollover is stateless, a restart only pauses it for seconds).
