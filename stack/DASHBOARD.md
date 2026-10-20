# Dashboard

A password-protected web page for the servers, the rollover, the bridge and the bots
(`RESKATE_MODE=dashboard`, [`modules/dashboard.py`](../modules/dashboard.py), standard library only).

**Stage 1 (this one) starts and stops nothing:** server cards (players, map, version, slots A/B), container state, the live
log from the bridge's events, the last lines of each tool's log, and a chat section with one tab per server. Typing in the chat box sends a `say` through the bridge (audit event "dashboard"); `DASHBOARD_SAY=false` makes the chat read-only.
Each server card says whether the project rule would allow a restart (at most `DASHBOARD_CUTOFF` players, default 3)
or only a rollover copy; an unknown player count counts as busy. Later stages that restart anything reuse that
check on the server side.

**Server commands:** a catalog of every console command of the ReSkate server (checked against the server's own `README.txt`: map, map-pool, rotation, votes and polls, parties, anti-cheat, reserved slots, announcements, `tpall`/`tphere` ...), a free command line, and shortcuts for switching the map, the map pool and the rotation. The reply is the last lines of the server's log, shown after a moment. `quit`, `stop`, `exit`, `update` and `restart` are refused here and in the bridge.

**New map:** "Neue Map hinzufügen" takes a Thunderstore package (`Owner-Name`) and optional pool names and appends `MODS+=` / `MAP_POOL+=` to `/shared/settings/server-<n>.env` (or `common.env` for all servers). The tools' rollover restarts the servers concerned once they are empty (a busy server is never taken down for it); afterwards switch to the map or add it to the pool. This needs `/shared` mounted writable (see `dashboard.yml`). A pool change typed on the console is not kept across a restart when `MAP_POOL` is set in the env or settings files.

**Moderator section:** search players over all servers, then kick, ban (needs the Steam ID, shown in the list), private message, and per server say and announce. Kick and ban ask for a second click. Everything goes through the bridge's console actions and shows up there as an audit event ("dashboard"); `quit`/`stop`/`restart` stay refused by the bridge. Teleport buttons appear only when `DASHBOARD_TP_COMMAND` is set to the server's console line (e.g. `tp {player} {target}`, the target being typed in the text box); the exact command is not known to the repository yet, so check `help` on the test server first. `DASHBOARD_MOD=false` makes the whole page read-only.

```sh
cd stack
DASHBOARD_PASSWORD=... BRIDGE_TOKEN=... docker compose -p tools -f docker-compose.yml -f dashboard.yml up -d dashboard
```

- Listens on `127.0.0.1:8790` in plain HTTP. Put the reverse proxy for your subdomain in front: HTTPS, proxying to
  that port, with `X-Forwarded-Proto` and `X-Forwarded-For` set (the session cookie gets `Secure` behind HTTPS).
- Login with `DASHBOARD_PASSWORD` (8+ characters, else it refuses to start). Session cookie is signed, HttpOnly,
  SameSite=Strict; 5 wrong passwords from one address lock it out for 5 minutes.
- The Docker socket is mounted read-only and only GET calls are made. Leave `DASHBOARD_DOCKER` out to run without it.
