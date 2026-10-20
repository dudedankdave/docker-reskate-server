# Dashboard

A password-protected web page for the servers, the rollover, the bridge and the bots
(`RESKATE_MODE=dashboard`, [`modules/dashboard.py`](../modules/dashboard.py), standard library only).

**Stage 1 (this one) is read-only:** server cards (players, map, version, slots A/B), container state, the live
log from the bridge's events, and the last lines of each tool's log. Nothing can be started, stopped or typed.
Each server card says whether the project rule would allow a restart (at most `DASHBOARD_CUTOFF` players, default 3)
or only a rollover copy; an unknown player count counts as busy. Later stages that restart anything reuse that
check on the server side.

```sh
cd stack
DASHBOARD_PASSWORD=... BRIDGE_TOKEN=... docker compose -p tools -f docker-compose.yml -f dashboard.yml up -d dashboard
```

- Listens on `127.0.0.1:8790` in plain HTTP. Put the reverse proxy for your subdomain in front: HTTPS, proxying to
  that port, with `X-Forwarded-Proto` and `X-Forwarded-For` set (the session cookie gets `Secure` behind HTTPS).
- Login with `DASHBOARD_PASSWORD` (8+ characters, else it refuses to start). Session cookie is signed, HttpOnly,
  SameSite=Strict; 5 wrong passwords from one address lock it out for 5 minutes.
- The Docker socket is mounted read-only and only GET calls are made. Leave `DASHBOARD_DOCKER` out to run without it.
