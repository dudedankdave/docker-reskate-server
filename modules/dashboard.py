"""The dashboard (RESKATE_MODE=dashboard): a small password-protected web page to watch the servers, the
rollover, the bridge and the bots. Stage 1 starts and stops nothing; the only write is a chat line (see Chat).

* Servers, slots, players and map come from the bridge (GET /v1/servers), the live log from its events
  (GET /v1/events). Per server the page says whether a restart would be allowed (at most DASHBOARD_CUTOFF
  players, default 3) or only a rollover copy; an unknown player count counts as busy.
* Containers (rollover, bridge, bot ... and the game servers) and the last log lines of the tools come from
  the Docker socket when DASHBOARD_DOCKER points at it (mount it read-only; only GETs are made). Without it
  that part of the page just says so.
* Login: DASHBOARD_PASSWORD (required, the dashboard refuses to start without it). Successful logins get a
  signed, HttpOnly, SameSite=Strict session cookie (Secure behind HTTPS, i.e. when the reverse proxy sends
  X-Forwarded-Proto: https); sessions end after DASHBOARD_SESSION_HOURS (default 12) or a restart. Five wrong
  passwords from one address lock it out for five minutes.
* Chat: one tab per server with the chat from the bridge (scope chat). Typing in the box sends a `say` through the
  bridge's console action (the only thing the dashboard writes); DASHBOARD_SAY=false makes the chat read-only.
  A send needs the session cookie plus the X-Requested-With header the page sets, and is logged by the bridge
  as an audit event ("dashboard").
* Moderator: search players over all servers (bridge GET /v1/servers/<n>/players), then kick, ban, private message,
  teleport (only when DASHBOARD_TP_COMMAND is set), and per server say, announce, poll. Everything goes through
  the bridge's console actions, so the bridge logs each one as an audit event ("dashboard"); restarts and stops
  stay refused there. DASHBOARD_MOD=false turns the whole section (and the chat box) read-only.
* Server commands: a catalog of every console command of the ReSkate server (map, map-pool, rotation, votes, polls,
  parties, anti-cheat, reserved slots, announcements ...) with a free command line, the last log lines as the reply,
  and map shortcuts (switch map, pool add/remove/clear, rotation). Adding a new map package writes `MODS+=` (and
  `MAP_POOL+=`) into /shared/settings/server-<n>.env or common.env (DASHBOARD_SHARED, default /shared, writable); the
  rollover then restarts the servers concerned once they are empty, so no busy server goes down because of it.
  `quit`, `stop`, `exit`, `update` and `restart` are refused (here and in the bridge).
* Map pictures: each server card shows the map's preview. Custom maps come from the shared map store
  (DASHBOARD_MODS, default /mods, read-only): a map's package folder in /mods/packages with a reskate-levels.json
  naming it gives its picture (an image the level entry points at, else the package's icon.png). Any map, built-in
  ones included, can also be given a picture by dropping <map name>.png/.jpg/.webp into /shared/mapimages/ (this
  one wins). No picture: the card simply has none.
* It listens on DASHBOARD_BIND:DASHBOARD_PORT (127.0.0.1:8790) in plain HTTP; put a reverse proxy with
  HTTPS in front of it for the subdomain.

Settings: DASHBOARD_PASSWORD, BRIDGE_URL, BRIDGE_TOKEN, DASHBOARD_BIND, DASHBOARD_PORT, DASHBOARD_CUTOFF,
DASHBOARD_DOCKER (socket path, default none), DASHBOARD_SESSION_HOURS, DASHBOARD_SAY (default true), DASHBOARD_MOD (default true), DASHBOARD_TP_COMMAND
(console line for a teleport, e.g. "tp {player} {target}"; unset = no teleport buttons). Standard library only."""
import glob
import gzip
import hashlib
import re
import hmac
import io
import http.client
import json
import os
import secrets
import socket
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
BRIDGE_URL = os.environ.get("BRIDGE_URL", "http://127.0.0.1:8780").rstrip("/")
BRIDGE_TOKEN = os.environ.get("BRIDGE_TOKEN", "").strip()
BIND = os.environ.get("DASHBOARD_BIND", "127.0.0.1")
PORT = int(os.environ.get("DASHBOARD_PORT", "8790"))
CUTOFF = int(os.environ.get("DASHBOARD_CUTOFF", "3"))
DOCKER = os.environ.get("DASHBOARD_DOCKER", "").strip()
SAY = os.environ.get("DASHBOARD_SAY", "true").strip().lower() not in ("false", "0", "off", "no")
MOD = os.environ.get("DASHBOARD_MOD", "true").strip().lower() not in ("false", "0", "off", "no")
SHARED = os.environ.get("DASHBOARD_SHARED", "/shared").rstrip("/")
BLOCKED = {"quit", "exit", "stop", "update", "restart", "shutdown"}
PACKAGE = re.compile(r"^[A-Za-z0-9_]+-[A-Za-z0-9_]+(?:-\d+\.\d+\.\d+)?$")
MAPNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _'.-]{0,63}$")
MODS_STORE = os.environ.get("DASHBOARD_MODS", "/mods").rstrip("/")
IMAGES = {}          # lower-case map name -> file, rebuilt every minute
IMAGES_AT = [0.0]
TP = os.environ.get("DASHBOARD_TP_COMMAND", "").strip()
SESSION = int(float(os.environ.get("DASHBOARD_SESSION_HOURS", "12")) * 3600)
KEY = secrets.token_bytes(32)
COOKIE = "reskate_dash"
TOOLS = ("rollover", "bridge", "bot", "bot-chat", "bot-admin", "leaderboard", "webhooks", "settings",
         "announcer", "leaderboard-backup", "dashboard")
fails = {}           # address -> [count, locked until]


def say(text):
    print(text, flush=True)


def restart_rule(players):
    """What the project rule allows for a server with this many players (None = unknown = busy)."""
    if players is not None and players <= CUTOFF:
        return "restart"
    return "rollover"


def sign(expires):
    return f"{expires}.{hmac.new(KEY, str(expires).encode(), hashlib.sha256).hexdigest()}"


def valid(token):
    try:
        expires, mac = token.split(".", 1)
        return hmac.compare_digest(sign(int(expires)), f"{expires}.{mac}") and int(expires) > time.time()
    except ValueError:
        return False


def bridge(path, body=None):
    """GET on the bridge, or a POST of `body` (a dict) when given."""
    headers = {"Authorization": f"Bearer {BRIDGE_TOKEN}"}
    data = None
    if body is not None:
        data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
    req = urllib.request.Request(BRIDGE_URL + path, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def line(value, limit):
    value = " ".join(str(value or "").split())[:limit]
    if not value:
        raise ValueError("a text is missing")
    return value


def moderate(body):
    """One moderator action through the bridge: {server, slot?, action, player?, steam_id?, text?, target?}."""
    server, action = str(body.get("server", "")), str(body.get("action", ""))
    if not server.isalnum():
        raise ValueError("a server is needed")
    slot = str(body.get("slot") or "")[:2] or None
    if action in ("say", "announce"):
        todo = {"action": action, "args": {"text": line(body.get("text"), 200)}}
    elif action == "msg":
        todo = {"action": "msg", "args": {"player": line(body.get("player"), 64), "text": line(body.get("text"), 190)}}
    elif action == "kick":
        todo = {"action": "kick", "args": {"player": line(body.get("player"), 64)}}
    elif action == "ban":
        steam_id = str(body.get("steam_id", ""))
        if not (steam_id.isdigit() and len(steam_id) == 17):
            raise ValueError("a ban needs the 17-digit Steam ID")
        reason = " ".join(str(body.get("text") or "").split())[:100]
        todo = {"action": "console", "args": {"command": f"ban {steam_id} {reason}".strip()}}
    elif action == "poll":
        todo = {"action": "poll", "args": {"question": line(body.get("text"), 120), "answers": line(body.get("answers"), 280)}}
    elif action == "console":
        command = line(body.get("text"), 300)
        if command.split(None, 1)[0].lower().lstrip("/") in BLOCKED:
            raise ValueError("that command stops or restarts the server; restarts are the rollover's job")
        todo = {"action": "console", "args": {"command": command}}
    elif action == "log":
        todo = {"action": "log", "args": {"count": str(min(max(int(body.get("count") or 12), 1), 50))}}
    elif action == "tp":
        if not TP:
            raise ValueError("teleport is not set up (DASHBOARD_TP_COMMAND)")
        player, target = line(body.get("player"), 64), line(body.get("target"), 64)
        todo = {"action": "console", "args": {"command": TP.format(player=player, target=target)}}
    else:
        raise ValueError(f"unknown action {action!r}")
    if slot:
        todo["slot"] = slot
    todo["by"] = "dashboard"
    return bridge(f"/v1/servers/{server}/do", todo)


def add_map(body):
    """Write a new map package (and pool names) into the settings files; the rollover restarts the servers
    concerned once they are empty. {server: "<n>" or "*", package: "Owner-Name[-1.2.3]", pool: "Map name, Other"}."""
    server, package = str(body.get("server", "")), str(body.get("package", "")).strip()
    if not (server == "*" or server.isalnum()):
        raise ValueError("a server (or all) is needed")
    if not PACKAGE.match(package):
        raise ValueError("the package looks like Owner-Name or Owner-Name-1.2.3")
    pool = [p.strip() for p in str(body.get("pool", "")).split(",") if p.strip()]
    for name in pool:
        if not MAPNAME.match(name):
            raise ValueError(f"map name {name!r} has characters the pool does not take")
    folder = f"{SHARED}/settings"
    if not os.path.isdir(folder):
        raise ValueError(f"{folder} is not mounted into the dashboard (DASHBOARD_SHARED)")
    path = f"{folder}/{'common' if server == '*' else 'server-' + server}.env"
    old = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
    have = {x.strip() for x in old.splitlines()}
    new = [f"MODS+={package}"] + [f'MAP_POOL+="{n}"' if " " in n else f"MAP_POOL+={n}" for n in pool]
    new = [x for x in new if x not in have]
    if not new:
        return "already in the settings"
    text = old + ("" if not old or old.endswith("\n") else "\n") + "# added by the dashboard\n" + "\n".join(new) + "\n"
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        f.write(text)
    st = os.stat(folder)
    try:
        os.chown(path + ".tmp", st.st_uid, st.st_gid)
    except OSError:
        pass
    os.replace(path + ".tmp", path)
    return f"written to {os.path.basename(path)}: " + ", ".join(new)


TS_API = "https://thunderstore.io/c/reskate/api/v1/package/"
TS_IMG_HOSTS = ("gcdn.thunderstore.io", "thunderstore.io")
TS_CACHE = {"at": 0, "rows": []}


def ts_fetch(url, limit):
    req = urllib.request.Request(url, headers={"User-Agent": "reskate-dashboard", "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read(limit + 1)
        if r.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
        return data, r.headers.get("Content-Type", "")


def ts_packages():
    """The community's package list, cached for 15 minutes."""
    if time.time() - TS_CACHE["at"] > 900 or not TS_CACHE["rows"]:
        data, _ = ts_fetch(TS_API, 60_000_000)
        rows = []
        for p in json.loads(data):
            if p.get("is_deprecated") or not p.get("versions"):
                continue
            v = p["versions"][0]
            rows.append({"package": f"{p['owner']}-{p['name']}", "owner": p["owner"], "name": p["name"].replace("_", " "),
                         "version": v.get("version_number", ""), "description": v.get("description", ""),
                         "icon": v.get("icon", ""), "downloads": sum(x.get("downloads", 0) for x in p["versions"]),
                         "rating": p.get("rating_score", 0), "updated": (p.get("date_updated") or "")[:10],
                         "url": p.get("package_url", ""), "categories": p.get("categories", [])})
        TS_CACHE.update(at=time.time(), rows=rows)
    return TS_CACHE["rows"]


def warm_ts():
    try:
        started = time.time()
        say(f"dashboard: Thunderstore list loaded, {len(ts_packages())} packages in {time.time() - started:.1f} s")
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as e:
        say(f"dashboard: Thunderstore list not loaded: {type(e).__name__}: {e}")


TS_SORTS = {"downloads": lambda r: r["downloads"], "updated": lambda r: r["updated"], "rating": lambda r: r["rating"],
            "name": lambda r: r["name"].lower()}


def ts_search(query, maps_only=True, sort="downloads", asc=False):
    terms = [t for t in str(query).lower().split() if t][:6]
    out = []
    for r in ts_packages():
        text = f"{r['package']} {r['name']} {r['description']} {' '.join(r['categories'])}".lower()
        cats = " ".join(r["categories"]).lower()
        if maps_only and "map" not in cats and "level" not in cats:
            continue
        if all(t in text for t in terms):
            out.append(r)
    out.sort(key=lambda r: r["package"])
    out.sort(key=TS_SORTS.get(sort, TS_SORTS["downloads"]), reverse=not asc)
    return {"total": len(out), "results": out[:100]}


def ts_image(url):
    u = urlparse(url)
    if u.scheme != "https" or u.hostname not in TS_IMG_HOSTS or u.port:
        raise ValueError("picture host not allowed")
    data, kind = ts_fetch(url, 2_000_000)
    if len(data) > 2_000_000 or not kind.startswith("image/") or "svg" in kind:
        raise ValueError("not a plain picture")
    return kind, data


def store_names(package):
    """displayName(s) of a map package that is already in the shared store (all versions, newest first)."""
    if not PACKAGE.match(package):
        raise ValueError("bad package")
    names = []
    for pack in sorted(glob.glob(f"{MODS_STORE}/packages/{package}-*/"), reverse=True):
        try:
            with open(f"{pack}reskate-levels.json", encoding="utf-8-sig") as f:
                for level in json.load(f).get("levels", []):
                    if isinstance(level, dict) and level.get("displayName") and level["displayName"] not in names:
                        names.append(level["displayName"])
        except (OSError, ValueError, AttributeError):
            continue
        if names:
            break
    return names



IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


def inside(path, base):
    path, base = os.path.realpath(path), os.path.realpath(base)
    return path == base or path.startswith(base + os.sep)


def scan_images():
    """Map name (lower case) -> picture file: the store's packages first, then /shared/mapimages overrides."""
    found = {}
    for pack in sorted(glob.glob(f"{MODS_STORE}/packages/*/")):
        try:
            with open(f"{pack}reskate-levels.json", encoding="utf-8-sig") as f:
                levels = json.load(f).get("levels", [])
        except (OSError, ValueError, AttributeError):
            continue
        for level in levels:
            name = level.get("displayName") if isinstance(level, dict) else None
            if not name:
                continue
            picks = [v for v in level.values() if isinstance(v, str) and os.path.splitext(v)[1].lower() in IMAGE_TYPES]
            for rel in picks + ["icon.png"]:
                path = os.path.join(pack, rel)
                if inside(path, pack) and os.path.isfile(path):
                    found[name.lower()] = path
                    break
    for path in glob.glob(f"{SHARED}/mapimages/*"):
        base, ext = os.path.splitext(os.path.basename(path))
        if ext.lower() in IMAGE_TYPES and os.path.isfile(path):
            found[base.lower()] = path
    return found


def map_image(name):
    """(content type, bytes) of a map's picture, or None."""
    if time.time() - IMAGES_AT[0] > 60:
        IMAGES.clear()
        IMAGES.update(scan_images())
        IMAGES_AT[0] = time.time()
    path = IMAGES.get(str(name).strip().rstrip(".").strip().lower())
    if not path or os.path.getsize(path) > 2_000_000:
        return None
    with open(path, "rb") as f:
        return IMAGE_TYPES[os.path.splitext(path)[1].lower()], f.read()


def players():
    """Everybody who is on, from every running server: [{name, steam_id, team, server, slot, role}]."""
    out = []
    for r in bridge("/v1/servers"):
        if not r.get("running"):
            continue
        try:
            for p in bridge(f"/v1/servers/{r['server']}/players"):
                out.append(dict(p, server=r["server"], role=r.get("role", "")))
        except (urllib.error.URLError, OSError, ValueError):
            pass
    return out


class Unix(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("docker", timeout=10)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.sock.connect(self.path)


def docker_get(path):
    """GET on the Docker socket (read-only: the dashboard never sends anything else)."""
    conn = Unix(DOCKER)
    try:
        conn.request("GET", "/v1.41" + path)
        resp = conn.getresponse()
        data = resp.read()
        if resp.status != 200:
            raise RuntimeError(f"docker {resp.status}")
        return data
    finally:
        conn.close()


def demux(data):
    """Text of a Docker log stream (8-byte frame headers unless the container has a TTY)."""
    out, i = [], 0
    while i + 8 <= len(data) and data[i] in (0, 1, 2) and data[i + 1:i + 4] == b"\0\0\0":
        size = int.from_bytes(data[i + 4:i + 8], "big")
        out.append(data[i + 8:i + 8 + size])
        i += 8 + size
    return b"".join(out).decode("utf-8", "replace") if out else data.decode("utf-8", "replace")


def disk():
    """Free space of the disk the shared store lives on (the host disk): {total, free} in bytes, or None."""
    try:
        v = os.statvfs(MODS_STORE)
    except OSError:
        return None
    return {"total": v.f_blocks * v.f_frsize, "free": v.f_bavail * v.f_frsize}


def containers():
    if not DOCKER:
        return {"docker": False, "containers": [], "disk": disk()}
    rows = []
    for c in json.loads(docker_get("/containers/json?all=1")):
        name = c["Names"][0].lstrip("/")
        if not name.startswith("reskate"):
            continue
        rows.append({"name": name, "image": c["Image"], "state": c["State"], "status": c["Status"],
                     "tool": name.removeprefix("reskate-") in TOOLS})
    return {"docker": True, "containers": sorted(rows, key=lambda r: (not r["tool"], r["name"])), "disk": disk()}


def tool_log(name, lines=60):
    if not DOCKER or not name.removeprefix("reskate-") in TOOLS:
        raise ValueError("unknown tool")
    data = docker_get(f"/containers/{name}/logs?stdout=1&stderr=1&tail={max(1, min(lines, 300))}")
    return {"name": name, "log": demux(data)}


HIDE = ("TOKEN", "WEBHOOK", "SECRET", "KEY", "PASSWORD")
SKIP = {"PATH", "HOME", "LD_LIBRARY_PATH", "SteamAppId", "COMMANDS", "RESKATE_SERVER", "HOSTNAME"}
DETAILS = {"at": 0, "rows": {}}
CODES = {}           # container id -> (read at, join code)


def join_code(cid):
    """The server's join code: the last `Join code:` line in the log (cached for a minute)."""
    got = CODES.get(cid)
    if got and time.time() - got[0] < 60:
        return got[1]
    code = None
    try:
        for line in reversed(demux(docker_get(f"/containers/{cid}/logs?stdout=1&stderr=1&tail=6000")).splitlines()):
            if "Join code: " in line:
                code = line.split("Join code: ", 1)[1].strip().split()[0]
                break
    except (RuntimeError, OSError):
        pass
    CODES[cid] = (time.time(), code)
    return code


def server_details():
    """Server number -> what its running containers say (name, password set, settings); cached for 20 s."""
    if not DOCKER:
        return {}
    if time.time() - DETAILS["at"] < 20:
        return DETAILS["rows"]
    rows = {}
    for c in json.loads(docker_get("/containers/json")):
        name = c["Names"][0].lstrip("/")
        if not name.startswith("reskate-server"):
            continue
        info = json.loads(docker_get(f"/containers/{c['Id']}/json"))
        env = dict(e.partition("=")[::2] for e in info["Config"].get("Env") or [])
        number = (info["Config"].get("Labels") or {}).get("reskate.server") or env.get("RESKATE_SERVER")
        if not number:
            continue
        slot = (info["Config"].get("Labels") or {}).get("reskate.slot", "")
        row = rows.setdefault(number, {"containers": [], "env": {}, "password": "", "name": ""})
        row["containers"].append({"name": name, "slot": slot, "image": info["Config"].get("Image", ""),
                                  "version": env.get("RESKATE_IMAGE_VERSION", ""), "revision": env.get("RESKATE_IMAGE_REVISION", ""),
                                  "started": info["State"].get("StartedAt", ""), "port": env.get("PORT", ""),
                                  "health": (info["State"].get("Health") or {}).get("Status", ""), "id": c["Id"]})
        row["name"] = env.get("SERVER_NAME", row["name"])
        word = env.get("SERVER_PASSWORD", "")
        row["password"] = word if word.lower() not in ("", "off") else row["password"]
        for k, v in env.items():
            if k not in SKIP and not any(h in k for h in HIDE):
                row["env"][k] = v[:200]
    DETAILS.update(at=time.time(), rows=rows)
    return rows


MAPS = {"at": 0, "rows": {}}


def tar_of(cid, path):
    return tarfile.open(fileobj=io.BytesIO(docker_get(f"/containers/{cid}/archive?path={path}")))


BUILTIN_MAPS = ["San Vansterdam", "Isle of Grom", "Super Ultra Mega Resort", "Stadium 1"]


def package_maps(mods, pool=()):
    """Built-in maps plus every map the linked packages declare (read from the shared store), plus the pool's own names."""
    names = set()
    for m in mods:
        if not re.fullmatch(r"[\w.-]+", m):
            continue
        try:
            with open(f"{MODS_STORE}/packages/{m}/reskate-levels.json", encoding="utf-8-sig") as f:
                for level in json.load(f).get("levels", []):
                    if isinstance(level, dict) and level.get("displayName"):
                        names.add(level["displayName"])
        except (OSError, ValueError, AttributeError):
            continue
    names.update(pool)
    return BUILTIN_MAPS + sorted(names - set(BUILTIN_MAPS), key=str.lower)


def live_maps(cid):
    """(current map, pool, installed packages) as the running server has them now: its saved settings file
    and the links in its Mods folder, not the environment it was started with."""
    with tar_of(cid, "/data/ReSkateServer.json") as t:
        member = t.getmember("ReSkateServer.json")
        maps = json.load(t.extractfile(member)).get("maps", {})
    pool = [x for x in maps.get("pool", []) if isinstance(x, str)]
    try:
        with tar_of(cid, "/data/Mods") as t:
            mods = sorted(m.linkname.rsplit("/", 1)[-1] for m in t.getmembers() if m.issym())
    except (RuntimeError, OSError, tarfile.TarError):
        mods = []
    return str(maps.get("map") or ""), pool, mods


def server_maps():
    """Server number -> {map, pool, mods, live}; live=False falls back to the container's start-up environment."""
    if time.time() - MAPS["at"] < 20:
        return MAPS["rows"]
    out = {}
    for number, row in server_details().items():
        env = row["env"]
        split = lambda v: [x.strip() for x in v.split(",") if x.strip()]  # noqa: E731
        item = {"map": env.get("MAP", ""), "pool": split(env.get("MAP_POOL", "")), "mods": split(env.get("MODS", "")), "live": False}
        for c in row["containers"]:
            try:
                item["map"], item["pool"], item["mods"] = live_maps(c["id"])
                item["live"] = True
                break
            except (RuntimeError, OSError, ValueError, KeyError, tarfile.TarError):
                continue
        item["available"] = package_maps(item["mods"], item["pool"])
        out[number] = item
    MAPS.update(at=time.time(), rows=out)
    return out


def server_info(number):
    row = server_details().get(str(number))
    if not row:
        raise ValueError("no running container for that server")
    containers = [dict(c, code=join_code(c["id"])) for c in row["containers"]]
    for c in containers:
        c.pop("id")
    return {"server": str(number), "name": row["name"], "locked": bool(row["password"]), "containers": containers,
            "env": dict(sorted(row["env"].items()))}


def server_secret(number):
    row = server_details().get(str(number))
    if not row:
        raise ValueError("no running container for that server")
    return {"password": row["password"]}


def servers():
    rows = bridge("/v1/servers")
    for r in rows:
        r["rule"] = restart_rule(r.get("players")) if r.get("running") else "empty"
        for s in r.get("slots", {}).values():
            s["rule"] = restart_rule(s.get("players")) if s.get("running") else "empty"
    try:
        known = server_details()
    except (RuntimeError, OSError, ValueError):
        known = {}
    for r in rows:
        r["locked"] = bool(known.get(str(r["server"]), {}).get("password"))
    return {"cutoff": CUTOFF, "say": SAY and MOD, "mod": MOD, "tp": bool(TP), "servers": rows}


PAGE = """<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ReSkate Dashboard</title><style>
:root{color-scheme:dark;--bg:#1a1a1a;--fg:#e8e8e8;--card:#232323;--line:#333;--mut:#9a9a9a;--ok:#4cc38a;--warn:#f0a53c;--bad:#f97066;--acc:#8ea2ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}
header{display:flex;gap:8px 12px;flex-wrap:wrap;align-items:center;padding:12px max(16px,env(safe-area-inset-right,0px),calc((100% - 1100px)/2 + 16px)) 12px max(16px,env(safe-area-inset-left,0px),calc((100% - 1100px)/2 + 16px));border-bottom:1px solid var(--line)}header h1{font-size:17px;margin:0;flex:1 1 auto}
main{padding:20px max(16px,env(safe-area-inset-right,0px)) 32px max(16px,env(safe-area-inset-left,0px));max-width:1100px;margin:auto;display:grid;grid-template-columns:minmax(0,1fr);gap:28px}section h2{display:flex;flex-wrap:wrap;align-items:center;gap:8px;font-size:13px;font-weight:600;color:var(--mut);text-transform:uppercase;letter-spacing:.05em;margin:0 0 10px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:10px}
.card{min-width:0;overflow-wrap:anywhere;background:var(--card);border:1px solid var(--line);border-radius:6px;padding:12px}.cgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:10px;margin:10px 0}.cgrid h3{margin:0 0 4px;font-size:14px}.crow{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:8px}.crow select{flex:1;min-width:0}button.arm{background:var(--bad);color:#111}details.adv{margin:8px 0}details.adv summary{cursor:pointer;color:var(--mut);padding:4px 0}.chips{display:flex;flex-wrap:wrap;gap:4px;margin:4px 0 8px}.chip{background:var(--line);border-radius:4px;padding:2px 8px;font-size:12px}.chip.on{background:var(--acc);color:#111}.chip.mod{font-family:ui-monospace,monospace;font-size:11px}.card.open{display:grid;grid-template-columns:230px minmax(0,1fr);column-gap:16px;align-items:start;grid-auto-rows:min-content}.card.open>.head{grid-column:1/-1}.card.open>:not(.head):not(.detail){grid-column:1}.card.open>.detail{grid-column:2;grid-row:2/12;margin-top:0;border-top:0;padding-top:0}.card.open .pic{margin-bottom:6px}.dgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:14px}.dblock h4{margin:0 0 6px;font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut)}.dblock .kv{margin:0}@media(max-width:700px){.card.open{display:block}.card.open>.detail{margin-top:10px;border-top:1px solid var(--line);padding-top:10px}}.card.open{grid-column:1/-1}.card.srv{cursor:pointer}.detail{margin-top:10px;border-top:1px solid var(--line);padding-top:10px;cursor:default}.detail .set{max-height:320px;overflow:auto;font-size:12px;margin-top:6px}.lock{margin-left:auto;font-size:13px;padding:2px 8px}.head .lock+.pill{margin-left:0}.pic{position:relative;width:100%;aspect-ratio:16/9;border-radius:4px;margin-bottom:8px;background:repeating-linear-gradient(135deg,var(--line) 0 8px,var(--card) 8px 16px);display:flex;align-items:center;justify-content:center;overflow:hidden;color:var(--mut);font-size:13px;text-align:center;padding:6px}.mapimg{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}.kv{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;margin:8px 0 0;font-size:13px}.kv dt{color:var(--mut)}.pbar{grid-column:1/-1;box-sizing:border-box;width:100%;height:6px;border-radius:3px;background:var(--line);overflow:hidden}.kv dd{font-variant-numeric:tabular-nums}.pbar span{display:block;height:100%;border-radius:3px;background:var(--ok)}.pbar .warn{background:var(--warn)}.pbar .bad{background:var(--bad)}.pbar.full{overflow:visible;animation:shake .5s ease-in-out infinite}.pbar.full span{box-shadow:0 0 6px var(--bad)}@keyframes shake{0%,100%{transform:translateX(0)}20%{transform:translateX(-2px)}40%{transform:translateX(2px)}60%{transform:translateX(-2px)}80%{transform:translateX(2px)}}@media(prefers-reduced-motion:reduce){.pbar.full{animation:none}}.kv dd{margin:0;min-width:0;overflow-wrap:anywhere}.num{background:var(--line);border-radius:4px;min-width:26px;height:26px;padding:0 6px;display:inline-flex;align-items:center;justify-content:center;font-weight:700;font-size:15px}.head h3{flex:1}.card h3{margin:0;font-size:15px;min-width:0;overflow-wrap:anywhere}.head{display:flex;justify-content:space-between;align-items:flex-start;gap:8px;margin-bottom:6px}.pill{display:inline-block;max-width:100%;margin-bottom:6px;box-sizing:border-box}
.tsrow{display:grid;grid-template-columns:64px minmax(0,1fr) 110px auto;gap:12px;align-items:center;background:var(--card);border:1px solid var(--line);border-radius:6px;padding:8px 12px;margin-bottom:6px}.tspic{position:relative;width:64px;height:48px;border-radius:4px;background:var(--line);display:flex;align-items:center;justify-content:center;overflow:hidden;color:var(--mut)}.tsmain{min-width:0;overflow-wrap:anywhere}.tsmain .mut{font-size:12px}.tsname a{font-weight:600}.tsstat{text-align:right;font-variant-numeric:tabular-nums;font-size:13px}.tsrow .act{display:flex;gap:6px}@media(max-width:700px){.tsrow{grid-template-columns:64px minmax(0,1fr)}.tsstat{text-align:left;grid-column:2}.tsrow .act{grid-column:1/-1}}
.mut{color:var(--mut);font-size:13px}.pill{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;border:1px solid currentColor}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
#chat{min-height:120px}pre{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:10px;margin:0;max-height:320px;overflow:auto;font:12px/1.4 ui-monospace,monospace;white-space:pre-wrap;word-break:break-all}
.tablewrap{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:6px}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--line);font-size:14px;vertical-align:middle}th{color:var(--mut);font-weight:500}
td .act{display:flex;gap:6px;flex-wrap:wrap}td button{padding:3px 8px;font-size:13px}button.arm{border-color:var(--bad);color:var(--bad)}
#find,#srvtools{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 8px}#srvtools{margin:8px 0 0}#find input,#srvtools input{flex:1;min-width:140px;font:inherit;padding:7px 10px;border-radius:5px;border:1px solid var(--line);background:var(--card);color:var(--fg)}
nav{display:flex;gap:6px;flex-wrap:wrap;max-width:1100px;margin:0 auto;padding:12px max(16px,env(safe-area-inset-right,0px)) 0 max(16px,env(safe-area-inset-left,0px))}nav button.on{border-color:var(--acc);color:var(--acc);font-weight:600}
.off{display:none!important}section.closed>*:not(h2){display:none}.fold{margin-left:auto;padding:1px 9px}th[data-k]{cursor:pointer;user-select:none;white-space:nowrap}th[data-k]:hover{color:var(--fg)}
.bar{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 8px}.bar input,.bar select{flex:1 1 160px;min-width:0;font:inherit;padding:7px 10px;border-radius:5px;border:1px solid var(--line);background:var(--card);color:var(--fg)}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px}.tabs button.on{border-color:var(--acc);color:var(--acc)}
#sayform{display:flex;gap:8px;margin-top:8px}#sayform input{flex:1;min-width:0;font:inherit;padding:7px 10px;border-radius:5px;border:1px solid var(--line);background:var(--card);color:var(--fg)}
button,select{font:inherit;color:var(--fg);background:var(--card);border:1px solid var(--line);border-radius:5px;padding:5px 10px}button{cursor:pointer}
</style></head><body>
<header><h1>ReSkate Dashboard</h1><span class="mut" id="upd"></span><button onclick="fetch('/logout',{method:'POST'}).then(()=>location.reload())">Abmelden</button></header>
<nav id="nav"></nav>
<main>
<div id="diskwarn" class="card" hidden></div>
<section data-v="uebersicht"><h2>Server</h2><div class="grid" id="servers"></div></section>
<section data-v="uebersicht"><h2>Tools</h2><div class="grid" id="tools"></div></section>
<section data-v="uebersicht"><h2>Server-Container</h2><div class="grid" id="containers"></div></section>
<section data-v="chat"><h2>Chat</h2><div class="tabs" id="tabs"></div><pre id="chat">-</pre>
<form id="sayform" autocomplete="off"><input id="saytext" maxlength="200" placeholder="Nachricht an den Server-Chat"><button>Senden</button></form></section>
<section id="modsec" data-v="moderator"><h2>Moderator</h2>
<form id="find" autocomplete="off"><input id="q" placeholder="Spieler suchen (Name, Steam-ID, Team)"><input id="mtext" maxlength="200" placeholder="Text / Grund (Nachricht, Ban) oder Ziel-Spieler (Teleport)"></form>
<div class="tablewrap"><table id="plist"><thead><tr><th data-k="name">Spieler</th><th data-k="team">Team</th><th data-k="server">Server</th><th>Aktionen</th></tr></thead><tbody></tbody></table></div>
<form id="srvtools" autocomplete="off"><select id="msrv"></select><input id="stext" maxlength="200" placeholder="Text an alle auf diesem Server">
<button type="button" data-a="say">Say</button><button type="button" data-a="announce">Announce</button></form>
<div class="mut" id="mres"></div></section>
<section id="cmdsec" data-v="befehle"><h2>Server-Befehle</h2>
<div class="bar"><select id="csrv"></select><span class="mut" id="cinfo"></span></div>
<div class="cgrid">
<div class="card"><h3>Map wechseln</h3><div class="crow"><select id="cmap"></select><button id="cmapgo">Wechseln</button></div></div>
<div class="card"><h3>Map-Pool</h3><div class="crow"><select id="cpooladd"></select><button id="cpooladdgo">Hinzufügen</button></div>
<div class="crow"><select id="cpoolrm"></select><button id="cpoolrmgo">Entfernen</button></div>
<div class="crow"><button data-c="map-pool">Pool zeigen</button><button data-c="map-pool clear">Alle Maps erlauben</button></div></div>
<div class="card"><h3>Rotation</h3><div class="crow"><select id="crot"><option>10</option><option>15</option><option>20</option><option selected>30</option><option>45</option><option>60</option><option>90</option><option>120</option></select><span class="mut">Minuten</span><button id="crotgo">Setzen</button><button data-c="rotation off">Aus</button></div></div>
<div class="card"><h3>Votes</h3><div class="crow"><button data-c="votes map on">Map-Vote an</button><button data-c="votes map off">aus</button></div>
<div class="crow"><button data-c="votes kick on">Kick-Vote an</button><button data-c="votes kick off">aus</button></div>
<div class="crow"><button data-c="votes tod on">Zeit-Vote an</button><button data-c="votes tod off">aus</button><button data-c="vote-cancel">Vote abbrechen</button></div></div>
<div class="card"><h3>Tageszeit</h3><div class="crow"><select id="ctod"><option value="default">Standard</option><option value="morning">Morgen</option><option value="noon">Mittag</option><option value="afternoon">Nachmittag</option><option value="evening">Abend</option><option value="night">Nacht</option></select><button id="ctodgo">Setzen</button></div></div>
<div class="card"><h3>Info</h3><div class="crow"><button data-c="status">Status</button><button data-c="players">Spieler</button><button data-c="maps">Maps</button><button data-c="net">Netzwerk</button><button data-c="help">Hilfe</button></div></div>
</div>
<details class="adv"><summary>Erweitert: Befehl frei eingeben</summary><div class="bar"><select id="cat"></select><input id="cmd" maxlength="300" list="cmdsug" placeholder="Befehl, z. B. status oder map grom"><datalist id="cmdsug"></datalist><button id="csend">Senden</button></div></details>
<div class="mut">Befehle wirken sofort auf den laufenden Server. Nach einem Neustart gelten wieder die Einstellungen des Servers (siehe "Neue Map hinzufügen" für dauerhafte Änderungen).</div>
<pre id="cout">Antwort des Servers erscheint hier (letzte Log-Zeilen).</pre>
<h2 style="margin-top:14px">Neue Map hinzufügen</h2>
<div class="bar"><input id="pkg" maxlength="80" placeholder="Thunderstore-Paket, z. B. Owner-MapName"><input id="poolnames" maxlength="200" placeholder="Name(n) im Pool (optional, Komma getrennt)"><select id="pscope"></select><label class="mut" style="display:flex;gap:6px;align-items:center"><input type="checkbox" id="live" checked style="flex:none;min-width:0">sofort laden</label><button id="addmap">Hinzufügen</button></div>
<div class="mut">Schreibt MODS/MAP_POOL in die Settings-Datei. "Sofort laden" schickt zusätzlich mods add und map-pool add an die laufenden Server (ohne Neustart, nur mit neuem Image). Die Settings-Datei sorgt dafür, dass es nach einem Neustart bleibt; der Rollover startet Server dafür nur, wenn sie leer sind.</div></section>
<section data-v="suche"><h2>Pool &amp; Maps pro Server</h2><div class="grid" id="poolbox"></div></section>
<section data-v="suche"><h2>Maps auf Thunderstore</h2>
<form class="bar" id="tsform"><input id="tsq" maxlength="60" placeholder="Suche, z. B. plaza, skatepark, Name"><select id="tsrv"></select><select id="tssort" title="Sortierung"><option value="downloads">Meiste Downloads</option><option value="updated">Zuletzt aktualisiert</option><option value="rating">Beste Bewertung</option><option value="name">Name</option></select><select id="tsdir" title="Richtung"><option value="desc">absteigend</option><option value="asc">aufsteigend</option></select><label class="mut" style="display:flex;gap:6px;align-items:center"><input type="checkbox" id="tsall" style="flex:none;min-width:0">alle Pakete</label><button>Suchen</button></form>
<div class="mut" id="tsinfo">Sucht in der Thunderstore-Community reskate. "Auf Server" lädt das Paket, "In den Pool" nimmt die Map zusätzlich in den Map-Pool auf (ohne Neustart, mit neuem Image).</div><div class="tslist" id="tsres"></div></section>
<section data-v="logs"><h2>Live-Log <select id="sel"><option value="">alle Server</option></select> <select id="scope"><option>log</option><option>public</option><option>admin</option><option>chat</option><option>audit</option></select></h2><pre id="events"></pre></section>
<section data-v="logs"><h2>Tool-Log <select id="tool"></select></h2><pre id="toollog">-</pre></section>
</main>
<script>
const $=id=>document.getElementById(id);
const el=(t,c,x)=>{const e=document.createElement(t);if(c)e.className=c;if(x!==undefined)e.textContent=x;return e};
const RULE={restart:["Neustart ok · ≤ 3 Spieler","ok"],rollover:["Nur Rollover","warn"],empty:["aus","mut"]};
async function api(p){const r=await fetch(p);if(r.status==401){location.reload();throw 0}if(!r.ok)throw new Error((await r.json()).error||r.status);return r.json()}
const openCards=new Set(),infoCache={},infoAt={},shown={};
let drawTimer=0;const drawn=()=>{clearTimeout(drawTimer);drawTimer=setTimeout(()=>servers().catch(()=>{}),50)};
const GROUPS=[['Spieler',/^(MAX_PLAYERS|PART|VOICE|NOCLIP|NO_BAIL|BOOSTS|OBJECT|ADMINS|BANS|AFK|KICK_)/],['Votes',/^VOTE_/],['Anti-Cheat',/^(SCORE_|SPEED_|ENFORCE_|BONE_|DISTANCE_)/],
 ['Maps',/^(MAP|PARK_|LAYER|WORLD_LAYER|ROTATION)/],['Ansagen & Chat',/^(ANNOUNCE|WELCOME|DISCORD|CHAT|COMMANDS)/],['Rangliste & Teams',/^(LEADERBOARD|TEAMS|RANKED|UNRANKED)/],
 ['Betrieb',/^(UPDATE_|LISTED|PORT|QUERY_PORT|ACTIVITY|SERVER_NAME|RESKATE_)/]];
function block(title,rows){const b=el('div','dblock');b.append(el('h4',0,title));const dl=el('dl','kv');for(const [k,v] of rows){dl.append(el('dt',0,k));const d=el('dd',0,v);dl.append(d)}b.append(dl);return b}
function detail(s){const p=el('div','detail'),i=infoCache[s.server];
 if(!i){p.textContent='Lade …';return p}
 if(i.error){p.textContent='Keine Details: '+i.error;return p}
 const grid=el('div','dgrid'),gen=el('div','dblock');gen.append(el('h4',0,'Allgemein'));
 const g1=el('dl','kv'),row=(dl,a,b)=>{dl.append(el('dt',0,a));const d=el('dd',0,b);dl.append(d);return d};
 row(g1,'Servername',i.name||'–');
 for(const c of i.containers){const lab=c.slot?' (Slot '+c.slot+')':'',d=row(g1,'Join-Code'+lab,c.code||'unbekannt');
  if(c.code){const b=el('button',0,'Kopieren');b.style.marginLeft='8px';b.onclick=()=>{navigator.clipboard.writeText(c.code).then(()=>{b.textContent='Kopiert'},()=>{b.textContent='Markieren & Strg+C'})};d.append(b)}}
 gen.append(g1);grid.append(gen);
 const ops=[];for(const c of i.containers){const lab=c.slot?' ('+c.slot+')':'';
  ops.push(['Version'+lab,(c.version||'?')+(c.revision?' · '+c.revision:'')],['Port'+lab,c.port||'?'],['Zustand'+lab,c.health||'?'],['Gestartet'+lab,c.started?new Date(c.started).toLocaleString():'?'])}
 grid.append(block('Betrieb',ops));
 const mx=mapsCache[s.server];
 if(mx){const cur=(s.map||mx.map||'').replace(/[.]$/,''),b=el('div','dblock');b.append(el('h4',0,'Maps'),el('div','mut','Aktuell: '+(cur||'?')),
   el('div','mut','Map-Pool ('+(mx.pool.length||'alle')+')'),mx.pool.length?chipRow(mx.pool,cur,false):el('div','mut','Kein Pool gesetzt: alle Maps erlaubt'),
   el('div','mut','Map-Pakete ('+mx.mods.length+')'),mx.mods.length?chipRow(mx.mods,'',true):el('div','mut','Keine Mods'));
  if(!mx.live)b.append(el('div','mut','Stand vom Containerstart (Live-Daten nicht lesbar)'));grid.append(b)}
 const buckets=GROUPS.map(g=>[g[0],g[1],[]]),rest=[];
 for(const [k,v] of Object.entries(i.env)){if(['MODS','MAP_POOL','MAP','SERVER_NAME'].includes(k))continue;const t=buckets.find(x=>x[1].test(k));(t?t[2]:rest).push([k,v||'(leer)'])}
 for(const [t,,rows] of buckets)if(rows.length&&t!='Betrieb')grid.append(block(t,rows));else if(rows.length){const o=grid.querySelector('.dblock:nth-child(2) dl');for(const [k,v] of rows){o.append(el('dt',0,k));o.append(el('dd',0,v))}}
 if(rest.length)grid.append(block('Sonstiges',rest));
 p.append(grid,el('div','mut','Einstellungen aus der Umgebung des Containers (ohne Tokens und Webhooks).'));return p}
const imgState={};
async function servers(){
 const d=await api('/api/servers'),box=$('servers');box.replaceChildren();
 const sel=$('sel');if(sel.options.length==1)d.servers.forEach(s=>sel.add(new Option(s.server+' '+s.role,s.server)));
 tabs(d);mod(d);
 for(const s of d.servers){
  const c=el('div','card srv'+(openCards.has(s.server)?' open':'')),[t,k]=RULE[s.rule]||[s.rule,'mut'],h=el('div','head');
  h.append(el('span','num','#'+s.server),el('h3',0,s.role||('Server '+s.server)));
  if(s.locked){const sh=shown[s.server],on=sh&&sh.until>Date.now(),lk=el('button','lock',on?'🔓 '+sh.pw:'🔒');lk.title=on?'Passwort (verschwindet nach 15 s)':'Passwortgeschützt, Klick zeigt das Passwort';
   lk.onclick=async e=>{e.stopPropagation();try{const r=await fetch('/api/secret?server='+s.server,{headers:{'X-Requested-With':'dashboard'}}),d=await r.json();if(!r.ok)throw new Error(d.error||r.status);shown[s.server]={pw:d.password,until:Date.now()+15000};servers()}catch(x){lk.title='Fehler: '+x.message}};h.append(lk)}
  c.append(h);
  c.onclick=async e=>{if(e.target.closest('button,a,input,.detail'))return;
   if(openCards.has(s.server))openCards.delete(s.server);else{openCards.add(s.server);delete infoAt[s.server]}
   await servers()};
  const mapName=(s.map||'').replace(/[.]$/,''),pic=el('div','pic');pic.append(el('span',0,mapName||'Kein Bild'));
  if(s.running&&s.map&&imgState[mapName]!='none'){const im=el('img','mapimg');im.alt='';im.style.opacity=imgState[mapName]=='ok'?1:0;im.onload=()=>{imgState[mapName]='ok';im.style.opacity=1};im.onerror=()=>{imgState[mapName]='none';im.remove()};im.src='/api/mapimg?name='+encodeURIComponent(mapName);pic.append(im)}
  c.append(pic,Object.assign(el('span','pill '+k,t),{title:'Neustart direkt nur bis '+d.cutoff+' Spieler; mehr Spieler oder unbekannt: nur über den Rollover'}));
  const dl=el('dl','kv'),row=(a,b)=>dl.append(el('dt',0,a),el('dd',0,b));
  row('Spieler',s.running?(s.players==null?'?':s.players)+' / '+(s.max||'?'):'offline');
  if(s.running&&s.players!=null&&s.max){const f=Math.min(1,s.players/s.max),bar=el('div',f>=1?'pbar full':'pbar'),fill=el('span',f>=.85?'bad':f>=.5?'warn':'ok');fill.style.width=Math.max(f*100,s.players?3:0)+'%';bar.append(fill);bar.title=Math.round(f*100)+' % belegt';dl.append(bar)}
  row('Karte',s.running?(mapName||'?'):'–');
  for(const [l,x] of Object.entries(s.slots||{}))row('Slot '+l,x.running?(x.version||'?')+' · '+(x.players==null?'?':x.players)+' Spieler':'aus');
  c.append(dl);
  if(openCards.has(s.server)){c.append(detail(s));if(!(infoAt[s.server]>Date.now()-30000)){infoAt[s.server]=Date.now();Promise.all([api('/api/serverinfo?server='+s.server),api('/api/maps')]).then(([i,m])=>{Object.assign(mapsCache,m);infoCache[s.server]=i;infoAt[s.server]=Date.now();drawn()},x=>{infoCache[s.server]={error:x.message};drawn()})}}
  box.append(c)}
}
function diskWarn(k){const w=$('diskwarn');if(!k){w.hidden=true;return}
 const gb=k.free/1e9,pct=k.free/k.total*100,low=gb<20||pct<10;w.hidden=!low;
 w.textContent='Speicherplatz knapp: nur noch '+gb.toFixed(1)+' GB ('+pct.toFixed(0)+' %) auf der Host-Platte frei'+(gb<5?'. Leaderboards und Settings können nicht mehr schreiben.':'.');
 w.style.borderColor=gb<5?'var(--bad)':'var(--warn)'}
async function containers(){
 const d=await api('/api/containers'),srv=$('containers'),tl=$('tools');srv.replaceChildren();tl.replaceChildren();diskWarn(d.disk);
 if(!d.docker){srv.append(el('div','mut','Docker-Socket nicht eingebunden (DASHBOARD_DOCKER).'));return}
 const t=$('tool');if(!t.options.length)d.containers.filter(c=>c.tool).forEach(c=>t.add(new Option(c.name)));
 const order=c=>(c.state=='running'?0:1);
 for(const c of d.containers.slice().sort((a,b)=>order(a)-order(b)||a.name.localeCompare(b.name))){const e=el('div','card'),run=c.state=='running';
  const hd=el('div','head');hd.append(el('h3',0,c.name),el('span','pill '+(run?'ok':'bad'),c.state));
  e.append(hd,el('div','mut',c.image.replace(/^sha256:(.{12}).*/,'$1')+' · '+c.status));(c.tool?tl:srv).append(e)}
 if(!tl.children.length)tl.append(el('div','mut','Keine Tools gefunden.'));
 if(!srv.children.length)srv.append(el('div','mut','Keine Server-Container gefunden.'))
}
const CATALOG={
 'Karte':[['map <name>','Map wechseln'],['maps','Bekannte Maps'],['map-pool','Pool anzeigen'],['map-pool add <map>','Map in den Pool'],['map-pool remove <map>','Map aus dem Pool'],['map-pool clear','Alle Maps erlauben'],['rotation <minutes>','Rotation (Minuten)'],['rotation off','Rotation aus'],['park <construction|historic|financial> <layout>','Park-Layout'],['park random','Parks zufällig'],['layer-sync on','Layer-Sync an'],['layer-sync off','Layer-Sync aus'],['tod <default|morning|noon|afternoon|evening|night>','Tageszeit']],
 'Votes & Umfragen':[['votes','Vote-Einstellungen'],['votes map on','Map-Vote an'],['votes map off','Map-Vote aus'],['votes kick on','Kick-Vote an'],['votes kick off','Kick-Vote aus'],['votes tod on','Tageszeit-Vote an'],['votes seconds <n>','Vote-Dauer'],['votes cooldown <n>','Vote-Pause'],['vote-cancel','Vote abbrechen'],['vote map <map>','Map-Vote starten'],['vote tod <time>','Tageszeit-Vote starten'],['poll <frage> | <antwort> | <antwort>','Umfrage'],['poll end','Umfrage beenden'],['votes polls <off|admins|everyone>','Wer darf Umfragen']],
 'Spieler':[['players','Spieler & Steam-IDs'],['kick <player>','Kick (bis Neustart)'],['ban <player or id> [name]','Ban'],['unban <id>','Unban'],['bans','Ban-Liste'],['msg <player> <text>','Private Nachricht'],['msg-admins <text>','An alle Admins'],['reserved','Reservierte Slots'],['reserved add <id>','Slot reservieren'],['reserved remove <id>','Slot freigeben'],['admins','Admin-Liste'],['admin add <id>','Admin hinzufügen'],['admin remove <id>','Admin entfernen'],['tpall [player]','Alle zu mir/Spieler'],['tphere <player>','Spieler zu mir'],['parties','Parties'],['parties off','Parties beenden'],['afk-kick <minutes>','AFK-Kick (Minuten)'],['afk-kick off','AFK-Kick aus']],
 'Nachrichten':[['say <text>','Chat als Server'],['announce <text>','Ansage an alle'],['announce-to <player> <text>','Ansage an Spieler'],['announcements list','Timer-Ansagen'],['announcements add <text>','Timer-Ansage +'],['announcements remove <n>','Timer-Ansage −'],['announcements interval <minutes>','Ansage-Intervall'],['announcements clear','Alle Timer-Ansagen löschen']],
 'Server':[['help','Befehlsliste'],['status','Name, Map, Spieler, Code'],['net','Netzwerk'],['name <text>','Servername'],['password <text|off>','Passwort'],['welcome <text|off>','Willkommen-Text'],['listed on','Gelistet'],['listed off','Versteckt'],['chat-color <#badge> [<#text>]','Chat-Farbe'],['activity-log on','Aktivitäts-Log an'],['activity-log off','Aktivitäts-Log aus']],
 'Regeln & Anti-Cheat':[['noclip on','Noclip an'],['noclip off','Noclip aus'],['nobail on','No Bail an'],['nobail off','No Bail aus'],['boosts on','Boosts an'],['boosts off','Boosts aus'],['tuning on','Physik erzwingen'],['tuning off','Physik frei'],['placement <everyone|admins|nobody>','Objekte platzieren'],['objects <number|off>','Objekt-Limit'],['clear-objects','Alle Objekte löschen'],['object-scaling on','Skalieren an'],['object-scaling off','Skalieren aus'],['effects on','Effekte an'],['effects off','Effekte aus'],['speed-check <off|warn|kick>','Speed-Check'],['score-check','Score-Check: Ergebnis'],['score-check <off|warn|kick>','Score-Check setzen'],['score-allow','Erlaubte Score-Mods'],['voice on','Voice an'],['voice off','Voice aus'],['voice-range <m>','Voice-Reichweite']],
 'Netzwerk-Tuning':[['rate <KB/s>','Sendrate'],['crowd <n|off>','Crowd-Budget'],['distances <full> <half> <half-return> <low>','Distanzen'],['bone-scale <1-8|off>','Bone-Scale'],['bone-reach <0.5-20|off>','Bone-Reichweite'],['throwdown-locks on','Throwdown-Sperren an'],['throwdown-locks off','Throwdown-Sperren aus']]};
let people=[],modOn=true,tpOn=false;
function mod(d){modOn=d.mod;tpOn=d.tp;$('modsec').hidden=!d.mod;$('cmdsec').hidden=!d.mod;for(const b of $('nav').children)if(b.dataset.v=='moderator'||b.dataset.v=='befehle')b.hidden=!d.mod;const m=$('msrv');
 if(!m.options.length){for(const id of['msrv','csrv'])d.servers.forEach(s=>$(id).add(new Option(s.server+' '+s.role,s.server)));$('tsrv').add(new Option('alle Server','*'));d.servers.forEach(s=>$('tsrv').add(new Option(s.server+' '+s.role,s.server)));
  $('pscope').add(new Option('nur diesen Server (Auswahl links)','one'));$('pscope').add(new Option('alle Server','*'));
  const sug=$('cmdsug');for(const l of Object.values(CATALOG))for(const [cmd] of l)sug.append(new Option(cmd));
  const c=$('cat');c.add(new Option('Befehlskatalog …',''));for(const [g,l] of Object.entries(CATALOG)){const o=document.createElement('optgroup');o.label=g;for(const [cmd,txt] of l)o.append(new Option(txt+' · '+cmd,cmd));c.append(o)}}}
async function sendCmd(c){c=c.trim();if(!c)return;const o=$('cout');o.textContent='Sende: '+c+' …';
 const r=await fetch('/api/mod',{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'dashboard'},body:JSON.stringify({server:$('csrv').value,action:'console',text:c})});
 const d=await r.json();if(!r.ok){o.textContent='Fehler: '+(d.error||r.status);return}
 await new Promise(x=>setTimeout(x,1500));
 const l=await fetch('/api/mod',{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'dashboard'},body:JSON.stringify({server:$('csrv').value,action:'log',count:14})});
 const t=await l.json();o.textContent='> '+c+'\\n'+(t.text||'(keine Log-Zeilen)');o.scrollTop=o.scrollHeight}
let cmdAt=0;
async function cmdMaps(force){const sv=$('csrv').value;if(!sv)return;
 if(force||!mapsCache[sv]||Date.now()-cmdAt>20000){cmdAt=Date.now();try{Object.assign(mapsCache,await api('/api/maps'))}catch(e){}}
 const x=mapsCache[sv]||{pool:[],available:[],map:''},pool=x.pool||[],av=x.available||[],lower=new Set(pool.map(n=>n.toLowerCase())),rest=av.filter(n=>!lower.has(n.toLowerCase()));
 const fillSel=(sel,groups)=>{const keep=sel.value;sel.replaceChildren();for(const [label,list] of groups){if(!list.length)continue;const o=document.createElement('optgroup');o.label=label;for(const n of list)o.append(new Option(n,n));sel.append(o)}
  if(!sel.options.length)sel.add(new Option('(keine)',''));if([...sel.options].some(o=>o.value==keep))sel.value=keep};
 fillSel($('cmap'),[['Im Pool',pool],['Weitere installierte',rest]]);fillSel($('cpooladd'),[['Installiert, nicht im Pool',rest]]);fillSel($('cpoolrm'),[['Im Pool',pool]]);
 $('cinfo').textContent='Karte: '+((x.map||'?').replace(/[.]$/,''))+' · Pool: '+(pool.length||'alle')+' · installiert: '+av.length}
const armed=(b,f)=>{if(!b.classList.contains('arm')){b.dataset.t=b.textContent;b.classList.add('arm');b.textContent='Sicher?';setTimeout(()=>{if(b.classList.contains('arm')){b.classList.remove('arm');b.textContent=b.dataset.t}},3000);return}b.classList.remove('arm');b.textContent=b.dataset.t;f()};
$('csrv').onchange=()=>cmdMaps(true);
$('cmapgo').onclick=e=>{const m=$('cmap').value;if(m)armed(e.target,()=>sendCmd('map '+m))};
$('cpooladdgo').onclick=()=>{const m=$('cpooladd').value;if(m)sendCmd('map-pool add '+m).then(()=>cmdMaps(true))};
$('cpoolrmgo').onclick=e=>{const m=$('cpoolrm').value;if(m)armed(e.target,()=>sendCmd('map-pool remove '+m).then(()=>cmdMaps(true)))};
$('crotgo').onclick=()=>sendCmd('rotation '+$('crot').value);
$('ctodgo').onclick=()=>sendCmd('tod '+$('ctod').value);
document.addEventListener('DOMContentLoaded',()=>{});
async function who(){if(!modOn)return;people=await api('/api/players');draw()}
function say_(m){$('mres').textContent=m}
async function act(body,b){
 if(b&&!b.classList.contains('arm')&&/^(kick|ban)$/.test(body.action)){b.classList.add('arm');const t=b.textContent;b.textContent='Sicher?';setTimeout(()=>{b.classList.remove('arm');b.textContent=t},3000);return}
 const r=await fetch('/api/mod',{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'dashboard'},body:JSON.stringify(body)});
 const d=await r.json();say_(r.ok?'Gesendet: '+body.action+(body.player?' '+body.player:''):'Fehler: '+(d.error||r.status));if(r.ok)setTimeout(who,1500)}
let sortK='name',sortD=1;
function cmp(a,b){let x,y;if(sortK=='server'){x=(parseInt(a.server)||0)*10+(a.slot=='B'?1:0);y=(parseInt(b.server)||0)*10+(b.slot=='B'?1:0)}
 else if(sortK=='team'){x=(a.team||'\\uffff').toLowerCase();y=(b.team||'\\uffff').toLowerCase()}else{x=a.name.toLowerCase();y=b.name.toLowerCase()}
 return (x<y?-1:x>y?1:0)*sortD}
function draw(){const q=$('q').value.trim().toLowerCase(),tb=document.querySelector('#plist tbody');tb.replaceChildren();
 const rows=people.filter(p=>!q||(p.name+' '+p.steam_id+' '+(p.team||'')).toLowerCase().includes(q)).sort(cmp);
 for(const th of document.querySelectorAll('#plist th[data-k]'))th.textContent=({name:'Spieler',team:'Team',server:'Server'})[th.dataset.k]+(th.dataset.k==sortK?(sortD>0?' ▲':' ▼'):'');
 document.querySelector('#modsec h2').firstChild.textContent='Moderator · '+people.length+' online ';
 if(!rows.length){const tr=el('tr'),td=el('td','mut',people.length?'Kein Treffer.':'Niemand online.');td.colSpan=4;tr.append(td);tb.append(tr);return}
 for(const p of rows){const tr=el('tr'),n=el('td'),tm=el('td','mut',p.team||''),s=el('td','mut'),a=el('td'),w=el('div','act');
  n.append(el('div',0,p.name),el('div','mut',p.steam_id));s.textContent=p.server+(p.slot||'')+' '+p.role;
  const base={server:p.server,slot:p.slot,player:p.name,steam_id:p.steam_id};
  const add=(l,f)=>{const b=el('button',0,l);b.onclick=()=>f(b);w.append(b)};
  add('Nachricht',()=>act({...base,action:'msg',text:$('mtext').value}));
  add('Kick',b=>act({...base,action:'kick'},b));
  add('Ban',b=>act({...base,action:'ban',text:$('mtext').value},b));
  if(tpOn)add('Teleport',()=>act({...base,action:'tp',target:$('mtext').value}));
  a.append(w);tr.append(n,tm,s,a);tb.append(tr)}}
for(const th of document.querySelectorAll('#plist th[data-k]'))th.onclick=()=>{if(sortK==th.dataset.k)sortD=-sortD;else{sortK=th.dataset.k;sortD=1}draw()};
$('q').oninput=draw;$('find').onsubmit=e=>e.preventDefault();
for(const b of document.querySelectorAll('#srvtools button'))b.onclick=()=>act({server:$('msrv').value,action:b.dataset.a,text:$('stext').value}).then(()=>{$('stext').value=''});
$('cat').onchange=()=>{const v=$('cat').value;if(!v)return;$('cmd').value=v;$('cmd').focus();const i=v.search(/[<\\[]/);if(i>0)$('cmd').setSelectionRange(i,v.length);$('cat').value=''};
$('csend').onclick=()=>sendCmd($('cmd').value);$('cmd').onkeydown=e=>{if(e.key=='Enter')sendCmd($('cmd').value)};
for(const b of document.querySelectorAll('[data-c]'))b.onclick=()=>{const c=b.dataset.c;if(c=='map-pool clear')return armed(b,()=>sendCmd(c).then(()=>cmdMaps(true)));sendCmd(c)};
$('addmap').onclick=async()=>{const o=$('cout'),pk=$('pkg').value.trim();if(!pk)return;
 const r=await fetch('/api/addmap',{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'dashboard'},body:JSON.stringify({server:$('pscope').value=='*'?'*':$('csrv').value,package:pk,pool:$('poolnames').value})});
 const d=await r.json();o.textContent=r.ok?'Map-Paket: '+d.text:'Fehler: '+(d.error||r.status);if(!r.ok||!$('live').checked)return;
 const srvs=$('pscope').value=='*'?[...$('csrv').options].map(x=>x.value):[$('csrv').value],names=$('poolnames').value.split(',').map(x=>x.trim()).filter(Boolean);
 $('pkg').value='';$('poolnames').value='';
 for(const sv of srvs){const send=c=>fetch('/api/mod',{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'dashboard'},body:JSON.stringify({server:sv,action:'console',text:c})});
  await send('mods add '+pk);o.textContent+='\\nServer '+sv+': mods add '+pk+' gesendet, lädt ohne Neustart …'}
 await new Promise(x=>setTimeout(x,6000));
 for(const sv of srvs)for(const n of names)await fetch('/api/mod',{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'dashboard'},body:JSON.stringify({server:sv,action:'console',text:'map-pool add '+n})});
 o.textContent+='\\nPool-Eintraege gesendet. Ergebnis im Live-Log; Server ohne `mods add` (altes Image) kennen den Befehl noch nicht.'};
const HDR={'Content-Type':'application/json','X-Requested-With':'dashboard'};
const post=(u,b)=>fetch(u,{method:'POST',headers:HDR,body:JSON.stringify(b)}).then(async r=>{const d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(d.error||r.status);return d});
const sleep=ms=>new Promise(x=>setTimeout(x,ms));
async function tsAdd(r,pool,note,btns){
 btns.forEach(b=>b.disabled=true);const sc=$('tsrv').value,srvs=sc=='*'?[...$('csrv').options].map(x=>x.value):[sc],con=(sv,t)=>post('/api/mod',{server:sv,action:'console',text:t});
 try{note.textContent='Schreibe Settings …';await post('/api/addmap',{server:sc,package:r.package,pool:''});
  for(const sv of srvs)await con(sv,'mods add '+r.package);note.textContent='Paket an '+srvs.length+' Server geschickt, lädt …';
  if(pool){await sleep(8000);let names=[];try{names=(await api('/api/mapname?package='+encodeURIComponent(r.package))).names}catch(e){}
   if(!names.length)names=[r.name];await post('/api/addmap',{server:sc,package:r.package,pool:names.join(', ')});
   for(const sv of srvs)for(const n of names)await con(sv,'map-pool add '+n);note.textContent='Im Pool: '+names.join(', ')+' ('+srvs.length+' Server).'}
  else note.textContent='Auf '+srvs.length+' Server geladen.'}
 catch(e){note.textContent='Fehler: '+e.message}
 btns.forEach(b=>b.disabled=false)}
const tsRun=async()=>{const box=$('tsres'),info=$('tsinfo');box.replaceChildren();info.textContent='Suche …';
 let d;try{d=await api('/api/ts?q='+encodeURIComponent($('tsq').value)+'&sort='+$('tssort').value+'&dir='+$('tsdir').value+($('tsall').checked?'&all=1':''))}catch(x){info.textContent='Thunderstore nicht erreichbar: '+x.message;return}
 info.textContent=d.total+' Treffer'+(d.total>d.results.length?' (die ersten '+d.results.length+' nach Sortierung)':'');
 for(const r of d.results){const row=el('div','tsrow'),pic=el('div','tspic');pic.append(el('span',0,r.name.slice(0,2)));
  if(r.icon){const im=el('img','mapimg');im.alt='';im.loading='lazy';im.onerror=()=>im.remove();im.src='/api/tsimg?u='+encodeURIComponent(r.icon);pic.append(im)}
  const main=el('div','tsmain'),a=el('a',0,r.name);a.href=r.url;a.target='_blank';a.rel='noopener noreferrer';
  const t=el('div','tsname');t.append(a,el('span','mut',' '+r.owner+' · v'+r.version));
  const note=el('div','mut');main.append(t,el('div','mut',r.description),note);
  const st=el('div','tsstat');st.append(el('div',0,r.downloads.toLocaleString('de-DE')+' ↓'),el('div','mut','★ '+r.rating),el('div','mut',r.updated));
  const w=el('div','act'),b1=el('button',0,'Auf Server'),b2=el('button',0,'In den Pool');w.append(b1,b2);
  b1.onclick=()=>tsAdd(r,false,note,[b1,b2]);b2.onclick=()=>tsAdd(r,true,note,[b1,b2]);
  row.append(pic,main,st,w);box.append(row)}};
$('tsform').onsubmit=e=>{e.preventDefault();tsRun()};$('tssort').onchange=$('tsdir').onchange=tsRun;
const mapsCache={};
function chipRow(list,cur,mod){const box=el('div','chips');
 for(const n of list){let t=n,tip='';if(mod){const m=n.match(/^(.*)-(\\d+[.]\\d+[.]\\d+)$/);t=m?m[1]:n;tip=n}
  const ch=el('span','chip'+(mod?' mod':'')+(!mod&&cur&&n.toLowerCase()==cur.toLowerCase()?' on':''),t);if(tip)ch.title=tip;box.append(ch)}
 return box}
let mapsAt=0;
async function poolmaps(){if(view!='suche'||Date.now()-mapsAt<20000)return;mapsAt=Date.now();
 const [d,m]=await Promise.all([api('/api/servers'),api('/api/maps')]),box=$('poolbox');box.replaceChildren();
 for(const sv of d.servers){const x=m[sv.server];if(!x)continue;const c=el('div','card'),h=el('div','head');
  h.append(el('span','num','#'+sv.server),el('h3',0,sv.role||('Server '+sv.server)));c.append(h);
  const cur=(sv.map||x.map||'').replace(/[.]$/,'');
  c.append(el('div','mut','Aktuelle Karte: '+(cur||'?')));
  c.append(el('div','mut','Map-Pool ('+(x.pool.length||'alle')+')'));
  const pool=x.pool.length?chipRow(x.pool,cur,false):el('div','chips');
  if(!x.pool.length)pool.append(el('span','mut','Kein Pool gesetzt: alle Maps erlaubt'));
  c.append(pool);
  if(!x.live)c.append(el('div','mut','Stand vom Containerstart (Live-Daten nicht lesbar)'));
  c.append(el('div','mut','Map-Pakete ('+x.mods.length+')'));
  const mods=x.mods.length?chipRow(x.mods,'',true):el('div','chips');
  if(!x.mods.length)mods.append(el('span','mut','Keine Mods'));
  c.append(mods);box.append(c)}
 if(!box.children.length)box.append(el('div','mut','Keine Server-Daten (Docker-Zugriff nötig).'))}
let chatSrv='*',chatLast=0,chatLines=[],canSay=true;
function tabs(d){canSay=d.say;$('sayform').hidden=!d.say;const t=$('tabs');if(t.children.length)return;
 const all=el('button',0,'Alle Chats');all.dataset.s='*';all.onclick=()=>pick('*');t.append(all);
 for(const s of d.servers){const b=el('button',0,s.server+(s.role?' '+s.role:''));b.dataset.s=s.server;
  b.onclick=()=>pick(s.server);t.append(b)}mark()}
function pick(v){chatSrv=v;chatLast=0;chatLines=[];$('chat').textContent='-';mark();chat()}
function mark(){for(const b of $('tabs').children)b.classList.toggle('on',b.dataset.s==chatSrv);
 const i=$('saytext');i.disabled=chatSrv=='*';i.placeholder=chatSrv=='*'?'Server-Tab wählen, um zu schreiben':'Nachricht an den Server-Chat'}
async function chat(){if(!chatSrv)return;const srv=chatSrv;
 const d=await api('/api/events?'+new URLSearchParams({after:chatLast,server:srv=='*'?'':srv,scope:'chat'}));
 if(srv!=chatSrv)return;chatLast=d.last;
 for(const e of d.events)chatLines.push((e.time||'').slice(11,19)+(srv=='*'?' ['+e.server+']':'')+' '+e.text.replace(/^(\\[[^\\]]*\\]\\s*)?\\[chat\\]\\s*/,''));
 chatLines.splice(0,Math.max(0,chatLines.length-300));const p=$('chat'),b=p.scrollTop+p.clientHeight>=p.scrollHeight-20;
 if(d.events.length||p.textContent=='-'){p.textContent=chatLines.join('\\n')||'(noch keine Chat-Nachrichten)';if(b)p.scrollTop=p.scrollHeight}}
$('sayform').onsubmit=async ev=>{ev.preventDefault();const t=$('saytext').value.trim();if(!t||chatSrv=='*')return;
 const r=await fetch('/api/say',{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'dashboard'},body:JSON.stringify({server:chatSrv,text:t})});
 if(r.ok){$('saytext').value='';chat()}else $('saytext').placeholder=(await r.json()).error||'Fehler'};
let last=0;const lines=[];
async function events(){
 const q=new URLSearchParams({after:last,server:$('sel').value,scope:$('scope').value});
 const d=await api('/api/events?'+q);last=d.last;
 for(const e of d.events)lines.push((e.time||'').slice(11,19)+' ['+e.server+(e.slot?e.slot:'')+'] '+e.text);
 lines.splice(0,Math.max(0,lines.length-400));const p=$('events'),b=p.scrollTop+p.clientHeight>=p.scrollHeight-20;
 if(d.events.length){p.textContent=lines.join('\\n');if(b)p.scrollTop=p.scrollHeight}
}
for(const id of['sel','scope'])$(id).onchange=()=>{last=0;lines.length=0;$('events').textContent=''};
async function toollog(){const n=$('tool').value;if(n)$('toollog').textContent=(await api('/api/toollog?name='+n)).log}
$('tool').onchange=toollog;
const VIEWS=[['uebersicht','Übersicht'],['chat','Chat'],['moderator','Moderator'],['befehle','Befehle & Maps'],['suche','Maps suchen'],['logs','Logs']];
let view='uebersicht';
try{const h=location.hash.slice(1),v=localStorage.getItem('rv');if(VIEWS.some(x=>x[0]==h))view=h;else if(VIEWS.some(x=>x[0]==v))view=v}catch(e){}
function show(v){view=v;mapsAt=0;if(v=='befehle')setTimeout(()=>cmdMaps(),0);try{localStorage.setItem('rv',v)}catch(e){}try{history.replaceState(null,'','#'+v)}catch(e){}
 for(const s of document.querySelectorAll('section[data-v]'))s.classList.toggle('off',s.dataset.v!=v);
 $('diskwarn').classList.toggle('off',v!='uebersicht');
 for(const b of $('nav').children)b.classList.toggle('on',b.dataset.v==v)}
for(const [v,l] of VIEWS){const b=el('button',0,l);b.dataset.v=v;b.onclick=()=>show(v);$('nav').append(b)}
show(view);
document.querySelectorAll('section').forEach((s,i)=>{const h=s.querySelector('h2'),k='fold'+i,b=el('button','fold','–');b.title='Ein-/ausklappen';
 const set=c=>{s.classList.toggle('closed',c);b.textContent=c?'+':'–'};
 b.onclick=()=>{const c=!s.classList.contains('closed');set(c);try{localStorage.setItem(k,c?'1':'')}catch(e){}};
 try{set(!!localStorage.getItem(k))}catch(e){}h.append(b)});
async function tick(){
 for(const f of[servers,who,chat,containers,events,toollog,poolmaps,async()=>{if(view=='befehle')await cmdMaps()}])try{await f()}catch(e){if(e)console.warn(e)}
 $('upd').textContent='aktualisiert '+new Date().toLocaleTimeString()}
tick();setInterval(tick,5000);
</script></body></html>"""

LOGIN = """<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ReSkate Dashboard</title><style>
:root{color-scheme:dark;--bg:#1a1a1a;--fg:#e8e8e8;--line:#333}
body{margin:0;background:var(--bg);color:var(--fg);font:15px system-ui,sans-serif}form{max-width:320px;margin:20vh auto;padding:0 16px;display:grid;gap:10px}
input,button{font:inherit;padding:9px;border-radius:8px;border:1px solid var(--line);background:transparent;color:inherit}p{color:#f97066;margin:0}
</style></head><body><form method="post" action="/login"><h2>ReSkate Dashboard</h2>%s
<input type="password" name="password" placeholder="Passwort" autofocus required><button>Anmelden</button></form></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def address(self):
        return self.headers.get("X-Forwarded-For", self.client_address[0]).split(",")[-1].strip()

    def send(self, code, body, kind="text/html; charset=utf-8", headers=()):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        if not any(k == "Cache-Control" for k, _ in headers):
            self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def json(self, code, body):
        self.send(code, json.dumps(body, ensure_ascii=False), "application/json; charset=utf-8")

    def logged_in(self):
        jar = SimpleCookie(self.headers.get("Cookie", ""))
        return COOKIE in jar and valid(jar[COOKIE].value)

    def cookie(self, value, age):
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
        return ("Set-Cookie", f"{COOKIE}={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={age}{secure}")

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/health":
            return self.json(200, {"ok": True})
        if not self.logged_in():
            if url.path.startswith("/api/"):
                return self.json(401, {"error": "login"})
            return self.send(200, LOGIN % "")
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path == "/":
                return self.send(200, PAGE)
            if url.path == "/api/servers":
                return self.json(200, servers())
            if url.path == "/api/mapimg":
                found = map_image(q.get("name", ""))
                if not found:
                    return self.json(404, {"error": "no picture"})
                return self.send(200, found[1], found[0], headers=[("Cache-Control", "private, max-age=3600")])
            if url.path == "/api/ts":
                try:
                    return self.json(200, ts_search(q.get("q", ""), q.get("all") != "1", q.get("sort", "downloads"), q.get("dir") == "asc"))
                except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as e:
                    return self.json(502, {"error": f"Thunderstore: {type(e).__name__}: {e}"})
            if url.path == "/api/tsimg":
                kind, data = ts_image(q.get("u", ""))
                return self.send(200, data, kind, headers=[("Cache-Control", "private, max-age=86400")])
            if url.path == "/api/mapname":
                return self.json(200, {"names": store_names(q.get("package", ""))})
            if url.path == "/api/maps":
                return self.json(200, server_maps())
            if url.path == "/api/serverinfo":
                return self.json(200, server_info(q.get("server", "")))
            if url.path == "/api/secret":
                if self.headers.get("X-Requested-With") != "dashboard" or not MOD:
                    return self.json(403, {"error": "forbidden"})
                say(f"dashboard: password of server {q.get('server', '')} shown")
                return self.json(200, server_secret(q.get("server", "")))
            if url.path == "/api/players":
                return self.json(200, players() if MOD else [])
            if url.path == "/api/containers":
                return self.json(200, containers())
            if url.path == "/api/toollog":
                return self.json(200, tool_log(q.get("name", "")))
            if url.path == "/api/events":
                path = "/v1/events?" + "&".join(f"{k}={q[k]}" for k in ("after", "server", "scope") if q.get(k))
                return self.json(200, bridge(path))
        except ValueError as e:
            return self.json(400, {"error": str(e)})
        except (urllib.error.URLError, OSError, RuntimeError) as e:
            return self.json(502, {"error": f"bridge or docker not reachable: {e}"})
        self.json(404, {"error": "not found"})

    def say(self):
        if not self.logged_in():
            return self.json(401, {"error": "login"})
        if not (SAY and MOD) or self.headers.get("X-Requested-With") != "dashboard":
            return self.json(403, {"error": "sending is off" if not (SAY and MOD) else "forbidden"})
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 4096)) or b"{}")
            moderate({"server": body.get("server", ""), "action": "say", "text": body.get("text", "")})
        except (ValueError, TypeError) as e:
            return self.json(400, {"error": str(e)})
        except urllib.error.HTTPError as e:
            return self.json(502, {"error": f"bridge: {e.code} {e.read()[:200].decode(errors='replace')}"})
        except (urllib.error.URLError, OSError) as e:
            return self.json(502, {"error": f"bridge not reachable: {e}"})
        say(f"dashboard: chat sent to server {body.get('server')}")
        self.json(200, {"ok": True})

    def mod(self):
        if not self.logged_in():
            return self.json(401, {"error": "login"})
        if not MOD or self.headers.get("X-Requested-With") != "dashboard":
            return self.json(403, {"error": "moderation is off" if not MOD else "forbidden"})
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 4096)) or b"{}")
            result = moderate(body)
        except (ValueError, TypeError, AttributeError) as e:
            return self.json(400, {"error": str(e)})
        except urllib.error.HTTPError as e:
            detail = e.read()[:200].decode(errors="replace")
            if "could not type into the console" in detail:
                detail = "Dieser Server hat keine offene Konsole (Container ohne stdin). Er muss einmal neu angelegt werden."
            return self.json(502, {"error": f"bridge: {e.code} {detail}"})
        except (urllib.error.URLError, OSError) as e:
            return self.json(502, {"error": f"bridge not reachable: {e}"})
        say(f"dashboard: {body.get('action')} on server {body.get('server')}")
        self.json(200, {"ok": True, "text": (result or {}).get("text", "")})

    def addmap(self):
        if not self.logged_in():
            return self.json(401, {"error": "login"})
        if not MOD or self.headers.get("X-Requested-With") != "dashboard":
            return self.json(403, {"error": "forbidden"})
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 4096)) or b"{}")
            text = add_map(body)
        except (ValueError, TypeError, AttributeError) as e:
            return self.json(400, {"error": str(e)})
        except OSError as e:
            return self.json(500, {"error": f"could not write the settings: {e}"})
        say(f"dashboard: map package {body.get('package')} for server {body.get('server')}: {text}")
        self.json(200, {"ok": True, "text": text})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/logout":
            return self.send(200, "ok", headers=[self.cookie("", 0)])
        if path == "/api/say":
            return self.say()
        if path == "/api/mod":
            return self.mod()
        if path == "/api/addmap":
            return self.addmap()
        if path != "/login":
            return self.json(404, {"error": "not found"})
        who = self.address()
        count, until = fails.get(who, [0, 0])
        if until > time.time():
            return self.send(429, LOGIN % "<p>Zu viele Versuche, bitte später erneut.</p>")
        form = parse_qs(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 4096)).decode(errors="replace"))
        given = form.get("password", [""])[0]
        if hmac.compare_digest(hashlib.sha256(given.encode()).digest(), hashlib.sha256(PASSWORD.encode()).digest()):
            fails.pop(who, None)
            return self.send(303, "", headers=[("Location", "/"), self.cookie(sign(int(time.time()) + SESSION), SESSION)])
        count += 1
        fails[who] = [count, time.time() + 300 if count >= 5 else 0]
        time.sleep(1)
        say(f"dashboard: wrong password from {who}")
        self.send(401, LOGIN % "<p>Falsches Passwort.</p>")


def main():
    if len(PASSWORD) < 8:
        sys.exit("dashboard: DASHBOARD_PASSWORD (at least 8 characters) is not set; refusing to start without a login")
    if not BRIDGE_TOKEN:
        sys.exit("dashboard: BRIDGE_TOKEN is not set")
    say(f"dashboard: listening on {BIND}:{PORT} (restart rule: at most {CUTOFF} players)")
    threading.Thread(target=warm_ts, daemon=True).start()
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
