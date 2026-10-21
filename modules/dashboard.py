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
import base64
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
        return f"not saved ({folder} does not exist): the map only stays until the server's next restart"
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
TS_IMG_HOSTS = ("thunderstore.io",)   # and its subdomains (gcdn., ccdn., ...)
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
                         "created": (p.get("date_created") or min((x.get("date_created") or "" for x in p["versions"]), default=""))[:10],
                         "url": p.get("package_url", ""), "categories": p.get("categories", [])})
        TS_CACHE.update(at=time.time(), rows=rows)
    return TS_CACHE["rows"]


def warm_ts():
    try:
        started = time.time()
        say(f"dashboard: Thunderstore list loaded, {len(ts_packages())} packages in {time.time() - started:.1f} s")
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as e:
        say(f"dashboard: Thunderstore list not loaded: {type(e).__name__}: {e}")


TS_SORTS = {"created": lambda r: r["created"], "downloads": lambda r: r["downloads"], "updated": lambda r: r["updated"], "rating": lambda r: r["rating"],
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
    host = u.hostname or ""
    if u.scheme != "https" or not any(host == h or host.endswith("." + h) for h in TS_IMG_HOSTS) or u.port:
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
    """(current map, pool, installed packages, on/off flags) as the running server has them now: its saved settings file
    and the links in its Mods folder, not the environment it was started with."""
    with tar_of(cid, "/data/ReSkateServer.json") as t:
        member = t.getmember("ReSkateServer.json")
        config = json.load(t.extractfile(member))
    maps, votes = config.get("maps", {}), config.get("votes", {})
    pool = [x for x in maps.get("pool", []) if isinstance(x, str)]
    flags = {"vote_map": bool(votes.get("map", {}).get("enabled")), "vote_kick": bool(votes.get("kick", {}).get("enabled")),
             "vote_tod": bool(votes.get("time_of_day", {}).get("enabled")),
             "rotation": int(maps.get("rotation_minutes") or 0), "layer_sync": bool(maps.get("world_layer_sync"))}
    try:
        with tar_of(cid, "/data/Mods") as t:
            mods = sorted(m.linkname.rsplit("/", 1)[-1] for m in t.getmembers() if m.issym())
    except (RuntimeError, OSError, tarfile.TarError):
        mods = []
    return str(maps.get("map") or ""), pool, mods, flags


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
                item["map"], item["pool"], item["mods"], item["flags"] = live_maps(c["id"])
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
<title>ReSkate Dashboard</title><link rel="icon" type="image/png" href="/favicon.png"><style>
:root{color-scheme:dark;--bg:#1a1a1a;--fg:#e8e8e8;--card:#232323;--line:#333;--mut:#9a9a9a;--ok:#4cc38a;--warn:#f0a53c;--bad:#f97066;--acc:#8ea2ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}
header{display:flex;gap:8px 12px;flex-wrap:wrap;align-items:center;padding:12px max(16px,env(safe-area-inset-right,0px),calc((100% - 1100px)/2 + 16px)) 12px max(16px,env(safe-area-inset-left,0px),calc((100% - 1100px)/2 + 16px));border-bottom:1px solid var(--line)}header h1{font-size:17px;margin:0;flex:1 1 auto;display:flex;align-items:center;gap:12px;flex-wrap:wrap}.logo{height:32px;width:auto}.flags{display:grid;gap:2px}.poollist{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:4px 12px;margin-top:8px}.tgl{display:flex;align-items:center;gap:10px;padding:6px 4px;border-radius:6px;cursor:pointer;min-width:0}.tgl:hover{background:var(--line)}.tgl input{position:absolute;opacity:0;width:0;height:0;min-width:0;padding:0;border:0}.tgl .sw{flex:none;width:34px;height:18px;border-radius:9px;background:var(--line);position:relative;transition:background .15s}.tgl .sw::after{content:"";position:absolute;top:2px;left:2px;width:14px;height:14px;border-radius:50%;background:var(--mut);transition:left .15s,background .15s}.tgl.on .sw{background:var(--ok)}.tgl.on .sw::after{left:18px;background:#111}.tgl input:focus-visible+.sw{outline:2px solid var(--acc)}.tgl input:disabled+.sw{opacity:.5}.tgl .tn{min-width:0;overflow-wrap:anywhere;font-size:13px}.ring{flex:none;transform:rotate(-90deg)}.ring circle{fill:none;stroke-width:2.5}.ring .rb{stroke:var(--line)}.ring .rf{stroke:var(--acc);stroke-dasharray:100;stroke-dashoffset:100;stroke-linecap:round}.ring.go .rf{animation:ringfill 5s linear forwards}.ring.busy .rf{stroke-dashoffset:0;animation:none;opacity:.6}@keyframes ringfill{to{stroke-dashoffset:0}}@media(prefers-reduced-motion:reduce){.ring.go .rf{animation:none;stroke-dashoffset:50}}
main{padding:20px max(16px,env(safe-area-inset-right,0px)) 32px max(16px,env(safe-area-inset-left,0px));max-width:1100px;margin:auto;display:grid;grid-template-columns:minmax(0,1fr);gap:28px}section h2{display:flex;flex-wrap:wrap;align-items:center;gap:8px;font-size:13px;font-weight:600;color:var(--mut);text-transform:uppercase;letter-spacing:.05em;margin:0 0 10px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:10px}
.card{min-width:0;overflow-wrap:anywhere;background:var(--card);border:1px solid var(--line);border-radius:6px;padding:12px}.cgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:10px;margin:10px 0}.cgrid h3{margin:0 0 4px;font-size:14px}.crow{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:8px}.crow select{flex:1;min-width:0}button.arm{background:var(--bad);color:#111}details.adv{margin:8px 0}details.adv summary{cursor:pointer;color:var(--mut);padding:4px 0}.chips{display:flex;flex-wrap:wrap;gap:4px;margin:4px 0 8px}.chip{background:var(--line);border-radius:4px;padding:2px 8px;font-size:12px}.chip.on{background:var(--acc);color:#111}.chip.mod{font-family:ui-monospace,monospace;font-size:11px}.card.open{display:grid;grid-template-columns:230px minmax(0,1fr);column-gap:16px;align-items:start;grid-auto-rows:min-content}.card.open>.head{grid-column:1/-1}.card.open>:not(.head):not(.detail){grid-column:1}.card.open>.detail{grid-column:2;grid-row:2/12;margin-top:0;border-top:0;padding-top:0}.card.open .pic{margin-bottom:6px}.dgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:14px}.dblock h4{margin:0 0 6px;font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut)}.dblock .kv{margin:0}@media(max-width:700px){.card.open{display:block}.card.open>.detail{margin-top:10px;border-top:1px solid var(--line);padding-top:10px}}.card.open{grid-column:1/-1}.card.srv{cursor:pointer}.detail{margin-top:10px;border-top:1px solid var(--line);padding-top:10px;cursor:default}.detail .set{max-height:320px;overflow:auto;font-size:12px;margin-top:6px}.lock{margin-left:auto;font-size:13px;padding:2px 8px}.head .lock+.pill{margin-left:0}.pic{position:relative;width:100%;aspect-ratio:16/9;border-radius:4px;margin-bottom:8px;background:repeating-linear-gradient(135deg,var(--line) 0 8px,var(--card) 8px 16px);display:flex;align-items:center;justify-content:center;overflow:hidden;color:var(--mut);font-size:13px;text-align:center;padding:6px}.mapimg{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}.kv{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;margin:8px 0 0;font-size:13px}.kv dt{color:var(--mut)}.pbar{grid-column:1/-1;box-sizing:border-box;width:100%;height:6px;border-radius:3px;background:var(--line);overflow:hidden}.kv dd{font-variant-numeric:tabular-nums}.pbar span{display:block;height:100%;border-radius:3px;background:var(--ok)}.pbar .warn{background:var(--warn)}.pbar .bad{background:var(--bad)}.pbar.full{overflow:visible;animation:shake .5s ease-in-out infinite}.pbar.full span{box-shadow:0 0 6px var(--bad)}@keyframes shake{0%,100%{transform:translateX(0)}20%{transform:translateX(-2px)}40%{transform:translateX(2px)}60%{transform:translateX(-2px)}80%{transform:translateX(2px)}}@media(prefers-reduced-motion:reduce){.pbar.full{animation:none}}.kv dd{margin:0;min-width:0;overflow-wrap:anywhere}.num{background:var(--line);border-radius:4px;min-width:26px;height:26px;padding:0 6px;display:inline-flex;align-items:center;justify-content:center;font-weight:700;font-size:15px}.head h3{flex:1}.card h3{margin:0;font-size:15px;min-width:0;overflow-wrap:anywhere}.head{display:flex;justify-content:space-between;align-items:flex-start;gap:8px;margin-bottom:6px}.pill{display:inline-block;max-width:100%;margin-bottom:6px;box-sizing:border-box}
.tsrow{display:grid;grid-template-columns:64px minmax(0,1fr) 150px auto;gap:12px;align-items:center;background:var(--card);border:1px solid var(--line);border-radius:6px;padding:8px 12px;margin-bottom:6px}.tspic{position:relative;width:64px;height:48px;border-radius:4px;background:var(--line);display:flex;align-items:center;justify-content:center;overflow:hidden;color:var(--mut)}.tsmain{min-width:0;overflow-wrap:anywhere}.tsmain .mut{font-size:12px}.tsname a{font-weight:600}.tsstat{white-space:nowrap;text-align:right;font-variant-numeric:tabular-nums;font-size:13px}.tsrow .act{display:flex;gap:6px}@media(max-width:700px){.tsrow{grid-template-columns:64px minmax(0,1fr)}.tsstat{text-align:left;grid-column:2}.tsrow .act{grid-column:1/-1}}
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
<header><h1><img class="logo" src="/logo.svg" alt="Cocojambo" width="230" height="32"> ReSkate Dashboard</h1><svg id="ring" class="ring" viewBox="0 0 20 20" width="18" height="18" aria-hidden="true"><circle class="rb" cx="10" cy="10" r="8"/><circle class="rf" cx="10" cy="10" r="8" pathLength="100"/></svg><span class="mut" id="upd"></span><button onclick="fetch('/logout',{method:'POST'}).then(()=>location.reload())">Abmelden</button></header>
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
<div class="card"><h3>Schalter</h3><div id="cflags" class="flags"></div>
<div class="crow"><span class="mut">Rotation alle</span><select id="crot"><option>10</option><option>15</option><option>20</option><option selected>30</option><option>45</option><option>60</option><option>90</option><option>120</option></select><span class="mut">Minuten</span></div>
<div class="crow"><button data-c="vote-cancel">Laufenden Vote abbrechen</button></div></div>
<div class="card"><h3>Tageszeit</h3><div class="crow"><select id="ctod"><option value="default">Standard</option><option value="morning">Morgen</option><option value="noon">Mittag</option><option value="afternoon">Nachmittag</option><option value="evening">Abend</option><option value="night">Nacht</option></select><button id="ctodgo">Setzen</button></div></div>
<div class="card"><h3>Info</h3><div class="crow"><button data-c="status">Status</button><button data-c="players">Spieler</button><button data-c="maps">Maps</button><button data-c="net">Netzwerk</button><button data-c="help">Hilfe</button></div></div>
</div>
<div class="card" id="poolcard"><div class="crow" style="margin:0 0 8px"><h3 style="margin:0">Map-Pool</h3><input id="poolfilter" placeholder="Maps filtern" style="flex:1;min-width:120px"><button data-c="map-pool">Pool zeigen</button><button data-c="map-pool clear">Alle Maps erlauben</button></div>
<div class="mut" id="poolhint"></div><div class="poollist" id="poollist"></div></div>
<details class="adv"><summary>Erweitert: Befehl frei eingeben</summary><div class="bar"><select id="cat"></select><input id="cmd" maxlength="300" list="cmdsug" placeholder="Befehl, z. B. status oder map grom"><datalist id="cmdsug"></datalist><button id="csend">Senden</button></div></details>
<div class="mut">Befehle wirken sofort auf den laufenden Server. Nach einem Neustart gelten wieder die Einstellungen des Servers.</div>
<pre id="cout">Antwort des Servers erscheint hier (letzte Log-Zeilen).</pre>
</section>
<section data-v="befehle"><h2>Maps suchen (Thunderstore)</h2>
<form class="bar" id="tsform"><input id="tsq" maxlength="60" placeholder="Suche, z. B. plaza, skatepark, Name"><select id="tsrv"></select><select id="tssort" title="Sortierung"><option value="created">Neueste Maps</option><option value="downloads">Meiste Downloads</option><option value="updated">Zuletzt aktualisiert</option><option value="rating">Beste Bewertung</option><option value="name">Name</option></select><select id="tsdir" title="Richtung"><option value="desc">absteigend</option><option value="asc">aufsteigend</option></select><label class="mut" style="display:flex;gap:6px;align-items:center"><input type="checkbox" id="tsall" style="flex:none;min-width:0">alle Pakete</label><button>Suchen</button></form>
<div class="mut" id="tsinfo">Sucht in der Thunderstore-Community reskate. "Auf Server" lädt das Paket, "In den Pool" nimmt die Map zusätzlich in den Map-Pool auf (ohne Neustart, mit neuem Image).</div><div class="tslist" id="tsres"></div></section>
<section data-v="befehle"><h2>Pool &amp; Maps pro Server</h2><div class="grid" id="poolbox"></div></section>
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
 fillSel($('cmap'),[['Im Pool',pool],['Weitere installierte',rest]]);drawPool(sv,pool,av);drawFlags(sv,x.flags);
 $('cinfo').textContent='Karte: '+((x.map||'?').replace(/[.]$/,''))+' · Pool: '+(pool.length||'alle')+' · installiert: '+av.length}
const armed=(b,f)=>{if(!b.classList.contains('arm')){b.dataset.t=b.textContent;b.classList.add('arm');b.textContent='Sicher?';setTimeout(()=>{if(b.classList.contains('arm')){b.classList.remove('arm');b.textContent=b.dataset.t}},3000);return}b.classList.remove('arm');b.textContent=b.dataset.t;f()};
$('csrv').onchange=()=>cmdMaps(true);
$('cmapgo').onclick=e=>{const m=$('cmap').value;if(m)armed(e.target,()=>sendCmd('map '+m))};
const FLAGS=[['vote_map','Map-Vote','votes map on','votes map off'],['vote_kick','Kick-Vote','votes kick on','votes kick off'],['vote_tod','Zeit-Vote','votes tod on','votes tod off'],['rotation','Map-Rotation',null,'rotation off'],['layer_sync','Layer-Sync','layer-sync on','layer-sync off']];
function drawFlags(sv,fl){const box=$('cflags');box.replaceChildren();
 if(!fl){box.append(el('div','mut','Zustand unbekannt (Server nicht erreichbar).'));return}
 if(fl.rotation>0&&[...$('crot').options].some(o=>o.value==fl.rotation))$('crot').value=fl.rotation;
 for(const [k,label,onc,offc] of FLAGS){const on=k=='rotation'?fl.rotation>0:fl[k],lb=el('label','tgl'+(on?' on':'')),cb=document.createElement('input');cb.type='checkbox';cb.checked=on;
  cb.onchange=async()=>{cb.disabled=true;lb.classList.toggle('on',cb.checked);try{await sendCmd(cb.checked?(onc||'rotation '+$('crot').value):offc);await sleep(1200);delete mapsCache[sv];await cmdMaps(true)}finally{cb.disabled=false}};
  lb.append(cb,el('span','sw'),el('span','tn',label+(k=='rotation'&&on?' ('+fl.rotation+' min)':'')));box.append(lb)}}
function drawPool(sv,pool,av){const box=$('poollist'),f=$('poolfilter').value.trim().toLowerCase(),all=!pool.length,on=new Set(pool.map(n=>n.toLowerCase()));
 const names=[...av];for(const n of pool)if(!names.some(x=>x.toLowerCase()==n.toLowerCase()))names.push(n);
 $('poolhint').textContent=pool.length?pool.length+' von '+names.length+' Maps im Pool. Nur Maps im Pool kommen in die Rotation und in den Vote.':'Kein Pool gesetzt: alle Maps sind erlaubt (alle Schalter an). Schaltest du eine Map aus, gilt ab dann der Pool aus den übrigen Maps.';
 box.replaceChildren();
 for(const n of names){if(f&&!n.toLowerCase().includes(f))continue;
  const isOn=all||on.has(n.toLowerCase()),lb=el('label','tgl'+(isOn?' on':'')),cb=document.createElement('input');cb.type='checkbox';cb.checked=isOn;
  cb.onchange=async()=>{cb.disabled=true;lb.classList.toggle('on',cb.checked);try{await sendCmd('map-pool '+(cb.checked?'add ':'remove ')+n);await sleep(1200);delete mapsCache[sv];await cmdMaps(true)}finally{cb.disabled=false}};
  lb.append(cb,el('span','sw'),el('span','tn',n));box.append(lb)}
 if(!box.children.length)box.append(el('div','mut','Keine Treffer.'))}
$('poolfilter').oninput=()=>{const sv=$('csrv').value,x=mapsCache[sv]||{pool:[],available:[]};drawPool(sv,x.pool||[],x.available||[])};
$('crot').onchange=()=>{const x=mapsCache[$('csrv').value];if(x&&x.flags&&x.flags.rotation>0)sendCmd('rotation '+$('crot').value).then(()=>sleep(1200)).then(()=>cmdMaps(true))};
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
 info.textContent=($('tsq').value.trim()?d.total+' Treffer':d.total+' Maps auf Thunderstore')+(d.total>d.results.length?' (die ersten '+d.results.length+' nach Sortierung)':'');
 for(const r of d.results){const row=el('div','tsrow'),pic=el('div','tspic');pic.append(el('span',0,r.name.slice(0,2)));
  if(r.icon){const im=el('img','mapimg');im.alt='';im.loading='lazy';im.onerror=()=>im.remove();im.src='/api/tsimg?u='+encodeURIComponent(r.icon);pic.append(im)}
  const main=el('div','tsmain'),a=el('a',0,r.name);a.href=r.url;a.target='_blank';a.rel='noopener noreferrer';
  const t=el('div','tsname');t.append(a,el('span','mut',' '+r.owner+' · v'+r.version));
  const note=el('div','mut');main.append(t,el('div','mut',r.description),note);
  const st=el('div','tsstat');st.append(el('div',0,r.downloads.toLocaleString('de-DE')+' ↓'),el('div','mut','★ '+r.rating),el('div','mut','neu '+(r.created||'?')),el('div','mut','Update '+r.updated));
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
async function poolmaps(){if(view!='befehle'||Date.now()-mapsAt<20000)return;mapsAt=Date.now();
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
const VIEWS=[['uebersicht','Übersicht'],['chat','Chat'],['moderator','Moderator'],['befehle','Befehle & Maps'],['logs','Logs']];
let tsLoaded=false;
let view='uebersicht';
try{const h=location.hash.slice(1),v=localStorage.getItem('rv');if(VIEWS.some(x=>x[0]==h))view=h;else if(VIEWS.some(x=>x[0]==v))view=v}catch(e){}
function show(v){view=v;mapsAt=0;if(v=='befehle'){if(!tsLoaded){tsLoaded=true;setTimeout(()=>tsRun(),0)}setTimeout(()=>cmdMaps(),0)}try{localStorage.setItem('rv',v)}catch(e){}try{history.replaceState(null,'','#'+v)}catch(e){}
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
 const t0=Date.now(),rg=$('ring');rg.classList.remove('go');rg.classList.add('busy');
 for(const f of[servers,who,chat,containers,events,toollog,poolmaps,async()=>{if(view=='befehle')await cmdMaps()}])try{await f()}catch(e){if(e)console.warn(e)}
 $('upd').textContent='aktualisiert '+new Date().toLocaleTimeString();
 rg.classList.remove('busy');void rg.getBoundingClientRect();rg.querySelector('.rf').style.animationDuration=Math.max(.5,(5000-(Date.now()-t0))/1000)+'s';rg.classList.add('go')}
tick();setInterval(tick,5000);
</script></body></html>"""

LOGO_SVG = '<svg id="Ebene_2" data-name="Ebene 2" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 4000 556.85"> <defs> <style> .cls-1 { fill: #b3d7ac; } .cls-5 { fill: #ececec; } .cls-1, .cls-2, .cls-3, .cls-4, .cls-5, .cls-6, .cls-7, .cls-8, .cls-9, .cls-10 { stroke-width: 0px; } .cls-2 { fill: #d4a373; } .cls-3 { fill: #dbe1bc; } .cls-4 { fill: #eec6df; } .cls-6 { fill: #808ec7; } .cls-7 { fill: #ac88bd; } .cls-8 { fill: #ffd972; } .cls-9 { fill: #1a1a1a; } .cls-10 { fill: #87cfe4; } </style> </defs> <g id="Ebene_1-2" data-name="Ebene 1"> <g> <g> <path class="cls-10" d="m120.45,160.65c8.45-2.65,19.02-3.96,31.69-3.96,29.59,0,53.89,8.98,72.91,26.94l11.09,10.3,7.92-6.34c14.26-11.62,31.43-17.43,51.5-17.43,1.06,0,2.25.13,3.57.4s2.25.4,2.77.4c8.45,1.58,16.91,6.73,25.36,15.45s13.99,18.1,16.64,28.13c2.11,9.51,3.17,17.18,3.17,22.98,0,22.72-6.86,43.07-20.6,61.02-13.74,17.97-30.9,26.94-51.5,26.94-12.15,0-25.1-6.59-38.83-19.81-13.22-12.68-28.52-19.02-45.97-19.02-15.85,0-29.59,5.02-41.2,15.05-16.91,13.74-25.36,29.85-25.36,48.33s8.97,34.07,26.94,45.16c8.45,5.83,19.54,8.72,33.28,8.72,18.49,0,42.01-6.06,70.53-18.22,13.2-5.8,23.77-8.72,31.69-8.72,12.15,0,25.09,6.08,38.83,18.22,19.54,16.38,29.33,39.36,29.33,68.95,0,19.55-4.9,37.24-14.67,53.09-9.78,15.85-23.37,26.15-40.81,30.9-8.98,2.66-16.12,3.96-21.39,3.96-12.68,0-26.69-7.39-41.99-22.19-12.68-11.62-20.09-17.43-22.19-17.43-3.71,0-9,2.65-15.85,7.92-19.03,14.26-42.28,21.39-69.74,21.39-20.08,0-40.68-4.47-61.8-13.47-23.26-10.56-41.22-27.73-53.89-51.5-5.82-10.56-9.24-18.49-10.3-23.77s-1.58-15.58-1.58-30.9c0-21.12,2.9-37.51,8.72-49.14,10.03-20.06,24.83-34.59,44.39-43.58l14.26-6.34c.51,0,.25-.65-.79-1.98-1.08-1.32-2.52-3.17-4.36-5.55-1.87-2.38-3.85-4.88-5.94-7.53-10.58-12.15-15.85-25.88-15.85-41.22,0-6.34.25-11.35.79-15.05,3.17-22.71,11.74-41.99,25.75-57.84,13.99-15.85,31.82-26.94,53.48-33.28Z"/> <path class="cls-8" d="m574.51,152.73c6.34-1.58,13.2-2.38,20.6-2.38,10.03,0,19.02,1.06,26.94,3.17,8.98,2.65,19.03,9.51,30.12,20.6,11.09,11.09,17.96,21.14,20.6,30.11l3.96,14.26,19.81-7.13c9.51-3.69,19.81-5.55,30.9-5.55,23.77,0,43.98,8.98,60.63,26.94,16.64,17.97,24.96,40.69,24.96,68.16s-9.25,50.98-27.73,68.93l-11.9,11.89,7.92,15.05c5.28,9.51,7.94,26.16,7.94,49.93,0,16.37-.54,27.34-1.58,32.88-1.08,5.55-4.24,12.55-9.52,21-6.34,11.09-15.05,21.27-26.15,30.51-11.09,9.25-22.19,15.2-33.28,17.83-6.88,2.12-14.8,3.17-23.77,3.17-30.65,0-55.75-10.81-75.29-32.49l-14.26-15.05-10.3,10.3c-17.43,17.43-37.78,26.15-61.01,26.15-20.6,0-39.22-6.85-55.86-20.6-16.65-13.72-27.35-31.69-32.1-53.88-1.06-3.69-1.58-9.51-1.58-17.43,0-16.37,4.49-33.55,13.47-51.52l7.92-15.05-9.51-9.51c-14.26-14.79-21.39-32.74-21.39-53.88,0-15.32,4.23-29.59,12.68-42.8,8.45-13.19,20.33-22.7,35.67-28.52,7.92-2.63,15.31-3.96,22.19-3.96h3.17c1.05,0,1.98.14,2.77.4.79.27,1.44.4,1.98.4,3.69,0,6.07-5.55,7.13-16.64v-3.17c1.58-16.37,7.53-31.43,17.83-45.16s22.31-22.71,36.05-26.94Zm97.47,202.06v-5.55c0-12.14-3.83-23.5-11.49-34.07-7.67-10.57-17.04-15.85-28.13-15.85-16.38,1.06-31.18,8.19-44.39,21.39-13.2,13.22-19.81,27.73-19.81,43.58,0,8.46,2.65,17.18,7.92,26.15,5.28,9,11.09,15.05,17.43,18.24,5.82,3.17,12.15,4.75,19.02,4.75,22.71,0,39.89-10.57,51.52-31.71,4.22-7.92,6.86-16.89,7.92-26.94Z"/> <path class="cls-1" d="m1015.87,160.65c8.45-2.65,19.02-3.96,31.69-3.96,29.59,0,53.89,8.98,72.91,26.94l11.09,10.3,7.92-6.34c14.26-11.62,31.43-17.43,51.5-17.43,1.06,0,2.25.13,3.57.4s2.25.4,2.77.4c8.45,1.58,16.91,6.73,25.36,15.45s13.99,18.1,16.64,28.13c2.11,9.51,3.17,17.18,3.17,22.98,0,22.72-6.86,43.07-20.6,61.02-13.74,17.97-30.9,26.94-51.5,26.94-12.15,0-25.1-6.59-38.83-19.81-13.22-12.68-28.52-19.02-45.97-19.02-15.85,0-29.59,5.02-41.2,15.05-16.91,13.74-25.36,29.85-25.36,48.33s8.97,34.07,26.94,45.16c8.45,5.83,19.54,8.72,33.28,8.72,18.49,0,42.01-6.06,70.53-18.22,13.2-5.8,23.77-8.72,31.69-8.72,12.15,0,25.09,6.08,38.83,18.22,19.54,16.38,29.33,39.36,29.33,68.95,0,19.55-4.9,37.24-14.67,53.09s-23.37,26.15-40.81,30.9c-8.98,2.66-16.12,3.96-21.39,3.96-12.68,0-26.69-7.39-41.99-22.19-12.68-11.62-20.09-17.43-22.19-17.43-3.71,0-9,2.65-15.85,7.92-19.03,14.26-42.28,21.39-69.74,21.39-20.08,0-40.68-4.47-61.8-13.47-23.26-10.56-41.22-27.73-53.89-51.5-5.82-10.56-9.24-18.49-10.3-23.77s-1.58-15.58-1.58-30.9c0-21.12,2.9-37.51,8.72-49.14,10.03-20.06,24.83-34.59,44.39-43.58l14.26-6.34c.51,0,.25-.65-.79-1.98-1.08-1.32-2.52-3.17-4.36-5.55-1.87-2.38-3.85-4.88-5.94-7.53-10.58-12.15-15.85-25.88-15.85-41.22,0-6.34.25-11.35.79-15.05,3.17-22.71,11.74-41.99,25.75-57.84,13.99-15.85,31.82-26.94,53.48-33.28Z"/> <path class="cls-4" d="m1469.93,152.73c6.34-1.58,13.2-2.38,20.6-2.38,10.03,0,19.02,1.06,26.94,3.17,8.98,2.65,19.03,9.51,30.12,20.6,11.09,11.09,17.96,21.14,20.6,30.11l3.96,14.26,19.81-7.13c9.51-3.69,19.81-5.55,30.9-5.55,23.77,0,43.98,8.98,60.63,26.94,16.64,17.97,24.96,40.69,24.96,68.16s-9.25,50.98-27.73,68.93l-11.9,11.89,7.92,15.05c5.28,9.51,7.94,26.16,7.94,49.93,0,16.37-.54,27.34-1.58,32.88-1.08,5.55-4.24,12.55-9.52,21-6.34,11.09-15.05,21.27-26.15,30.51-11.09,9.25-22.19,15.2-33.28,17.83-6.88,2.12-14.8,3.17-23.77,3.17-30.65,0-55.75-10.81-75.29-32.49l-14.26-15.05-10.3,10.3c-17.43,17.43-37.78,26.15-61.01,26.15-20.6,0-39.22-6.85-55.86-20.6-16.65-13.72-27.35-31.69-32.1-53.88-1.06-3.69-1.58-9.51-1.58-17.43,0-16.37,4.49-33.55,13.47-51.52l7.92-15.05-9.51-9.51c-14.26-14.79-21.39-32.74-21.39-53.88,0-15.32,4.23-29.59,12.68-42.8,8.45-13.19,20.33-22.7,35.67-28.52,7.92-2.63,15.31-3.96,22.19-3.96h3.17c1.05,0,1.98.14,2.77.4.79.27,1.44.4,1.98.4,3.69,0,6.07-5.55,7.13-16.64v-3.17c1.58-16.37,7.53-31.43,17.83-45.16,10.3-13.74,22.31-22.71,36.05-26.94Zm97.47,202.06v-5.55c0-12.14-3.83-23.5-11.49-34.07-7.67-10.57-17.04-15.85-28.13-15.85-16.38,1.06-31.18,8.19-44.39,21.39-13.2,13.22-19.81,27.73-19.81,43.58,0,8.46,2.65,17.18,7.92,26.15,5.28,9,11.09,15.05,17.43,18.24,5.82,3.17,12.15,4.75,19.02,4.75,22.71,0,39.89-10.57,51.52-31.71,4.22-7.92,6.86-16.89,7.92-26.94Z"/> <path class="cls-2" d="m2054.73,154.31c9.51-3.17,19.27-4.75,29.32-4.75,16.37,0,30.38,3.69,41.99,11.09,8.45,5.28,15.86,13.47,22.2,24.56,6.34,11.09,10.02,22.19,11.09,33.28,0,1.06.11,2.65.4,4.75.25,2.12.4,3.71.4,4.75,0,18.49-5.29,33.56-15.85,45.16-10.58,11.63-24.31,17.45-41.22,17.45-7.92,0-11.89.52-11.89,1.58s1.85,9.51,5.55,25.36c2.65,12.15,3.96,24.31,3.96,36.45,0,20.09-2.65,34.61-7.92,43.58l-5.55,11.11,13.47,15.05c13.74,16.37,20.6,33.8,20.6,52.29,0,25.89-11.36,46.49-34.07,61.8-12.68,7.92-26.94,11.89-42.79,11.89-29.06,0-51.25-10.02-66.57-30.11l-9.51-13.47-9.51,15.05c-10.57,17.97-31.43,28.52-62.6,31.69-1.58,0-3.57.14-5.94.4-2.38.28-4.1.4-5.15.4-7.92,0-17.97-2.63-30.11-7.92-20.09-9.51-35.8-23.63-47.16-42.39-11.36-18.75-17.04-39.74-17.04-62.99s5.55-45.69,16.64-68.95c9.51-20.06,23.11-36.18,40.82-48.33,17.69-12.14,35.51-18.22,53.48-18.22,19.02,0,34.34,6.88,45.96,20.6,5.82,6.88,8.72,17.7,8.72,32.49,0,12.17-1.85,24.31-5.55,36.45-3.17,10.58-4.49,16.13-3.96,16.65h.79c3.17,0,8.72-2.11,16.64-6.35,7.92-4.22,14.53-8.72,19.81-13.47,17.45-15.85,27.46-40.14,30.12-72.9v-11.09c0-12.15-.79-20.33-2.38-24.56l-3.96-9.52-17.43,12.69c-11.63,7.92-24.05,11.89-37.25,11.89-23.77,0-41.74-8.72-53.88-26.16-8.46-12.14-12.68-25.61-12.68-40.41,0-17.43,6.07-32.74,18.22-45.96,14.26-14.26,30.9-21.39,49.92-21.39,14.79,0,28,4.23,39.63,12.68l9.51,7.13,16.64-11.89c10.03-7.4,21.39-13.2,34.07-17.43Z"/> <path class="cls-3" d="m2383.58,165.41c7.4-4.23,16.12-6.34,26.15-6.34,16.37,0,30.38,4.23,42.01,12.68,19.02,13.74,30.11,31.44,33.28,53.09,1.05,7.92,2.38,12.68,3.96,14.26s6.59,3.44,15.05,5.55c22.71,5.83,39.62,18.51,50.71,38.05,6.34,11.62,9.51,26.94,9.51,45.96s-2.11,33.28-6.34,41.2l-6.34,11.09,11.09,13.47c16.91,21.68,25.36,47.03,25.36,76.08,0,19.02-2.5,34.34-7.53,45.96-5.02,11.63-13.87,21.68-26.54,30.11-10.57,6.88-19.81,10.3-27.73,10.3-6.34,0-11.89-.25-16.64-.79-17.97-1.05-31.17-4.47-39.62-10.3-19.55-12.68-34.88-30.11-45.97-52.29l-9.51-19.02-3.96,12.68c-7.92,25.36-25.36,44.91-52.29,58.63-13.22,6.88-25.1,10.3-35.66,10.3h-8.72c-27.48-1.58-47.55-15.05-60.23-40.41-6.34-11.09-9.51-21.92-9.51-32.49,0-1.58.13-4.22.4-7.92.27-3.69.4-6.86.4-9.51,1.58-29.05,10.03-53.09,25.36-72.12l7.92-9.51-7.13-15.05c-5.82-11.62-8.72-24.29-8.72-38.03,0-15.31,3.44-30.38,10.3-45.16,12.15-23.78,29.59-37.78,52.31-42.01,4.75-.52,7.92-1.84,9.51-3.96,1.58-2.11,2.9-6.59,3.96-13.47,3.17-25.36,18.22-45.69,45.16-61.01Zm57.06,152.94c-7.41-14.26-16.92-21.39-28.54-21.39-7.92,0-15.18,3.69-21.79,11.09s-10.97,17.43-13.07,30.11c0,1.06-.14,2.38-.4,3.96-.27,1.58-.4,2.65-.4,3.17,0,8.98,2.38,17.7,7.13,26.15,4.75,8.46,10.57,14.55,17.43,18.22,5.82,2.66,9.64,3.96,11.49,3.96s5.42-1.3,10.7-3.96c6.34-3.17,12.01-9.37,17.05-18.62,5.01-9.24,7.53-18.08,7.53-26.54,0-7.92-2.38-16.64-7.13-26.15Z"/> <path class="cls-7" d="m2732.24,155.11c9.51-3.17,20.33-4.75,32.49-4.75,23.23,0,43.06,4.23,59.43,12.68,6.86,3.69,15.32,10.7,25.36,21s17.16,19.68,21.41,28.13l6.34,11.09,8.72-15.05c16.37-30.11,41.73-48.6,76.07-55.46,8.45-1.58,16.64-2.38,24.56-2.38,27.46,0,50.98,9.24,70.53,27.73,9.51,8.98,15.71,17.43,18.62,25.36,2.9,7.92,4.36,20.6,4.36,38.03,0,19.55-2.65,34.86-7.92,45.97l-7.13,15.05,8.72,5.55c5.28,3.17,10.3,9.78,15.05,19.81,4.22,10.05,6.34,20.08,6.34,30.11,0,22.19-7.92,39.9-23.77,53.1l-11.09,9.51,7.92,12.68c7.92,12.68,11.89,26.67,11.89,41.99,0,19.02-6.08,35.4-18.22,49.13-9.51,11.09-18.22,18.22-26.15,21.39-7.92,3.17-21.14,4.75-39.63,4.75-35.39,0-60.49-8.43-75.27-25.36-13.22-14.79-19.81-30.9-19.81-48.33s7.13-41.99,21.39-72.12c13.2-26.4,19.81-42.52,19.81-48.33,0-2.63-1.33-5.8-3.96-9.51-1.58-2.63-3.44-3.96-5.55-3.96-3.71,0-11.09,11.36-22.19,34.07-6.34,13.22-11.09,21.79-14.26,25.75s-6.62,6.23-10.3,6.75h-2.38c-4.24,0-8.46-2.9-12.68-8.73-4.24-5.8-10.84-17.43-19.82-34.86-8.98-16.89-15.58-25.36-19.81-25.36-2.65,0-5.28,2.91-7.92,8.72-1.06,3.17-1.58,5.29-1.58,6.34,0,4.23,5.82,18.76,17.43,43.58,13.2,26.95,20.87,45.7,22.98,56.27,1.06,7.4,1.6,12.95,1.6,16.64,0,14.8-3.71,28.4-11.11,40.81-7.4,12.42-17.16,21.53-29.32,27.34-17.43,7.92-36.99,11.89-58.63,11.89-11.09,0-19.81-1.05-26.15-3.17-11.09-3.17-21.53-9.51-31.3-19.02-9.79-9.51-15.99-19.27-18.63-29.32-1.58-6.86-2.38-14.52-2.38-22.98,0-18.49,3.17-33.28,9.51-44.37l7.92-12.68-11.09-9.51c-15.85-14.28-23.77-32.76-23.77-55.48,0-16.37,4.75-30.11,14.26-41.2l7.92-9.51-8.72-17.43c-6.34-14.28-9.51-29.59-9.51-45.97s2.65-29.84,7.92-40.41c3.69-8.97,10.7-17.96,21-26.94,10.31-8.98,20.47-15.32,30.52-19.02Z"/> <path class="cls-6" d="m3321.79,185.22c17.96-16.37,40.41-24.56,67.36-24.56,39.08,0,67.87,15.32,86.37,45.96,6.86,11.63,10.3,24.31,10.3,38.03,0,23.77-9.24,45.7-27.73,65.78l-12.68,14.26,11.09,6.34c24.29,12.15,42.79,30.11,55.46,53.88,5.28,10.05,8.59,17.57,9.9,22.6s1.98,14.39,1.98,28.13c0,20.08-3.17,36.45-9.51,49.13-10.57,21.66-26.27,38.97-47.14,51.9-20.87,12.96-43.45,19.41-67.75,19.41-11.09,0-22.19-1.58-33.29-4.75-23.25-6.85-39.35-15.56-48.33-26.15-2.11-2.63-4.09-3.82-5.94-3.57-1.85.27-5.94,2.52-12.28,6.73-9.51,5.83-22.19,9.51-38.03,11.09-1.58,0-3.71.14-6.34.4-2.66.28-4.5.4-5.55.4-8.46,0-18.51-2.89-30.11-8.72-13.22-6.34-23.66-15.45-31.31-27.34-7.65-11.89-11.49-24.42-11.49-37.64,0-4.23.27-7.4.79-9.51,1.58-8.98,5.82-18.75,12.68-29.32,6.86-10.57,13.47-17.7,19.81-21.41l10.31-4.75-11.11-15.85c-7.4-10.56-11.09-23.23-11.09-38.03,0-13.72,3.69-25.61,11.09-35.66,7.4-9.51,9.52-15.58,6.35-18.22-5.29-3.69-10.31-11.76-15.07-24.18-4.75-12.39-7.13-22.84-7.13-31.3,0-37.5,19.02-56.26,57.06-56.26,14.79,0,27.73,2.9,38.83,8.72l16.64,8.72,15.85-14.26Zm65.78,212.36c-9-5.26-18.51-7.92-28.54-7.92-12.68,0-23.77,3.71-33.28,11.09-9.51,7.41-14.26,16.13-14.26,26.16,0,8.98,4.49,17.16,13.47,24.56,10.03,8.45,21.92,12.68,35.66,12.68,21.12,0,35.67-9.24,43.59-27.73,1.05-1.58,1.58-4.49,1.58-8.72,0-13.2-6.08-23.25-18.22-30.12Zm-11.09-126c-6.88-5.8-14.01-8.72-21.41-8.72-6.86,0-14.26,2.38-22.19,7.13s-12.95,9.79-15.05,15.07c-.52,1.06-.79,2.9-.79,5.55,0,4.75,1.58,9.64,4.75,14.66,3.17,5.02,7.13,8.32,11.89,9.9,5.82,2.12,11.62,3.17,17.43,3.17,9.51,0,17.83-2.77,24.97-8.32,7.13-5.55,10.7-12.01,10.7-19.41s-3.45-13.74-10.3-19.03Z"/> </g> <circle class="cls-5" cx="3747.24" cy="416.26" r="135.82"/> <path class="cls-9" d="m3641.54,391.59c-.49,0-.99-.06-1.49-.21-2.89-.82-4.58-3.83-3.75-6.73,10.76-37.76,39.61-67.18,77.18-78.7,2.88-.9,5.93.73,6.8,3.61.88,2.87-.72,5.93-3.61,6.8-34.02,10.44-60.15,37.09-69.9,71.27-.68,2.39-2.86,3.96-5.24,3.96Z"/> <path class="cls-5" d="m3988.49,26.59l-16.27,42.66c-.75,1.97-.48,4.17.71,5.9l25.92,37.59c3.01,4.36-.29,10.28-5.58,10.01l-45.6-2.29c-2.1-.11-4.12.83-5.4,2.5l-27.74,36.27c-3.22,4.21-9.86,2.9-11.24-2.21l-11.91-44.08c-.55-2.03-2.06-3.66-4.05-4.36l-43.06-15.17c-5-1.76-5.8-8.48-1.37-11.38l38.24-24.95c1.76-1.15,2.84-3.09,2.9-5.19l1.12-45.65c.13-5.29,6.28-8.14,10.4-4.82l35.55,28.66c1.64,1.32,3.82,1.75,5.83,1.15l43.76-13.04c5.08-1.51,9.68,3.45,7.8,8.4Z"/> <path class="cls-5" d="m3699.2,458.56c-2.41,0-4.34-1.45-6.85-3.62-1.89-1.65-7.51-7.78-.01-17.48l17.24,13.35-8.62-6.67,8.63,6.65c1.23-2.05,1.71-7.94-2.64-12.03-4.35-4.11-10.21-3.25-12.8-1.39,1.11-.8,6.91-5.64,35.05-34.65,11.08-11.43,27.1-25.41,44.05-40.22,39.7-34.68,99.69-87.1,95.28-111.91-.33-1.88-1.35-7.59-14.22-12.53-31.4-12.05-47.79-26.84-50.1-45.23-3.99-31.68,35.37-59.6,73.42-86.61,15.4-10.92,29.95-21.24,38.56-29.95l15.5,15.33c-9.95,10.06-25.24,20.91-41.44,32.41-28.18,19.99-66.77,47.37-64.41,66.1,1.18,9.41,13.73,18.95,36.28,27.61,16.17,6.2,25.54,15.99,27.86,29.05,6.1,34.29-38.6,76.39-102.4,132.14-16.6,14.51-32.29,28.2-42.74,38.98-30.16,31.1-39.44,40.67-45.64,40.67Z"/> <rect class="cls-5" x="3760.51" y="290.13" width="121.51" height="89.67" rx="2.17" ry="2.17" transform="translate(1174.25 -2441.28) rotate(41.36)"/> </g> </g> </svg>'
FAVICON_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAGAAAABgCAAAAADH8yjkAAAAIGNIUk0AAHomAACAhAAA+gAAAIDoAAB1MAAA6mAAADqYAAAXcJy6UTwAAAACYktHRAD/h4/MvwAAAAd0SU1FB+oKFQ0fKz9CScUAAAkeSURBVGjerVlpkFTVFf7Ou69nBsGwDWEYRFGJDCouiaIQUVQYxCUmKFVikUCilqa0SrHUaGHUwjIipoyoEY1BKxbGMUjiEsSYEQkYBA0OUTAlIKRE2QdB4tD97r3ny49mdHp7vXH/ddd95zv7doWo6hCiNJSCF4Lq6Ktsefp/xgk7+WQOw6zmqOUNOGMzI6WSSnWRp2ZcqVICYC9WjViScCoknZowQKa6qgaIULNr3KMJWk8xZtm0NuihVdEPYcTgqhQ997ScG+JORhk6CqvjXxAB9GbeurP3bGr7AgEuQJCpo+okcBwH87WmgwS+xywjVyUBAUQgAA0CQlUwGc5k3KkCgBBPJNM/VAHAd5+Y4zaVq4dqlXO70jOYSJepIVYKoEpn+UlzhtcHWEh7qADoVB/pga4KFzTspR4qAM+PRiMwWQr/G22WF1UIoI5/CFGb5SKCIXvoD1UuWuBMyknG9zQbb4UiI6FKhfWAsu+T7WteX+Ez/w50weUuzCgPFdqAqlT168d+7feBCQSCAZ9nKqlSFVHgNRX02dkJIOqVkNpt07NvViwCLXcO70wFgrGTT+orgGA5XZdrFacKigt3TvgwCM9ZQiDQ0W8I932+ecNabcosOVXwv2U40PBm+7cgQM2/mNKsECBZeRzQ8uNjgUt2kuchMLiHluqdjazNxKkMQGnZNgBmNv0B3osanJrszBGHJFUoLVd/G/1aGXnH5QjlH3T59FMhgNKxrR+Grk+3K/uPwi05Kag6AM8tg3HC54yoqo6/vvTLHM1UA6CqbEbjxjTXSlV6ZbUAqvQ23bWp5ePAfEbpX0rnCtMvHYBeqT7Nc3IYhhzQTrehxtAvNRdRGHz2xo5AhVC8+x94gaYzsUAE1XfXXt5qGn/C6sADRDtqNs8SOOZppnN5K9VxmlGLS+lUPbf1RYhr9lKtK+w+ZdlAPbf1xCSTWEuv6jgfUoOGWdvpHamHAsDyRfTdNBy30qrS8ZV6mACHT1tfMILLBrgW4zgTg9rpVWm547paiKCuhS5eSSUCMNmEB7mhF56kVVU6z40390cIWZ7Ty1UAoI6rgPfJaRhJVaXSW+X+x3sJLqoAQKk2IzjV8n4Mt57LgFY6ek+ls5YLgQH76OMA8sQBhQwNu3p5gFaMCZWjz8HTgBfxgiAEjgOSqfgwyBdoFPvGcg3F+fQIQAafvouxcClMxSsbjNm5zTgKgLeBxvqYMM4faEo3AXLyw9uo3nqqquUf0b+dXtkxFDO4ole3hYxUNXUycENOP13UBpaLUVsH9Jq23FOtU01yGprXzZ/z0MN/vxJNawZBMIc2xftgzOryjez4FvDAL/oActpvd1G95d4jUZdWpkHYGyIGt5FLTYirKogDVZ6P87n7yTMF6HvNSs+XmiAwCROGBoAIgBA/3XAEcOSO7G66FADHt4CnaXXF1X0AnDMRgMn2hkB6CLC4klSh6ngFhu13TrljzlkJIAgD4JSR2QgB5hYu9jEAVM8PumM2rbOOSxNiAsGEVi7N8kdB3eqDa5ByAdTyRjRupbdcdhhg0PAyuf7IXIfvvqgoQt5cpJ5b+uNmpriuHmIwciv53sBc+gJ5qqJsqrT8FXqs5Z4TYQzGdpDPH5Yv6MVgZhE3yp9NVblvKCZzCsIAp+6luxlB3uotIVozxoFSAWg5Dz2nhxD0+oh7mhHmTziCug8rAaCq+tMRCAL8nu0jY1Yazxbx1EIFRx0XwSDAePJyJApQFzxO5yOrZbeOStW9AxEgeIfPx/Avlz3xsafS2kLtaQEvUpviKwgMriBHx3dnNafdvmQ/lT7K26LmAqjSW1VdMwoB5H22xtaTxiEAMHDyM5uZv8lGLvPOKTtaxgQAkGi5qy6GvGDoF8tmnFELoO6su1fk6/OQxbxar/x05jFA5xoiviBiJZ1ufGpSAyCYWgQgvcPSt6f0ABI/uQ0C5C5sMo/BHHZ4KtuX3H8q8F5uTHR1ECHD/X9+ciUxcMrUYU8g8MjesuaeNiQkQqLPqN1BgRaiqwTJe/oDOGPePkacBVOMOBDgLKa8ct2N9QhwfZ681AVALe9A0G/Km5Zqk5xdykZSMMQy9deLaoCeP3uXGmsD9RyDAc9RlS5Kcm4pEgiO2DTneAiGPrCVWsRN1fIOGDlqastWquXLpU0/tX2BsPkvyex2M78XddzVZAAcPvaB97i5phT6EPS9fg1Vrc8/7GTGgadb+8jFfQBI03X1JQH0/s0uqrNayqR/MA64++XrhwUoGmIAYHAFrS19TlaqixyV0fuzx3YvgX+Dh5iKHQRzkp1SvbVK1d1nlmDm4MNKBhCluijiohJEOHxLRUWfVPX8oqGoGRK4kVYrKZlUyxaYeABBT6ldGy9CnAQXoDE2FgL0XHkKroxvvWKK/gcJLPp5TEIKBC9wHmRprJkLAljehqbUgVExCGELIx2FcfQxIhTsi/jl0ZhJv+v0fDlPDIBhbzNyfA1YEDemFQKwfA7d19Ny34WZriTpZzJB/Qp6tREv7bLTLBlA1XM8JtFpKuIMJBCEgBgTHpTmmPO6ATXNCzvouLIGj9GWKYE6rjZ4lSnHjkeHAnXfJKZvjf4OTou48e7BEBzzyw2e12Jwe+FpvwCA5XSclPSqLx4P9Lj7s9du6oZBY3506zOrdvP7uJMpZcdLF9cCdZe8vqkBMwtbodAAsmcQ7qMuHw3UXLOF3nESmtPLqI9CLKXzVpUfzxgMwXePx/mFPTW/BJbPoPf2Ty4LgIv/TW99kouBfzJpk5yLo/enOyin/OpP42tgcHu5EjheiOY7E8CIVh6sVf50XE2rjhPxY9qD799WletuGXFLR5l7U/U8F90Ex853dC7NLh9Dn0/puLsBz3byq51rQV9uHDgurkW/2V+xsxaq585GzGKSi9Djv98QVKqz3pa7+VUqN726nV2+VMubcOIBx+k4N4thrSBdpzvxroypY1sCCxidgnuLLHBKAji4Cu+yVVPHH2A82wTvFCmSpQDkw7R8Cdj0Oxx3oNh6IuOU/lBnMOEkPLEUZ9e5kr8BynjmUssHUd8bL5RlgjJeQNRze3+gcVexFVTmKeM1lgxWzZLbR6iU0PJ9Uz/KABBCQAjLAfg/bUhRzZN6yUYAAAAZdEVYdFNvZnR3YXJlAEFkb2JlIEltYWdlUmVhZHlxyWU8AAAAAElFTkSuQmCC")

LOGIN = """<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ReSkate Dashboard</title><link rel="icon" type="image/png" href="/favicon.png"><style>
:root{color-scheme:dark;--bg:#1a1a1a;--fg:#e8e8e8;--line:#333}
body{margin:0;background:var(--bg);color:var(--fg);font:15px system-ui,sans-serif}form{max-width:320px;margin:20vh auto;padding:0 16px;display:grid;gap:10px}
input,button{font:inherit;padding:9px;border-radius:8px;border:1px solid var(--line);background:transparent;color:inherit}p{color:#f97066;margin:0}
</style></head><body><form method="post" action="/login"><img src="/logo.svg" alt="Cocojambo" style="width:100%%;height:auto;margin-bottom:6px"><h2>ReSkate Dashboard</h2>%s
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
        if url.path == "/logo.svg":
            return self.send(200, LOGO_SVG, "image/svg+xml", headers=[("Cache-Control", "public, max-age=86400")])
        if url.path in ("/favicon.png", "/favicon.ico"):
            return self.send(200, FAVICON_PNG, "image/png", headers=[("Cache-Control", "public, max-age=86400")])
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
