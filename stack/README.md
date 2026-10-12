# tools: everything next to the game servers

`docker compose -p tools up -d` in this folder runs, each as its own container of the tools image
`dudedankdave/reskate-tools` (built from [`Dockerfile`](Dockerfile), the mode picked by `RESKATE_MODE`, see
[`modules/tools.py`](../modules/tools.py)). Updating one leaves the others and the game servers alone.

| Service | What it does | Reaches the servers through |
|---|---|---|
| `bridge` | Server logs as events, console actions, server status ([API](../rollover/README.md#bridge-servers---bot)) | Docker socket |
| `rollover` | New ReSkate releases, map updates, changed settings, image revisions: on empty servers, or a copy in the other slot | Docker socket |
| `bot` | The Discord bot: status cards, slash commands, `/coach`, feeds ([README](../bot/README.md)) | bridge |
| `leaderboard` | The leaderboard of the servers that hand it over | bridge |
| `webhooks` | Their Discord webhooks | bridge |
| `settings` | Their settings changes, typed into the running servers; `roles reload` | bridge |
| `announcer` | Their kick and ban announcements | bridge |
| `leaderboard-backup` | Hourly online backups of the leaderboard databases into `/root/reskate/backup` (168 per database) | `/shared` |

Which parts of which server the modules take over is set in `/shared/modules.json` while everything runs
([Modules and events](../docs/ENVIRONMENT.md#modules-and-events)); without it they do nothing and the servers keep
doing it themselves. How the parts fit together: [docs/MODULES.md](../docs/MODULES.md).

- `BRIDGE_TOKEN` (any long random string) and `DISCORD_TOKEN` come from the host's environment, never the repository.
- `reskate-shared` is the volume the servers mount at `/shared` (leaderboard, roles, `settings/`, `modules.json`).
  It is external: `docker volume create reskate-shared` once, or give it `name:` of the volume your servers already
  use there.
- `docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v reskate-shared:/shared dudedankdave/reskate-tools split-settings --write`
  writes the running servers' env out as settings files into it ([Settings files](../docs/ENVIRONMENT.md#settings-files));
  without `--write` it only lists the keys.
- The bridge and the rollover run as root (the Docker socket); the rest as uid 1000, like the servers.
- `TOOLS_TAG` picks the image (`latest`, or a revision such as `r160`); pull and recreate one service to update it.

## Switching over on the host
1. Start with `DRY_RUN=true` on the rollover, check the logs.
2. Stop the old `reskate-rollover`, `reskate-status` and `reskate-bot` first (same container names, and two
   rollovers would fight), then set `DRY_RUN=false` and recreate `rollover`.
3. Hand parts over per server in `/shared/modules.json` once the stack runs (see
   [Switching a stack over](../docs/MODULES.md#switching-a-stack-over)).

No game server is restarted by any of this.
