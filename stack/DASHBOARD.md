# Dashboard

A password-protected web page for the servers, the rollover, the bridge and the bots
(`RESKATE_MODE=dashboard`, [`modules/dashboard.py`](../modules/dashboard.py), standard library only).

**Stage 1 (this one) starts and stops nothing:** server cards (players, map, version, slots A/B), container state, the live
log from the bridge's events, the last lines of each tool's log, and a chat section with one tab per server. Typing in the chat box sends a `say` through the bridge (audit event "dashboard"); `DASHBOARD_SAY=false` makes the chat read-only.
Each server card says whether the project rule would allow a restart (at most `DASHBOARD_CUTOFF` players, default 3)
or only a rollover copy; an unknown player count counts as busy. Later stages that restart anything reuse that
check on the server side.

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
