# Dashboard

A password-protected web page for the servers, the rollover, the bridge and the bots
(`RESKATE_MODE=dashboard`, [`modules/dashboard.py`](../modules/dashboard.py), standard library only).

**Stage 1 (this one) starts and stops nothing:** server cards (players, map, version, slots A/B), container state, the live
log from the bridge's events, the last lines of each tool's log, and a chat section with one tab per server. Typing in the chat box sends a `say` through the bridge (audit event "dashboard"); `DASHBOARD_SAY=false` makes the chat read-only.
Each server card says whether the project rule would allow a restart (at most `DASHBOARD_CUTOFF` players, default 3)
or only a rollover copy; an unknown player count counts as busy. Later stages that restart anything reuse that
check on the server side.

**Pages:** tabs for Übersicht, Chat, Moderator, Befehle & Maps and Logs (the address carries the page, e.g. `#moderator`), every section can be folded (remembered in the browser), the player list sorts by name, team or server (click a column header), and the page is dark (#1a1a1a).

**Server details:** a click on a server card opens its details across the card width: server name, join code (copy button), version, port, health, start time and every setting from the container's environment (tokens, webhooks and passwords left out). A server with a join password shows a lock; a click on it reveals the password for 15 seconds (an authenticated request that is logged). The data comes from the Docker socket, not from the bridge.

**Maps suchen:** the tab "Maps suchen" searches the Thunderstore community `reskate` (the dashboard host needs outbound HTTPS to thunderstore.io; the list is cached for 15 minutes and the pictures are proxied through the dashboard). "Auf Server" writes `MODS+=<package>` to the settings file of the chosen server (or all) and sends `mods add <package>` to the running servers; "In den Pool" does the same and then adds the map's `displayName` (read from the shared store once it is downloaded, else the package name) with `map-pool add` and `MAP_POOL+=`. Nothing is restarted.

**Map pictures:** each server card shows its map's picture. Custom maps take it from the shared map store (`reskate-mods` mounted read-only at `/mods`: the picture a level entry points at, else the package's `icon.png`). Built-in maps (San Vansterdam, Isle of Grom, ...) have no picture in the repository: drop `<map name>.png` (or .jpg/.webp) into `/shared/mapimages/` to give any map one; that wins over the package's. Without a picture the card just has none.

**Server commands:** a catalog of every console command of the ReSkate server (checked against the server's own `README.txt`: map, map-pool, rotation, votes and polls, parties, anti-cheat, reserved slots, announcements, `tpall`/`tphere` ...), a free command line, and shortcuts for switching the map, the map pool and the rotation. The reply is the last lines of the server's log, shown after a moment. `quit`, `stop`, `exit`, `update` and `restart` are refused here and in the bridge.

**New map:** "Neue Map hinzufügen" takes a Thunderstore package (`Owner-Name`) and optional pool names and appends `MODS+=` / `MAP_POOL+=` to `/shared/settings/server-<n>.env` (or `common.env` for all servers). The tools' rollover restarts the servers concerned once they are empty (a busy server is never taken down for it); afterwards switch to the map or add it to the pool. This needs `/shared` mounted writable (see `dashboard.yml`). A pool change typed on the console is not kept across a restart when `MAP_POOL` is set in the env or settings files.

**Moderator section:** search players over all servers, then kick, ban (needs the Steam ID, shown in the list), private message, and per server say and announce. Kick and ban ask for a second click. Everything goes through the bridge's console actions and shows up there as an audit event ("dashboard"); `quit`/`stop`/`restart` stay refused by the bridge. Teleport buttons appear only when `DASHBOARD_TP_COMMAND` is set to the server's console line (e.g. `tp {player} {target}`, the target being typed in the text box); the exact command is not known to the repository yet, so check `help` on the test server first. `DASHBOARD_MOD=false` makes the whole page read-only.

```sh
cd stack
DASHBOARD_PASSWORD=... BRIDGE_TOKEN=... docker compose -p tools -f docker-compose.yml -f dashboard.yml up -d dashboard
```

- Listens on `127.0.0.1:8790` in plain HTTP. Put the reverse proxy for your subdomain in front: HTTPS, proxying to
  that port, with `X-Forwarded-Proto` and `X-Forwarded-For` set (the session cookie gets `Secure` behind HTTPS).
- The root (`/`, German on `/de`) is the public, read-only status page without login (old `/status` links redirect there). The admin dashboard lives under `/admin`; everything else is a 404.
- Link previews (Discord, chat apps) use `DASHBOARD_PUBLIC_URL` (or `public_url` of the branding, default none) as the absolute base for their OpenGraph tags and the preview picture `/status/og.jpg` (left out without an `og.jpg` in the branding directory).
- **Branding:** the status page is neutral ("ReSkate servers") until a branding directory is mounted at `/config/branding` (`DASHBOARD_BRANDING_DIR`). It holds `branding.json` (`name`, `public_url`, `discord_invite`, `discord_widget_id`, and `text.en` / `text.de` to replace any wording, e.g. `title`, `og_title`, `scat1`, `copyright`), `logo.svg`, `favicon.png`, `og.jpg` (1200x630) and a `stickers/` folder whose `<name>.webp` replaces the built-in sticker of that name. Every part is optional. `DASHBOARD_DISCORD` still overrides `discord_invite`.
- Login with `DASHBOARD_PASSWORD` (8+ characters, else it refuses to start). Session cookie is signed, HttpOnly,
  SameSite=Strict and only sent to `/admin`; 5 wrong passwords from one address lock it out for 5 minutes.
- Docker goes through the `dockerproxy` service (`RESKATE_MODE=dockerproxy`, [`modules/dockerproxy.py`](../modules/dockerproxy.py)), never the real socket: a Docker socket is root on the host whether it is mounted `:ro` or not. The proxy passes on the dashboard's reads, restarts of game servers and its file writes into a game server's `/data` and `/data/Mods`, refuses everything else, and hides secret env values of the tool containers. The dashboard then runs as uid 1000. Leave `DASHBOARD_DOCKER` out to run without Docker.
