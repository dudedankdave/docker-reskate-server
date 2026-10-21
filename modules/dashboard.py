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
from functools import reduce
from math import gcd
import glob
import gzip
import hashlib
import re
import shutil
import stat
import hmac
import io
import http.client
import json
import os
import secrets
import socket
import sqlite3
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
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


def ts_state(results):
    """Add `stored` ("current" = this version is in the shared store, "old" = another version, "" = not downloaded) and
    `on` (servers that have it linked) to Thunderstore results."""
    try:
        have = set(os.listdir(f"{SHARED}/packages"))
    except OSError:
        have = set()
    linked = {}
    try:
        for number, item in server_maps().items():
            for m in item.get("mods", []):
                linked.setdefault(m.rsplit("-", 1)[0], set()).add(number)
    except (RuntimeError, OSError, ValueError, KeyError):
        pass
    out = []
    for r in results:
        kind = "current" if f"{r['package']}-{r['version']}" in have else "old" if any(x.startswith(r["package"] + "-") for x in have) else ""
        out.append(dict(r, stored=kind, on=sorted(linked.get(r["package"], ()), key=lambda n: (len(n), n))))
    return out


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


JOINED = re.compile(r"^(?:\[[^\]]*\] )?(.+) joined \((\d{17})(?:, [a-z]+)?\), \d+/\d+ players")
LEFT = re.compile(r"^(?:\[[^\]]*\] )?(.+?) left \(.*\)(?: \[.*\])?$")
UPLINE = re.compile(r"^(?:\[[^\]]*\] )?.+ is up on .+ for \d+ players\.$")
LIVE = {}      # container id -> (read at, {steam_id: name})


def live_from_log(cid, started):
    """Who is on a server container, from its own join/leave lines since it started (cached for 5 s). The bridge follows
    the same lines; this is the check that does not depend on it."""
    hit = LIVE.get(cid)
    if hit and time.time() - hit[0] < 5:
        return hit[1]
    people = {}
    text = demux(docker_get(f"/containers/{cid}/logs?stdout=1&stderr=1&since={int(started)}"))
    for line in text.splitlines():
        if UPLINE.match(line):
            people.clear()
        elif j := JOINED.match(line):
            people[j.group(2)] = j.group(1)
        elif left := LEFT.match(line):
            for steam_id in [i for i, n in people.items() if n == left.group(1)][:1]:
                del people[steam_id]
    LIVE[cid] = (time.time(), people)
    return people


def server_players(number, rows=None):
    """[{name, steam_id, slot, team}] on a server now: the bridge's list plus everybody the server's log shows as joined."""
    out = {}
    try:
        for p in bridge(f"/v1/servers/{number}/players"):
            out[p.get("steam_id") or p.get("name")] = p
    except (urllib.error.URLError, OSError, ValueError):
        pass
    if DOCKER:
        try:
            for c in json.loads(docker_get("/containers/json")):
                labels = c.get("Labels") or {}
                if str(labels.get("reskate.server")) != str(number):
                    continue
                info = json.loads(docker_get(f"/containers/{c['Id']}/json"))
                started = time.mktime(time.strptime(info["State"]["StartedAt"][:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
                for steam_id, name in live_from_log(c["Id"], started).items():
                    out.setdefault(steam_id, {"name": name, "steam_id": steam_id, "slot": labels.get("reskate.slot", "a"), "team": ""})
        except (RuntimeError, OSError, ValueError, KeyError):
            pass
    return sorted(out.values(), key=lambda p: p["name"].lower())


def players():
    """Everybody who is on, from every running server: [{name, steam_id, team, server, slot, role}]."""
    out = []
    for r in bridge("/v1/servers"):
        if not r.get("running"):
            continue
        for p in server_players(r["server"]):
            out.append(dict(p, server=r["server"], role=r.get("role", "")))
    return out


class Unix(http.client.HTTPConnection):
    def __init__(self, path, timeout=10):
        super().__init__("docker", timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def docker_get(path):
    """GET on the Docker socket (everything the dashboard reads; its one write is docker_put_file)."""
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


def docker_put_tar(cid, directory, fill):
    """Write into a container through the Docker archive API (`fill` adds entries to the tar); the only writes the dashboard
    sends to the socket are files and symlinks owned by uid 1000."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        fill(t)
    conn = Unix(DOCKER)
    try:
        conn.request("PUT", f"/v1.41/containers/{cid}/archive?path={directory}", body=buf.getvalue(),
                     headers={"Content-Type": "application/x-tar"})
        resp = conn.getresponse()
        resp.read()
        if resp.status != 200:
            raise RuntimeError(f"docker {resp.status}")
    finally:
        conn.close()


def docker_restart(cid, grace=20):
    """POST /containers/<id>/restart: the one other write the dashboard sends to the socket (see restart_server)."""
    conn = Unix(DOCKER, timeout=grace + 40)
    try:
        conn.request("POST", f"/v1.41/containers/{cid}/restart?t={int(grace)}")
        resp = conn.getresponse()
        resp.read()
        if resp.status != 204:
            raise RuntimeError(f"docker {resp.status}")
    finally:
        conn.close()


def docker_put_file(cid, directory, name, data):
    def fill(t):
        info = tarfile.TarInfo(name)
        info.size, info.mode, info.uid, info.gid = len(data), 0o644, 1000, 1000
        t.addfile(info, io.BytesIO(data))
    docker_put_tar(cid, directory, fill)


def docker_put_link(cid, directory, name, target):
    def fill(t):
        info = tarfile.TarInfo(name)
        info.type, info.linkname, info.mode, info.uid, info.gid = tarfile.SYMTYPE, target, 0o777, 1000, 1000
        t.addfile(info)
    docker_put_tar(cid, directory, fill)


MODS_MAX = int(float(os.environ.get("DASHBOARD_MODS_MAX_GB", "3")) * 1024 ** 3)
MARKER = ".thunderstore.json"


def store_package(owner, name, version):
    """Make sure the shared store (SHARED/packages/<Owner-Name-version>) holds this package version; returns its folder."""
    final = f"{SHARED}/packages/{owner}-{name}-{version}"
    if os.path.exists(f"{final}/{MARKER}"):
        return final
    tmp = f"{SHARED}/.tmp"
    os.makedirs(tmp, exist_ok=True)
    work, archive = f"{tmp}/dash-{owner}-{name}-{version}", f"{tmp}/dash-{owner}-{name}-{version}.zip"
    shutil.rmtree(work, ignore_errors=True)
    url = f"https://thunderstore.io/package/download/{owner}/{name}/{version}/"
    req = urllib.request.Request(url, headers={"User-Agent": "reskate-dashboard"})
    size = 0
    with urllib.request.urlopen(req, timeout=60) as r, open(archive, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            size += len(chunk)
            if size > MODS_MAX:
                raise ValueError("the download is larger than the limit (DASHBOARD_MODS_MAX_GB)")
            f.write(chunk)
    try:
        with zipfile.ZipFile(archive) as z:
            infos = z.infolist()
            for i in infos:
                parts = i.filename.replace("\\", "/").split("/")
                if i.filename.startswith("/") or ".." in parts or stat.S_ISLNK(i.external_attr >> 16):
                    raise ValueError(f"unsafe entry in the zip: {i.filename!r}")
            if sum(i.file_size for i in infos) > MODS_MAX:
                raise ValueError("the package unpacks to more than the limit (DASHBOARD_MODS_MAX_GB)")
            if "manifest.json" not in {i.filename for i in infos}:
                raise ValueError("the package has no manifest.json")
            z.extractall(work)
        with open(f"{work}/{MARKER}", "w", encoding="utf-8") as f:
            json.dump({"owner": owner, "name": name, "version": version}, f)
        st = os.stat(f"{SHARED}/packages") if os.path.isdir(f"{SHARED}/packages") else os.stat(SHARED)
        for root, dirs, files in os.walk(work):
            for entry in dirs + files:
                try:
                    os.chown(os.path.join(root, entry), st.st_uid, st.st_gid)
                except OSError:
                    pass
        os.makedirs(f"{SHARED}/packages", exist_ok=True)
        shutil.rmtree(final, ignore_errors=True)
        os.rename(work, final)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        try:
            os.remove(archive)
        except OSError:
            pass
    return final


def install_package(body):
    """Download a Thunderstore package into the shared store and link it into the running servers' Mods folder
    (what the servers' `mods add` does, which older server images do not have): {server: "<n>"|"*", package, version?}."""
    server, package = str(body.get("server", "")), str(body.get("package", "")).strip()
    m = re.match(r"^([A-Za-z0-9_]+)-([A-Za-z0-9_]+)(?:-(\d+\.\d+\.\d+))?$", package)
    if not (server == "*" or server.isalnum()) or not m:
        raise ValueError("a server (or all) and a package like Owner-Name are needed")
    owner, name, version = m.group(1), m.group(2), str(body.get("version") or m.group(3) or "")
    if not re.match(r"^\d+\.\d+\.\d+$", version):
        version = next((r["version"] for r in ts_packages() if r["package"] == f"{owner}-{name}"), "")
    if not version:
        raise ValueError("the package version is unknown")
    store_package(owner, name, version)
    rows = server_details()
    targets = [n for n in rows if server == "*" or n == server]
    if not targets:
        raise ValueError("no running container for that server")
    for number in targets:
        for c in rows[number]["containers"]:
            docker_put_link(c["id"], "/data/Mods", f"{owner}-{name}", f"{MODS_STORE}/packages/{owner}-{name}-{version}")
        moderate({"server": number, "action": "console", "text": "maps reload"})
    MAPS["at"] = 0
    try:
        sync_links()                          # every server has every map; each keeps its own pool
    except (RuntimeError, OSError, ValueError, urllib.error.URLError) as e:
        say(f"dashboard: map sync after install failed: {e}")
    return {"text": f"{owner}-{name} {version} linked on server {', '.join(sorted(targets))}", "names": store_names(f"{owner}-{name}")}


LAYERS_MAX = 8_000_000
LAYERS_BUILD = "world-layers.version"   # server version the file was uploaded for (the file itself does not name its game build)


def upload_layers(server, text):
    """world-layers.json into /data of the running container(s) of one server (or all): {server: "<n>"|"*", content}."""
    if not (server == "*" or server.isalnum()):
        raise ValueError("a server (or all) is needed")
    if len(text) > LAYERS_MAX:
        raise ValueError("the file is too large")
    try:
        parsed = json.loads(text)
    except ValueError:
        raise ValueError("that is not a JSON file") from None
    if not isinstance(parsed, (dict, list)):
        raise ValueError("that is not a world-layers.json")
    rows = server_details()
    targets = [n for n in rows if server == "*" or n == server]
    if not targets:
        raise ValueError("no running container for that server")
    for number in targets:
        for c in rows[number]["containers"]:
            docker_put_file(c["id"], "/data", "world-layers.json", text.encode())
            docker_put_file(c["id"], "/data", LAYERS_BUILD, (c.get("version") or "").encode())
    MAPS["at"] = 0
    return f"world-layers.json written for server {', '.join(sorted(targets))} ({len(text) // 1024} KB)"


POOLS_FILE = f"{SHARED}/pools.json"
POOLS_LOCK = threading.Lock()
ALL_MAPS = "Alle Maps"   # the built-in pool: no pool set on the server, every map allowed


PKG_DIR = re.compile(r"^([A-Za-z0-9_]+)-([A-Za-z0-9_]+)-(\d+)\.(\d+)\.(\d+)$")
RELOAD_PENDING = set()


def store_maps():
    """{"Owner-Name": {"folder", "names"}} of the newest version of every map package in the shared store
    (names = the map names it declares, lower case); packages without maps are left out."""
    best = {}
    try:
        folders = os.listdir(f"{MODS_STORE}/packages")
    except OSError:
        return {}
    for folder in folders:
        m = PKG_DIR.match(folder)
        if not m:
            continue
        key, version = f"{m.group(1)}-{m.group(2)}", tuple(int(x) for x in m.groups()[2:])
        if key in best and best[key][0] >= version:
            continue
        try:
            with open(f"{MODS_STORE}/packages/{folder}/reskate-levels.json", encoding="utf-8-sig") as f:
                names = {level["displayName"].lower() for level in json.load(f).get("levels", [])
                         if isinstance(level, dict) and level.get("displayName")}
        except (OSError, ValueError, AttributeError):
            continue
        if names:
            best[key] = (version, {"folder": folder, "names": names})
    return {k: v[1] for k, v in best.items()}


def sync_plan(store, linked):
    """{server: [package]} to link so that every server has every map package of the store. `linked` is
    {server: ["Owner-Name-1.0.0", ...]} as found in each server's Mods folder. A package is skipped on a server where one
    of its map names is already provided by another package (the server refuses a name that two packages share);
    between two clashing packages the one more servers have linked wins."""
    spread = {}
    for mods in linked.values():
        for pkg in {PKG_DIR.sub(r"\1-\2", x) for x in mods}:
            spread[pkg] = spread.get(pkg, 0) + 1
    plan = {}
    for number, mods in linked.items():
        have = {PKG_DIR.sub(r"\1-\2", x) for x in mods}
        taken = set()
        for pkg in have:
            taken |= store.get(pkg, {}).get("names", set())
        todo = []
        for pkg in sorted((k for k in store if k not in have), key=lambda k: (-spread.get(k, 0), k.lower())):
            if store[pkg]["names"] & taken:
                continue
            taken |= store[pkg]["names"]
            todo.append(pkg)
        if todo:
            plan[number] = todo
    return plan


def sync_links(numbers=None, apply=True):
    """Link every map package of the store into the servers' Mods folders (the servers share the store but only load what
    is linked), then `maps reload`: right away on a server within the player rule, later (the next sync) on a busy one."""
    rows = server_details()
    store = store_maps()
    items = server_maps(True)
    linked = {n: list(items.get(n, {}).get("mods", [])) for n in rows if (numbers is None or n in numbers) and items.get(n, {}).get("live")}
    plan = sync_plan(store, linked)
    if apply:
        status = {r.get("server"): r for r in bridge("/v1/servers")}
        for number in sorted(set(plan) | RELOAD_PENDING):
            if number not in rows:
                continue
            for pkg in plan.get(number, []):
                for c in rows[number]["containers"]:
                    docker_put_link(c["id"], "/data/Mods", pkg, f"{MODS_STORE}/packages/{store[pkg]['folder']}")
            if plan.get(number):
                RELOAD_PENDING.add(number)
            row = status.get(int(number) if number.isdigit() else number) or status.get(number) or {}
            if restart_rule(row.get("players")) == "restart":
                moderate({"server": number, "action": "console", "text": "maps reload"})
                RELOAD_PENDING.discard(number)
        MAPS["at"] = 0
    return {"plan": plan, "pending": sorted(RELOAD_PENDING)}


def library():
    """Every map any server has installed (all servers load the same shared store), de-duplicated case-insensitively."""
    names = {}
    for item in server_maps().values():
        for n in item.get("available", []):
            names.setdefault(n.lower(), n)
    return sorted(names.values(), key=str.lower)


def load_pools():
    try:
        with open(POOLS_FILE) as f:
            data = json.load(f)
        return ({str(k): [str(x) for x in v] for k, v in data["pools"].items() if k != ALL_MAPS},
                {str(k): str(v) for k, v in data["assign"].items()})
    except (OSError, ValueError, KeyError, AttributeError, TypeError):
        return None


def save_pools(pools, assign):
    tmp = POOLS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"pools": pools, "assign": assign}, f, indent=1, ensure_ascii=False)
    os.replace(tmp, POOLS_FILE)


def seed_pools():
    """First start: one named pool per distinct pool the servers run now (named after the server's role)."""
    pools, assign = {}, {}
    details = server_details()
    for number, item in sorted(server_maps(True).items(), key=lambda kv: (len(kv[0]), kv[0])):
        if not item["pool"]:
            assign[number] = ALL_MAPS
            continue
        key = sorted(x.lower() for x in item["pool"])
        for name, maps in pools.items():
            if sorted(x.lower() for x in maps) == key:
                assign[number] = name
                break
        else:
            role = (details.get(number, {}).get("name") or "").rsplit(" - ", 1)[-1].strip() or f"Server {number}"
            name = role if role not in pools and role != ALL_MAPS else f"{role} {number}"
            pools[name], assign[number] = list(item["pool"]), name
    return pools, assign


def pools_state():
    state = load_pools()
    if state is None:
        state = seed_pools()
        save_pools(*state)
    pools, assign = state
    for number in server_maps():
        assign.setdefault(number, ALL_MAPS)
    return pools, assign


def pools_snapshot(pools, assign):
    lib = library()
    known = {n.lower() for n in lib}
    extra = [n for maps in pools.values() for n in maps if n.lower() not in known]
    drift = {}
    for number, item in server_maps().items():
        if item.get("live"):
            want = {x.lower() for x in pools.get(assign.get(number, ALL_MAPS), [])}
            drift[number] = want != {x.lower() for x in item["pool"]}
    servers = sorted(server_maps(), key=lambda n: (len(n), n))
    return {"pools": pools, "assign": assign, "library": sorted(set(lib) | set(extra), key=str.lower), "drift": drift,
            "servers": servers}


def apply_pool(number, target, lib):
    """Bring the running server's map pool to `target` (empty list = every map) with console commands; returns how many."""
    live = server_maps(True, number).get(number)
    if not live or not live.get("live"):
        raise RuntimeError(f"server {number}: settings not readable")
    have, want = {x.lower(): x for x in live["pool"]}, {x.lower(): x for x in target}
    if not want or {x.lower() for x in lib} <= set(want):
        commands = ["map-pool clear"] if have else []
    elif not have:
        commands = [f"map-pool remove {n}" for n in lib if n.lower() not in want]   # the first remove turns "all" into all-but-this
    else:
        commands = [f"map-pool add {want[k]}" for k in want if k not in have] + [f"map-pool remove {have[k]}" for k in have if k not in want]
    for command in commands:
        moderate({"server": number, "action": "console", "text": command})
    return len(commands)


def pools_action(body):
    """{action: create|assign|set|delete|reapply|sync, server, name, map, on}; saves first, links any map package the servers
    concerned are missing, then applies to them. sync links every map package of the store on every server."""
    action, server = str(body.get("action", "")), str(body.get("server", ""))
    with POOLS_LOCK:
        pools, assign = pools_state()
        lib = library()
        targets = []
        if action in ("create", "assign", "delete", "reapply") and not server.isalnum():
            raise ValueError("a server is needed")
        if action == "create":
            name = " ".join(str(body.get("name", "")).split())[:40]
            if not name:
                raise ValueError("the pool needs a name")
            if name.lower() == ALL_MAPS.lower() or name.lower() in {k.lower() for k in pools}:
                raise ValueError("a pool with that name exists")
            pools[name] = list(pools.get(assign.get(server, ALL_MAPS)) or lib)
            assign[server], targets = name, [server]
        elif action == "assign":
            name = str(body.get("name", ""))
            if name != ALL_MAPS and name not in pools:
                raise ValueError("unknown pool")
            assign[server], targets = name, [server]
        elif action == "set":
            name, wanted = str(body.get("name", "")), str(body.get("map", ""))
            if name not in pools or not wanted:
                raise ValueError("unknown pool or map")
            rest = [m for m in pools[name] if m.lower() != wanted.lower()]
            if body.get("on"):
                rest.append(next((m for m in lib if m.lower() == wanted.lower()), wanted))
            if not rest:
                raise ValueError("a pool needs at least one map")
            pools[name] = rest
            targets = [n for n, p in assign.items() if p == name]
        elif action == "delete":
            name = str(body.get("name", ""))
            if name not in pools:
                raise ValueError("unknown pool")
            others = [n for n, p in assign.items() if p == name and n != server]
            if others:
                raise ValueError(f"the pool is still used by server {', '.join(others)}")
            del pools[name]
            assign[server], targets = ALL_MAPS, [server]
        elif action == "reapply":
            targets = [server]
        elif action == "sync":
            pass
        else:
            raise ValueError(f"unknown action {action!r}")
        save_pools(pools, assign)
        if action == "sync":
            sync_links()
        elif targets:
            sync_links(set(targets))          # a pool can name maps the server has not linked yet
        for number in targets:
            if number in server_maps():
                apply_pool(number, pools.get(assign.get(number, ALL_MAPS), []), lib)
        MAPS["at"] = 0
        return pools_snapshot(pools, assign)


ANN_FILE = f"{SHARED}/ansagen.json"
ANN_LOCK = threading.Lock()
ANN_TEXT = 220


def live_announcements(number):
    """(messages, interval minutes) of the running server's settings file, None when it cannot be read."""
    for c in server_details().get(number, {}).get("containers", []):
        try:
            with tar_of(c["id"], "/data/ReSkateServer.json") as t:
                a = json.load(t.extractfile(t.getmember("ReSkateServer.json"))).get("announcements", {})
            return [m for m in a.get("messages", []) if isinstance(m, str)], int(a.get("interval_minutes") or 0)
        except (RuntimeError, OSError, ValueError, KeyError, TypeError, tarfile.TarError):
            continue
    return None


def ann_clean(item):
    text = " ".join(str(item.get("text", "")).split())[:ANN_TEXT]
    if not text:
        raise ValueError("the announcement is empty")
    every, offset = int(item.get("every") or 0), int(item.get("offset") or 0)
    if not (0 <= every <= 1440 and 0 <= offset <= 1440):
        raise ValueError("the interval and the offset are 0 to 1440 minutes")
    return {"text": text, "on": bool(item.get("on", True)), "every": every, "offset": offset}


def ann_load():
    """Stored {global: [...], servers: {n: {messages: [...], interval}}}; the first time seeded from what the servers run now
    (lines every running server has become global ones)."""
    try:
        with open(ANN_FILE) as f:
            state = json.load(f)
        state["global"], state["servers"]
    except (OSError, ValueError, KeyError, TypeError):
        state = {"global": [], "servers": {}}
    live = {n: live_announcements(n) for n in server_details()}
    fresh = {n: v for n, v in live.items() if v is not None and n not in state["servers"]}
    if fresh:
        # the test server must not decide what counts as "on every server"
        probes = [v for n, v in live.items() if v is not None and "test" not in (server_details()[n].get("name") or "").lower().rsplit(" - ", 1)[-1]]
        everywhere = []
        if not state["servers"] and len(fresh) == len([v for v in live.values() if v is not None]) and probes:
            everywhere = [m for m in probes[0][0] if all(m in v[0] for v in probes)]
        if everywhere:
            state["global"] = [{"text": m, "on": True} for m in dict.fromkeys(everywhere)]
        for n, (messages, interval) in fresh.items():
            state["servers"][n] = {"messages": [{"text": m, "on": True} for m in messages if m not in everywhere], "interval": interval}
        ann_save(state)
    return state


def ann_save(state):
    tmp = ANN_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1, ensure_ascii=False)
    os.replace(tmp, ANN_FILE)


def ann_schedule(items):
    """[(text, every minutes, offset minutes)] -> {messages, interval, deviation}. The server has one interval and runs its
    list round-robin, so different intervals become a longer list: over the cycle (the least common multiple of the
    intervals) every announcement gets as many slots as it is due, spread evenly, each in a slot of its own so nothing
    runs at the same time; `offset` picks where in the cycle the first one goes. `deviation` is how far (fraction) the
    whole minutes of the slot length push an announcement off its interval."""
    if not items:
        return {"messages": [], "interval": 0, "deviation": 0}
    everys = {e for _, e, _ in items}
    if len(everys) == 1:   # one common interval: a plain rotation, the order is the stagger
        return {"messages": [t for t, _, _ in items], "interval": everys.pop(), "deviation": 0}
    cycle = reduce(lambda x, y: x * y // gcd(x, y), everys)
    counts = [cycle // e for _, e, _ in items]
    total = sum(counts)
    if cycle > 1440 or total > 80:
        raise ValueError("the intervals do not fit into each other; use intervals that are multiples of each other (10, 20, 30, 60)")
    slot = max(1, min((cycle // total, cycle // total + 1), key=lambda m: abs(m * total - cycle)))
    slots = [None] * total
    for i in sorted(range(len(items)), key=lambda i: -counts[i]):
        c, best = counts[i], None
        manual = items[i][2]
        for start in ([manual / slot] if manual else range(max(1, round(total / c)))):
            taken, cost = [], 0
            for j in range(c):
                target = round(start + j * total / c) % total
                free = [x for x in range(total) if slots[x] is None and x not in taken]
                pos = min(free, key=lambda x: min((x - target) % total, (target - x) % total))
                cost += min((pos - target) % total, (target - pos) % total)
                taken.append(pos)
            if best is None or cost < best[0]:
                best = (cost, taken)
        for pos in best[1]:
            slots[pos] = i
    real = slot * total
    deviation = max(abs(real / counts[i] / items[i][1] - 1) for i in range(len(items)))
    return {"messages": [items[i][0] for i in slots], "interval": slot, "deviation": round(deviation, 3)}


def ann_plan(state, number):
    """What the server should run: {messages, interval (None = leave it), deviation, error}."""
    mine = state["servers"].get(number, {})
    items = [m for m in state["global"] if m["on"]] + [m for m in mine.get("messages", []) if m["on"]]
    base = int(mine.get("interval") or 0)
    try:
        plan = ann_schedule([(m["text"], m.get("every") or base or 10, m.get("offset") or 0) for m in items])
        plan["error"] = ""
        if not base and not any(m.get("every") for m in items):
            plan["interval"] = None
    except ValueError as e:
        plan = {"messages": [m["text"] for m in items], "interval": base or None, "deviation": 0, "error": str(e)}
    return plan


def ann_snapshot(state):
    details = server_details()
    return {"global": state["global"], "servers": state["servers"],
            "live": {n: ({"messages": v[0], "interval": v[1]} if (v := live_announcements(n)) is not None else None) for n in details},
            "roles": {n: (r.get("name") or "").rsplit(" - ", 1)[-1].strip() for n, r in details.items()},
            "plans": {n: ann_plan(state, n) for n in details}}


def ann_apply(state, number):
    """Make the running server's announcement list and interval match the stored ones (console commands only)."""
    live = live_announcements(number)
    if live is None:
        raise RuntimeError(f"server {number}: settings not readable")
    plan = ann_plan(state, number)
    want, interval = plan["messages"], plan["interval"]
    commands = []
    if live[0] != want:
        commands = ["announcements clear"] + [f"announcements add {t}" for t in want]
    if interval and interval != live[1]:
        commands.append(f"announcements interval {interval}")
    for command in commands:
        moderate({"server": number, "action": "console", "text": command})
    return len(commands)


def ann_action(body):
    """{action: add|edit|delete|move|interval|reapply, scope: "global"|"<server>", index, text, on, dir, minutes, server}."""
    action, scope = str(body.get("action", "")), str(body.get("scope", ""))
    with ANN_LOCK:
        state = ann_load()
        servers = list(server_details())
        if action == "interval":
            target = str(body.get("server", ""))
            minutes = int(body.get("minutes"))
            if not 1 <= minutes <= 1440:
                raise ValueError("the interval is 1 to 1440 minutes")
            hit = servers if target == "*" else [target]
            for n in hit:
                if n not in state["servers"]:
                    raise ValueError("unknown server")
                state["servers"][n]["interval"] = minutes
        elif action == "reapply":
            hit = servers if scope in ("", "*", "global") else [scope]
        else:
            if scope == "global":
                items, hit = state["global"], servers
            elif scope in state["servers"]:
                items, hit = state["servers"][scope]["messages"], [scope]
            else:
                raise ValueError("unknown scope")
            if action == "add":
                items.append(ann_clean(body))
            else:
                i = int(body.get("index"))
                if not 0 <= i < len(items):
                    raise ValueError("no such announcement")
                if action == "edit":
                    items[i] = ann_clean({k: body.get(k, items[i].get(k)) for k in ("text", "on", "every", "offset")})
                elif action == "delete":
                    del items[i]
                elif action == "move":
                    j = i + (-1 if body.get("dir") == "up" else 1)
                    if 0 <= j < len(items):
                        items[i], items[j] = items[j], items[i]
                else:
                    raise ValueError(f"unknown action {action!r}")
        ann_save(state)
        errors = []
        for n in hit:
            try:
                ann_apply(state, n)
            except (RuntimeError, OSError, urllib.error.URLError) as e:
                errors.append(str(e))
        out = ann_snapshot(state)
        out["errors"] = errors
        return out


LB_FILES = {"unranked": "leaderboard-unranked.db", "ranked": "leaderboard.db"}
LB_KINDS = {"general": 0, "year": 1, "month": 2, "week": 3, "event": 4}


def db_rows(path, sql, args=()):
    """Rows of a read-only query on a SQLite file; [] when the file is missing or unreadable."""
    if not os.path.exists(path):
        return []
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3)
        try:
            con.row_factory = sqlite3.Row
            return [dict(r) for r in con.execute(sql, args)]
        finally:
            con.close()
    except sqlite3.Error:
        return []


def lb_team_of():
    """{steam_id: {"tag", "name"}} from the bridge's teams database."""
    rows = db_rows(f"{SHARED}/teams.db", "SELECT m.steam_id, t.tag, t.name FROM members m JOIN teams t ON t.id = m.team")
    return {r["steam_id"]: {"tag": r["tag"], "name": r["name"]} for r in rows}


def lb_snapshot(league, board="general", limit=100):
    """The leaderboard of one league (unranked = servers 1-5, ranked = server 6): the boards, the top
    players of `board`, the team standing on it and the latest awards. Read-only."""
    if league not in LB_FILES:
        raise ValueError("unknown league")
    path = f"{SHARED}/{LB_FILES[league]}"
    limit = max(1, min(int(limit), 500))
    boards = db_rows(path, "SELECT b.id, b.name, b.kind, b.created_at, b.closed_at, "
                           "(SELECT COUNT(*) FROM scores s WHERE s.board = b.id AND s.points > 0) AS players FROM boards b")
    for b in boards:
        b["base"], _, b["mode"] = b["id"].partition(":")
        if b["kind"] == "mode":
            b["kind"] = b["base"].split("-")[0]
    boards.sort(key=lambda b: (LB_KINDS.get(b["kind"], 9), -(b["created_at"] or 0), b["mode"]))
    if board not in {b["id"] for b in boards}:
        board = "general"
    teams = lb_team_of()
    rows = db_rows(path, "SELECT p.name, s.steam_id, s.points, s.wins, s.played FROM scores s JOIN players p USING (steam_id) "
                         "WHERE s.board = ? AND s.points > 0 ORDER BY s.points DESC, s.wins DESC, p.name COLLATE NOCASE",
                   (board,))
    standing = {}
    for r in rows:
        team = teams.get(r["steam_id"])
        r["tag"] = team["tag"] if team else ""
        if team:
            t = standing.setdefault(team["tag"], {"tag": team["tag"], "name": team["name"], "points": 0, "wins": 0, "members": 0})
            t["points"] += r["points"]
            t["wins"] += r["wins"]
            t["members"] += 1
    for i, r in enumerate(rows):
        r["rank"] = i + 1
    # one award is written to every board it counts on (all time, year, month, week, each also per game): show it once
    recent = db_rows(path, "SELECT l.at, p.name, l.points, l.reason, l.server, COUNT(*) AS boards FROM ledger l "
                           "LEFT JOIN players p USING (steam_id) GROUP BY l.at, l.steam_id, l.points, l.reason, l.server "
                           "ORDER BY MAX(l.id) DESC LIMIT 25")
    stats = (db_rows(path, "SELECT (SELECT COUNT(*) FROM players) AS players, (SELECT COUNT(*) FROM (SELECT 1 FROM ledger GROUP BY at, steam_id, points, reason, server)) AS awards, "
                           "(SELECT MAX(at) FROM ledger) AS last") or [{}])[0]
    return {"league": league, "board": board, "boards": boards, "total": len(rows), "rows": rows[:limit],
            "teams": sorted(standing.values(), key=lambda t: -t["points"]), "recent": recent, "stats": stats,
            "exists": os.path.exists(path)}


def teams_snapshot():
    """The teams (Discord bot / bridge keep them in /shared/teams.db): profile, members with their All Time points on both
    leagues' boards, open applications. Read-only here; changes go through the bridge (team_action)."""
    path = f"{SHARED}/teams.db"
    teams = db_rows(path, "SELECT id, name, tag, motto, description, logo, league, category, open, created FROM teams")
    members = db_rows(path, "SELECT m.steam_id, m.team, m.role, m.joined, COALESCE(k.name, m.steam_id) AS name FROM members m "
                            "LEFT JOIN known k USING (steam_id) ORDER BY m.role = 'owner' DESC, m.role = 'leader' DESC, m.joined")
    invites = {r["team"]: r["n"] for r in db_rows(path, "SELECT team, COUNT(*) AS n FROM invites GROUP BY team")}
    points = {league: {r["steam_id"]: r["points"] for r in db_rows(f"{SHARED}/{file}", "SELECT steam_id, points FROM scores WHERE board = 'general'")}
              for league, file in LB_FILES.items()}
    for t in teams:
        t["tag"], t["open"] = t["tag"].upper(), bool(t["open"])
        t["members"] = [m for m in members if m["team"] == t["id"]]
        t["invites"] = invites.get(t["id"], 0)
        t["points"] = {}
        for league, book in points.items():
            for m in t["members"]:       # the unranked boards know players by name when there is no Steam ID
                m.setdefault("points", {})[league] = book.get(m["steam_id"], book.get("name:" + m["name"], 0))
            t["points"][league] = sum(m["points"][league] for m in t["members"])
    teams.sort(key=lambda t: (-len(t["members"]), t["name"].lower()))
    apps = db_rows(path, "SELECT id, applicant, name, tag, status, created, decided_by, decided_at FROM applications ORDER BY id DESC LIMIT 60")
    return {"exists": os.path.exists(path) and bool(db_rows(path, "SELECT name FROM sqlite_master WHERE name = 'teams'")), "teams": teams,
            "pending": [a for a in reversed(apps) if a["status"] == "pending"], "recent": [a for a in apps if a["status"] != "pending"][:8]}


TEAM_KEYS = ("tag", "open", "motto", "description", "logo", "league", "category")


def team_action(body):
    """{action: decide|set|kick|leader|owner|disband, ...}: an admin's side of /team. decide {id, accept} answers an
    application. The others run the team command as the team's owner (the bridge delivers its in-game notices):
    set {team, key, value}, kick|leader|owner {team, player (Steam ID of a member)}, disband {team}."""
    action = str(body.get("action", ""))
    if action == "decide":
        result = bridge("/v1/teams/decide", {"id": int(body.get("id")), "accept": bool(body.get("accept")), "by": "dashboard"})
        if not result.get("ok"):
            raise ValueError(result.get("text") or "not possible")
        return result.get("text") or "done"
    snap = teams_snapshot()
    tag = str(body.get("team", "")).upper()
    team = next((t for t in snap["teams"] if t["tag"] == tag), None)
    owner = next((m for m in (team or {}).get("members", []) if m["role"] == "owner"), None)
    if not team or not owner:
        raise ValueError("unknown team")
    who, args = {"steam_id": owner["steam_id"], "name": owner["name"]}, {}
    if action == "set":
        key, value = str(body.get("key", "")), str(body.get("value", "")).strip()[:300]
        if key not in TEAM_KEYS:
            raise ValueError("unknown setting")
        verb, args = "set", {"key": key, "value": value}
        if key == "open":
            args["value"] = "yes" if body.get("value") in (True, "yes", "true") else "no"
    elif action in ("kick", "leader", "owner"):
        player = str(body.get("player", ""))
        if not any(m["steam_id"] == player for m in team["members"]):
            raise ValueError("that player is not in the team")
        verb, args = action, {"player": player}
    elif action == "disband":
        verb, args = "disband", {"confirm": "yes"}
    else:
        raise ValueError(f"unknown action {action!r}")
    result = bridge("/v1/teams/act", {"who": who, "verb": verb, "args": args})
    if not result.get("ok"):
        raise ValueError((result.get("lines") or ["not possible"])[0])
    return " ".join(result.get("lines") or ["done"])


STEAM_ID = re.compile(r"\d{17}")


def player_stats(league, steam_id, name):
    """One player's standing on a league's boards: per game mode ("" = all modes) and period (all time, the latest
    year, month and week): points, wins, games played and place. Unranked boards know players by name."""
    path = f"{SHARED}/{LB_FILES[league]}"
    out = {"exists": os.path.exists(path), "modes": [], "periods": [], "cells": {}}
    boards = db_rows(path, "SELECT id, name, kind, created_at FROM boards")
    keys = ([steam_id] if steam_id else []) + (["name:" + name] if name else [])
    mine = {r["board"]: r for r in db_rows(
        path, f"SELECT board, points, wins, played FROM scores WHERE steam_id IN ({','.join('?' * len(keys))}) "
              "ORDER BY points", keys)}
    latest = {}
    for b in boards:
        base, _, mode = b["id"].partition(":")
        kind = base.split("-")[0]
        if kind in ("general", "year", "month", "week") and (kind not in latest or base > latest[kind]):
            latest[kind] = base
    out["periods"] = [{"kind": k, "base": latest[k]} for k in ("general", "year", "month", "week") if k in latest]
    names = {b["id"]: b["name"] for b in boards}
    for period in out["periods"]:
        period["name"] = names.get(period["base"], period["base"])
    modes = sorted({b["id"].partition(":")[2] for b in boards if ":" in b["id"]})
    out["modes"] = [{"mode": "", "name": "Alle Modi"}] + [
        {"mode": m, "name": (names.get(f"general:{m}") or m).replace("All Time - ", "")} for m in modes]
    for m in out["modes"]:
        for period in out["periods"]:
            bid = period["base"] + (":" + m["mode"] if m["mode"] else "")
            row = mine.get(bid)
            if not row or not row["points"]:
                continue
            ahead = db_rows(path, "SELECT COUNT(*) AS n FROM scores WHERE board = ? AND points > ?", (bid, row["points"]))
            of = db_rows(path, "SELECT COUNT(*) AS n FROM scores WHERE board = ? AND points > 0", (bid,))
            out["cells"].setdefault(m["mode"], {})[period["kind"]] = {
                "points": row["points"], "wins": row["wins"], "played": row["played"],
                "rank": (ahead[0]["n"] if ahead else 0) + 1, "of": of[0]["n"] if of else 0}
    return out


def player_profile(steam_id, name=""):
    """Moderator view of one player: Steam profile link, team, all teams to pick from and stats per league."""
    steam_id = str(steam_id or "")
    name = str(name or "")[:64]
    if not steam_id and name:                  # a player the unranked boards know by name only
        return {"steam_id": "", "name": name, "steam": "", "discord": "", "team": None, "teams": [],
                "stats": {league: player_stats(league, "", name) for league in LB_FILES}}
    if not STEAM_ID.fullmatch(steam_id):
        raise ValueError("a SteamID64 is needed")
    teams = db_rows(f"{SHARED}/teams.db", "SELECT t.id, t.tag, t.name, (SELECT COUNT(*) FROM members m WHERE m.team = t.id) AS members "
                                         "FROM teams t ORDER BY t.name COLLATE NOCASE")
    mine = db_rows(f"{SHARED}/teams.db", "SELECT t.tag, t.name, m.role FROM members m JOIN teams t ON t.id = m.team WHERE m.steam_id = ?",
                   (steam_id,))
    known = db_rows(f"{SHARED}/teams.db", "SELECT name FROM known WHERE steam_id = ?", (steam_id,))
    name = name or (known[0]["name"] if known else "")
    link = db_rows(f"{SHARED}/teams.db", "SELECT discord_id FROM links WHERE steam_id = ?", (steam_id,))
    discord = link[0]["discord_id"] if link and re.fullmatch(r"\d{5,25}", str(link[0]["discord_id"])) else ""
    return {"steam_id": steam_id, "name": name, "steam": f"https://steamcommunity.com/profiles/{steam_id}",
            "discord": f"https://discord.com/users/{discord}" if discord else "",
            "team": ({"tag": mine[0]["tag"].upper(), "name": mine[0]["name"], "role": mine[0]["role"]} if mine else None),
            "teams": [{"tag": t["tag"].upper(), "name": t["name"], "members": t["members"]} for t in teams],
            "stats": {league: player_stats(league, steam_id, name) for league in LB_FILES}}


STEAM_CACHE = {}


def steam_info(steam_id):
    """What Steam shows publicly about an account (the profile's XML, no API key): name, avatar, VAC and trade ban,
    limited account, privacy, member since. Cached for ten minutes; {"error"} when Steam can not be reached."""
    steam_id = str(steam_id)
    if not STEAM_ID.fullmatch(steam_id):
        raise ValueError("a SteamID64 is needed")
    hit = STEAM_CACHE.get(steam_id)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    try:
        req = urllib.request.Request(f"https://steamcommunity.com/profiles/{steam_id}?xml=1", headers={"User-Agent": "reskate-dashboard"})
        with urllib.request.urlopen(req, timeout=8) as res:
            xml = res.read(200000).decode("utf-8", "replace")
    except OSError as e:
        return {"error": f"Steam is not reachable ({e.__class__.__name__})"}

    def text(tag):          # the profile is flat XML: <tag>value</tag> or <tag><![CDATA[value]]></tag>
        m = re.search(rf"<{tag}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{tag}>", xml, re.S)
        return m.group(1).strip() if m else ""
    if not text("steamID64"):
        return {"error": text("error") or "no such Steam profile"}
    avatar = text("avatarMedium")
    info = {"name": text("steamID"), "avatar": avatar if re.fullmatch(r"https://[\w.-]+\.steamstatic\.com/[\w./-]+", avatar) else "", "privacy": text("privacyState"), "vac": text("vacBanned") == "1",
            "trade_ban": text("tradeBanState"), "limited": text("isLimitedAccount") == "1", "since": text("memberSince")}
    STEAM_CACHE[steam_id] = (time.time(), info)
    return info


def players_known():
    """Everybody the system knows, online or not: names seen on a server (teams.db), then the boards' players that have
    a Steam ID. [{steam_id, name, seen, team}], newest first."""
    teams = lb_team_of()
    found = {}
    for r in db_rows(f"{SHARED}/teams.db", "SELECT steam_id, name, seen FROM known ORDER BY seen DESC"):
        if STEAM_ID.fullmatch(r["steam_id"]):
            found[r["steam_id"]] = {"steam_id": r["steam_id"], "name": r["name"], "seen": r["seen"] or 0}
    for file in LB_FILES.values():
        for r in db_rows(f"{SHARED}/{file}", "SELECT steam_id, name FROM players"):
            if STEAM_ID.fullmatch(r["steam_id"]):
                found.setdefault(r["steam_id"], {"steam_id": r["steam_id"], "name": r["name"], "seen": 0})
    have = {r["name"].lower() for r in found.values()}
    for r in db_rows(f"{SHARED}/{LB_FILES['unranked']}", "SELECT steam_id, name FROM players WHERE steam_id LIKE 'name:%'"):
        if r["name"].lower() not in have:      # no Steam ID on file: shown by name, stats only
            have.add(r["name"].lower())
            found["name:" + r["name"]] = {"steam_id": "", "name": r["name"], "seen": 0}
    for row in found.values():
        row["team"] = (teams.get(row["steam_id"]) or {}).get("tag", "").upper()
    return sorted(found.values(), key=lambda r: (-r["seen"], r["name"].lower()))


def player_team(body):
    """{steam_id, name, team}: put a player into the team with that tag ("" = out of any team). The teams module has no
    admin verb, so it goes the way a player's own commands do: the team's owner invites, the player joins."""
    profile = player_profile(body.get("steam_id"), body.get("name"))
    sid, name, tag = profile["steam_id"], profile["name"], str(body.get("team", "")).strip().upper()
    who = {"steam_id": sid, "name": name or sid}

    def run(actor, verb, args=None):
        result = bridge("/v1/teams/act", {"who": actor, "verb": verb, "args": args or {}})
        if not result.get("ok"):
            raise ValueError((result.get("lines") or ["not possible"])[0])

    current = profile["team"]
    if current and current["tag"] == tag:
        return "already in that team"
    if current:
        run(who, "leave")
    if not tag:
        return "out of the team"
    if tag not in {t["tag"] for t in profile["teams"]}:
        raise ValueError("unknown team")
    snap = next((t for t in teams_snapshot()["teams"] if t["tag"] == tag), None)
    owner = next((m for m in (snap or {}).get("members", []) if m["role"] == "owner"), None)
    if not owner:
        raise ValueError("that team has no owner")
    run({"steam_id": owner["steam_id"], "name": owner["name"]}, "invite", {"player": sid})
    run(who, "join", {"team": tag})
    return f"in team {tag}"


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
        try:                                        # what the server has now (the dashboard changes it live), not at start
            word = str((live_config(c["Id"]).get("server") or {}).get("password") or "")
        except (RuntimeError, OSError, KeyError, ValueError, tarfile.TarError):
            pass
        row["password"] = word if word.lower() not in ("", "off") else row["password"]
        for k, v in env.items():
            if k not in SKIP and not any(h in k for h in HIDE):
                row["env"][k] = v[:200]
    DETAILS.update(at=time.time(), rows=rows)
    return rows


def restart_server(body):
    """{server, slot?, force?, players?}: restart that server's container through Docker. Allowed up to CUTOFF players;
    with more (or an unknown count) only with force=true and `players` equal to the count right now, so a button
    pressed on a stale page cannot kick a crowd that arrived in the meantime."""
    number, slot = str(body.get("server", "")), str(body.get("slot", "") or "")
    DETAILS["at"] = 0
    known = server_details().get(number)
    if not known:
        raise ValueError("unknown server or not running")
    row = next((r for r in bridge("/v1/servers") if str(r.get("server")) == number), {})
    if slot:
        mine = [c for c in known["containers"] if c["slot"] == slot]
    else:
        mine = list(known["containers"])
    if not mine:
        raise ValueError("that slot is not running")
    if len(mine) > 1:
        raise ValueError("this server runs in several slots; say which one")
    players = ((row.get("slots") or {}).get(mine[0]["slot"]) or row).get("players")
    if restart_rule(players) != "restart":
        if not body.get("force"):
            raise ValueError(f"{players if players is not None else 'unknown number of'} players are online; "
                             f"restarts are only allowed up to {CUTOFF} (or force it)")
        if body.get("players") != players:
            raise ValueError(f"the player count changed to {players if players is not None else 'unknown'}; confirm again")
    docker_restart(mine[0]["id"])
    DETAILS["at"] = 0
    return {"ok": True, "container": mine[0]["name"], "players": players, "forced": restart_rule(players) != "restart"}


PW_FILE = f"{SHARED}/passwords.json"
PW_LOCK = threading.Lock()
RANKED_ON = ("tuning on", "score-check kick", "speed-check kick")
RANKED_OFF = ("score-check warn", "speed-check warn")
CHECKS = {"score": "score-check", "speed": "speed-check", "board": "board-check"}


def pw_memory(number, word=None):
    """The password a server had before it was unlocked (so "lock" can bring it back); with `word`, remember it."""
    with PW_LOCK:
        try:
            with open(PW_FILE, encoding="utf-8") as f:
                book = json.load(f)
        except (OSError, ValueError):
            book = {}
        if word is None:
            return str(book.get(number) or "")
        book[number] = word
        fd = os.open(PW_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(book, f)
        return word


MARK = "# set by the dashboard"
MANAGED = {"SERVER_PASSWORD": True, "ENFORCE_TUNING": False, "SCORE_CHECK": False, "SPEED_CHECK": False, "BOARD_CHECK": False,
           "LEADERBOARD_LEAGUE": False}      # the settings keys the dashboard writes (True = a secret)


def env_path(number, secret):
    return f"{SHARED}/settings/{'secrets/' if secret else ''}server-{number}.env"


def env_quote(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def env_read(number):
    """{KEY: value} of the dashboard's keys in the server's settings files (the password is left as 'set' / 'off')."""
    out = {}
    for secret in (False, True):
        try:
            with open(env_path(number, secret), encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        for raw in lines:
            key, sep, value = raw.strip().partition("=")
            if sep and key.strip() in MANAGED:
                value = value.strip().strip('"\'')
                out[key.strip()] = ("off" if value.lower() in ("off", "none", "") else "set") if key.strip() == "SERVER_PASSWORD" else value
    return out


def env_write(number, changes):
    """Put {KEY: value, KEY: None (drop it)} into the server's settings files (the secrets one for the password); the
    server applies them when it starts, before anything from its container."""
    top = f"{SHARED}/settings"
    st = os.stat(SHARED)
    for secret in (False, True):
        wanted = {k: v for k, v in changes.items() if MANAGED[k] == secret}
        path = env_path(number, secret)
        if not wanted or (not os.path.exists(path) and all(v is None for v in wanted.values())):
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError:
            lines = []
        left, done = [], set()
        for raw in lines:
            key = raw.strip().partition("=")[0].strip()
            if key in wanted and not raw.strip().startswith("#"):
                if key not in done and wanted[key] is not None:
                    left.append(f"{key}={env_quote(wanted[key]) if secret else wanted[key]}")
                done.add(key)
            else:
                left.append(raw)
        fresh = [f"{k}={env_quote(v) if secret else v}" for k, v in wanted.items() if v is not None and k not in done]
        if fresh:
            left += ([] if MARK in left else [MARK]) + fresh
        if not any(x.strip() and not x.strip().startswith("#") for x in left):
            left = [x for x in left if x != MARK]          # nothing of ours is left in it
        if not any(x.strip() for x in left):
            if os.path.exists(path):
                os.remove(path)
            continue
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if secret else 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(left) + "\n")
        for target in (top, os.path.dirname(path), tmp):
            try:
                os.chown(target, st.st_uid, st.st_gid)
            except OSError:
                pass
        os.replace(tmp, path)


def server_option(body):
    """{server, slot?, action, value?, on?, persist?}: the server options that change live through the console.
    password sets one (and remembers it), unlock clears it (remembering it), lock puts the remembered one back. ranked
    on/off sets the three checks a ranked server needs (physics forced, score and speed check on kick; off = warn);
    tuning on/off, score|speed|board with value off|warn|kick set each of them alone. With persist the same values go
    into the server's settings files, so they come back after a restart; league (ranked|unranked, only with persist)
    and forget (drops everything the dashboard saved) are only about those files and apply at the next start."""
    number, action, slot = str(body.get("server", "")), str(body.get("action", "")), str(body.get("slot") or "")
    DETAILS["at"] = 0
    known = server_details().get(number)
    mine = [c for c in (known or {}).get("containers", []) if not slot or c["slot"] == slot]
    if not mine:
        raise ValueError("unknown server or not running")
    if len(mine) > 1:
        raise ValueError("this server runs in several slots; say which one")
    cid, slot = mine[0]["id"], mine[0]["slot"]
    current = str((live_config(cid).get("server") or {}).get("password") or "")
    persist = bool(body.get("persist"))
    commands, saves = [], {}
    if action == "password":
        word = str(body.get("value", "")).strip()
        if not 1 <= len(word) <= 64 or any(ord(ch) < 32 for ch in word) or word.lower() in ("off", "none"):
            raise ValueError("a password is 1 to 64 characters (not 'off' or 'none', those clear it)")
        commands, saves = [f"password {pw_memory(number, word)}"], {"SERVER_PASSWORD": word}
    elif action == "unlock":
        if current:
            pw_memory(number, current)
        commands, saves = ["password off"], {"SERVER_PASSWORD": "off"}
    elif action == "lock":
        word = current or pw_memory(number) or known["password"]
        if not word:
            raise ValueError("no password is remembered for this server; set one first")
        commands, saves = [f"password {word}"], {"SERVER_PASSWORD": word}
    elif action == "ranked":
        on = bool(body.get("on"))
        commands = list(RANKED_ON if on else RANKED_OFF)
        saves = {"SCORE_CHECK": "kick" if on else "warn", "SPEED_CHECK": "kick" if on else "warn"}
        if on:
            saves["ENFORCE_TUNING"] = "true"
    elif action == "tuning":
        on = bool(body.get("on"))
        commands, saves = ["tuning on" if on else "tuning off"], {"ENFORCE_TUNING": "true" if on else "false"}
    elif action in CHECKS:
        value = str(body.get("value", ""))
        if value not in ("off", "warn", "kick"):
            raise ValueError("a check is off, warn or kick")
        commands, saves = [f"{CHECKS[action]} {value}"], {CHECKS[action].upper().replace("-", "_"): value}
    elif action == "league":
        value = str(body.get("value", ""))
        if value not in ("ranked", "unranked"):
            raise ValueError("the league is ranked or unranked")
        if not persist:
            raise ValueError("the league is read when the server starts; it can only be saved")
        saves = {"LEADERBOARD_LEAGUE": value}
    elif action == "forget":
        persist, saves = True, {k: None for k in MANAGED}
    else:
        raise ValueError(f"unknown action {action!r}")
    for command in commands:
        moderate({"server": number, "slot": slot, "action": "console", "text": command})
    if persist and saves:
        env_write(number, saves)
    DETAILS["at"] = 0
    return {"ok": True, "commands": len(commands), "saved": sorted(k for k, v in saves.items() if persist and v is not None)}


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


LAYERS_SEEN = {}


def layers_loaded(cid, started):
    """Did the server read world-layers.json when it started? It only does that at start, so a file uploaded later is
    ignored until the next restart ("World layers: N from world-layers.json." in the first minutes of its log)."""
    key = (cid, started)
    if key not in LAYERS_SEEN:
        try:
            begin = started[:19].replace("T", " ")
            with tar_of(cid, "/data/ReSkateServer.log") as t:
                text = t.extractfile(t.getmember("ReSkateServer.log")).read().decode("utf-8", "replace")
        except (RuntimeError, OSError, KeyError, tarfile.TarError):
            return None
        stamps = [x[1:20] for x in text.splitlines() if "from world-layers.json" in x and x.startswith("[")]
        LAYERS_SEEN[key] = bool(stamps) and stamps[-1] >= begin
    return LAYERS_SEEN[key]


def layers_fit(cid, version):
    """'ok' when world-layers.json was uploaded for this server version, 'old' when for another one, 'unknown' when
    nobody recorded it (a file copied in by hand)."""
    try:
        with tar_of(cid, "/data/" + LAYERS_BUILD) as t:
            built = t.extractfile(t.getmember(LAYERS_BUILD)).read().decode().strip()
    except (RuntimeError, OSError, KeyError, tarfile.TarError):
        return "unknown"
    return "ok" if built and built == version else "old"


def live_config(cid):
    """The server's settings file (/data/ReSkateServer.json) as it is right now."""
    with tar_of(cid, "/data/ReSkateServer.json") as t:
        return json.load(t.extractfile(t.getmember("ReSkateServer.json")))


def live_maps(cid, version="", started=""):
    """(current map, pool, installed packages, on/off flags) as the running server has them now: its saved settings file
    and the links in its Mods folder, not the environment it was started with."""
    config = live_config(cid)
    maps, votes = config.get("maps", {}), config.get("votes", {})
    pool = [x for x in maps.get("pool", []) if isinstance(x, str)]
    flags = {"vote_map": bool(votes.get("map", {}).get("enabled")), "vote_kick": bool(votes.get("kick", {}).get("enabled")),
             "vote_tod": bool(votes.get("time_of_day", {}).get("enabled")),
             "rotation": int(maps.get("rotation_minutes") or 0), "layer_sync": bool(maps.get("world_layer_sync"))}
    guard = config.get("anti_cheat") or {}
    flags.update(locked=bool((config.get("server") or {}).get("password")), enforce_tuning=guard.get("enforce_tuning") is not False,
                 score_check=str(guard.get("modified_scoring") or ""), speed_check=str(guard.get("speed_hack") or ""),
                 board_check=str(guard.get("pinned_board") or ""))
    flags["ranked"] = flags["enforce_tuning"] and flags["score_check"] == "kick" and flags["speed_check"] == "kick"
    try:
        with tar_of(cid, "/data/world-layers.json") as t:
            flags["layers_file"] = "world-layers.json" in t.getnames()
    except (RuntimeError, OSError, tarfile.TarError):
        flags["layers_file"] = False
    flags["layers_fit"] = layers_fit(cid, version) if flags["layers_file"] else "missing"
    if flags["layers_file"] and started and layers_loaded(cid, started) is False:
        flags["layers_fit"] = "unloaded"
    try:
        with tar_of(cid, "/data/Mods") as t:
            mods = sorted(m.linkname.rsplit("/", 1)[-1] for m in t.getmembers() if m.issym())
    except (RuntimeError, OSError, tarfile.TarError):
        mods = []
    return str(maps.get("map") or ""), pool, mods, flags


def server_maps(fresh=False, only=None):
    """Server number -> {map, pool, mods, live}; live=False falls back to the container's start-up environment."""
    if not fresh and time.time() - MAPS["at"] < 20:
        return MAPS["rows"]
    out = dict(MAPS["rows"]) if only else {}
    for number, row in server_details().items():
        if only and number != only:
            continue
        env = row["env"]
        split = lambda v: [x.strip() for x in v.split(",") if x.strip()]  # noqa: E731
        item = {"map": env.get("MAP", ""), "pool": split(env.get("MAP_POOL", "")), "mods": split(env.get("MODS", "")), "live": False}
        for c in row["containers"]:
            try:
                item["map"], item["pool"], item["mods"], item["flags"] = live_maps(c["id"], c.get("version", ""), c.get("started", ""))
                item["live"] = True
                break
            except (RuntimeError, OSError, ValueError, KeyError, tarfile.TarError):
                continue
        item["available"] = package_maps(item["mods"], item["pool"])
        item["saved"] = env_read(number)
        item["league"] = (env.get("LEADERBOARD_LEAGUE") or "ranked").lower()
        out[number] = item
    MAPS.update(at=time.time() if not only else MAPS["at"], rows=out)
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
        if r.get("running"):
            # who has really joined (the bridge follows join/leave lines); the status count can hold a ghost
            joined = server_players(r["server"])
            r["online"] = len(joined)
            for letter, s in r.get("slots", {}).items():
                s["online"] = sum(1 for p in joined if str(p.get("slot") or "").lower() == str(letter).lower())
    return {"cutoff": CUTOFF, "say": SAY and MOD, "mod": MOD, "tp": bool(TP), "servers": rows}


PAGE = """<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="darkreader-lock">
<title>ReSkate Dashboard</title><link rel="icon" type="image/png" href="/favicon.png"><style>
:root{color-scheme:dark;--gut:clamp(16px,3vw,48px);--bg:#1a1a1a;--fg:#e8e8e8;--card:#232323;--line:#333;--mut:#9a9a9a;--ok:#4cc38a;--warn:#f0a53c;--bad:#f97066;--acc:#8ea2ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}
header{display:flex;gap:8px 12px;flex-wrap:wrap;align-items:center;padding:12px max(var(--gut),env(safe-area-inset-right,0px)) 12px max(var(--gut),env(safe-area-inset-left,0px));border-bottom:1px solid var(--line)}header h1{font-size:17px;margin:0;flex:1 1 auto;display:flex;align-items:center;gap:12px;flex-wrap:wrap}.logo{height:32px;width:auto}.flags{display:grid;gap:2px}.drop{display:flex;align-items:center;justify-content:center;text-align:center;min-height:64px;margin:8px 0;padding:10px;border:2px dashed var(--line);border-radius:8px;color:var(--mut);font-size:13px;cursor:pointer;overflow-wrap:anywhere}.drop:hover,.drop.over{border-color:var(--acc);color:var(--fg);background:var(--line)}.annrow{display:flex;gap:8px;align-items:center;margin:6px 0;flex-wrap:wrap}.annrow>input:not(.annnum){flex:1 1 220px}.annnum{width:64px;flex:none}.annrow input[type=text],.annrow>input:not([type]){flex:1;min-width:0}.poollist{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:4px 12px;margin-top:8px}.tgl{display:flex;align-items:center;gap:10px;padding:6px 4px;border-radius:6px;cursor:pointer;min-width:0}.tgl:hover{background:var(--line)}.tgl input{position:absolute;opacity:0;width:0;height:0;min-width:0;padding:0;border:0}.tgl .sw{flex:none;width:34px;height:18px;border-radius:9px;background:var(--line);position:relative;transition:background .15s}.tgl .sw::after{content:"";position:absolute;top:2px;left:2px;width:14px;height:14px;border-radius:50%;background:var(--mut);transition:left .15s,background .15s}.tgl.on .sw{background:var(--ok)}.tgl.on .sw::after{left:18px;background:#111}.tgl input:focus-visible+.sw{outline:2px solid var(--acc)}.tgl input:disabled+.sw{opacity:.5}.tgl .tn{min-width:0;overflow-wrap:anywhere;font-size:13px}.ring{flex:none;transform:rotate(-90deg)}.ring circle{fill:none;stroke-width:2.5}.ring .rb{stroke:var(--line)}.ring .rf{stroke:var(--acc);stroke-dasharray:100;stroke-dashoffset:100;stroke-linecap:round}.ring.go .rf{animation:ringfill 5s linear forwards}.ring.busy .rf{stroke-dashoffset:0;animation:none;opacity:.6}@keyframes ringfill{to{stroke-dashoffset:0}}@media(prefers-reduced-motion:reduce){.ring.go .rf{animation:none;stroke-dashoffset:50}}
main{padding:clamp(20px,3vw,40px) max(var(--gut),env(safe-area-inset-right,0px)) clamp(32px,5vw,64px) max(var(--gut),env(safe-area-inset-left,0px));width:100%;margin:0;display:grid;grid-template-columns:minmax(0,1fr);gap:clamp(40px,5.5vw,72px)}section h2{display:flex;flex-wrap:wrap;align-items:center;gap:8px;font-size:13px;font-weight:600;color:var(--mut);text-transform:uppercase;letter-spacing:.05em;margin:0 0 clamp(12px,1.4vw,18px)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,260px),1fr));gap:clamp(10px,1.2vw,16px)}
.card{min-width:0;overflow-wrap:anywhere;background:var(--card);border:1px solid var(--line);border-radius:6px;padding:12px}.cmdstack{display:grid;gap:clamp(12px,1.6vw,24px);margin-top:clamp(10px,1.4vw,18px)}.toprow{display:grid;grid-template-columns:clamp(240px,24vw,340px) minmax(0,1fr);gap:clamp(10px,1.2vw,16px);align-items:stretch}.toprow .pic{aspect-ratio:16/10}.twocol{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:clamp(10px,1.2vw,16px);align-items:start}.col{display:grid;gap:clamp(10px,1.2vw,16px);align-content:start;min-width:0}@media(max-width:900px){.toprow,.twocol{grid-template-columns:minmax(0,1fr)}}.term{background:var(--bg);border:1px solid var(--line);border-radius:6px;margin:8px 0;font:12px/1.45 ui-monospace,monospace}.term pre{background:transparent;border:0;height:220px;max-height:none;margin:0;border-radius:6px 6px 0 0}.prompt{display:flex;gap:8px;align-items:center;padding:6px 10px;border-top:1px solid var(--line)}.prompt span{color:var(--ok);font-weight:700}.prompt input{flex:1;min-width:0;background:transparent;border:0;outline:0;color:inherit;font:inherit;padding:0}.cgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,280px),1fr));gap:clamp(10px,1.2vw,16px);margin:clamp(10px,1.4vw,18px) 0}.cgrid h3{margin:0 0 4px;font-size:14px}.crow{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:8px}.crow select{flex:1;min-width:0}button.arm{background:var(--bad);color:#111}details.adv{margin:8px 0}details.adv summary{cursor:pointer;color:var(--mut);padding:4px 0}.chips{display:flex;flex-wrap:wrap;gap:4px;margin:4px 0 8px}.chip{background:var(--line);border-radius:4px;padding:2px 8px;font-size:12px}.chip.on{background:var(--acc);color:#111}.chip.mod{font-family:ui-monospace,monospace;font-size:11px}.fit{display:inline-block;width:1.4em;height:1.4em;line-height:1.4em;text-align:center;border-radius:50%;font-weight:700;color:#fff;margin-right:.4em}.fit.ok{background:#2e9e4f}.fit.bad{background:#d23b3b}.fit.warn{background:#d49a1a}
.card.open{display:grid;grid-template-columns:clamp(200px,24vw,300px) minmax(0,1fr);column-gap:16px;align-items:start;grid-auto-rows:min-content}.card.open>.head{grid-column:1/-1}.card.open>:not(.head):not(.detail){grid-column:1}.card.open>.detail{grid-column:2;grid-row:2/12;margin-top:0;border-top:0;padding-top:0}.card.open .pic{margin-bottom:6px}.dgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:14px}.dblock h4{margin:0 0 6px;font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut)}.dblock .kv{margin:0}@media(max-width:700px){.card.open{display:block}.card.open>.detail{margin-top:10px;border-top:1px solid var(--line);padding-top:10px}}.card.open{grid-column:1/-1}.card.srv{cursor:pointer}.detail{margin-top:10px;border-top:1px solid var(--line);padding-top:10px;cursor:default}.detail .set{max-height:320px;overflow:auto;font-size:12px;margin-top:6px}.lock{margin-left:auto;font-size:13px;padding:2px 8px}.head .lock+.pill{margin-left:0}.pic{position:relative;width:100%;aspect-ratio:16/9;border-radius:4px;margin-bottom:8px;background:repeating-linear-gradient(135deg,var(--line) 0 8px,var(--card) 8px 16px);display:flex;align-items:center;justify-content:center;overflow:hidden;color:var(--mut);font-size:13px;text-align:center;padding:6px}.mapimg{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}.kv{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;margin:8px 0 0;font-size:13px}.kv dt{color:var(--mut)}.pbar{grid-column:1/-1;box-sizing:border-box;width:100%;height:6px;border-radius:3px;background:var(--line);overflow:hidden}.kv dd{font-variant-numeric:tabular-nums}.pbar span{display:block;height:100%;border-radius:3px;background:var(--ok)}.pbar .warn{background:var(--warn)}.pbar .bad{background:var(--bad)}.pbar.full{overflow:visible;animation:shake .5s ease-in-out infinite}.pbar.full span{box-shadow:0 0 6px var(--bad)}@keyframes shake{0%,100%{transform:translateX(0)}20%{transform:translateX(-2px)}40%{transform:translateX(2px)}60%{transform:translateX(-2px)}80%{transform:translateX(2px)}}@media(prefers-reduced-motion:reduce){.pbar.full{animation:none}}.kv dd{margin:0;min-width:0;overflow-wrap:anywhere}.num{background:var(--line);border-radius:4px;min-width:26px;height:26px;padding:0 6px;display:inline-flex;align-items:center;justify-content:center;font-weight:700;font-size:15px}.head h3{flex:1}.card h3{margin:0;font-size:15px;min-width:0;overflow-wrap:anywhere}.head{display:flex;justify-content:space-between;align-items:flex-start;gap:8px;margin-bottom:6px}.pill{display:inline-block;max-width:100%;margin-bottom:6px;box-sizing:border-box}
.dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:8px;background:var(--line);border:1px solid var(--mut);vertical-align:middle}.dot.current{background:var(--ok);border-color:var(--ok);box-shadow:0 0 6px var(--ok)}.dot.old{background:var(--warn);border-color:var(--warn)}.tsrow{display:grid;grid-template-columns:64px minmax(0,1fr) 150px auto;gap:12px;align-items:center;background:var(--card);border:1px solid var(--line);border-radius:6px;padding:8px 12px;margin-bottom:6px}.tspic{position:relative;width:64px;height:48px;border-radius:4px;background:var(--line);display:flex;align-items:center;justify-content:center;overflow:hidden;color:var(--mut)}.tsmain{min-width:0;overflow-wrap:anywhere}.tsmain .mut{font-size:12px}.tsname a{font-weight:600}.tsstat{white-space:nowrap;text-align:right;font-variant-numeric:tabular-nums;font-size:13px}.tsrow .act{display:flex;gap:6px}@media(max-width:700px){.tsrow{grid-template-columns:64px minmax(0,1fr)}.tsstat{text-align:left;grid-column:2}.tsrow .act{grid-column:1/-1}}
.mut{color:var(--mut);font-size:13px}.pill{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;border:1px solid currentColor}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
#chat{min-height:120px}pre{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:10px;margin:0;max-height:320px;overflow:auto;font:12px/1.4 ui-monospace,monospace;white-space:pre-wrap;word-break:break-all}
.tablewrap{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:6px}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--line);font-size:14px;vertical-align:middle}th{color:var(--mut);font-weight:500}
td .act{display:flex;gap:6px;flex-wrap:wrap}.pdet{display:grid;gap:8px;padding:6px 0;min-width:0}.pdet h4{margin:6px 0 0;font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut)}.pdet .tablewrap{background:var(--bg)}.avatar{border-radius:4px}.pill.ok{color:var(--ok)}.pill.bad{color:var(--bad)}.pill.warn{color:var(--warn)}.pdet a{padding:3px 8px;border:1px solid var(--line);border-radius:5px;font-size:13px}td button{padding:3px 8px;font-size:13px}button.arm{border-color:var(--bad);color:var(--bad)}
#find,#srvtools{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 8px}#srvtools{margin:8px 0 0}#find input,#srvtools input{flex:1;min-width:140px;font:inherit;padding:7px 10px;border-radius:5px;border:1px solid var(--line);background:var(--card);color:var(--fg)}
nav{display:flex;gap:6px;flex-wrap:wrap;width:100%;margin:0;padding:12px max(var(--gut),env(safe-area-inset-right,0px)) 0 max(var(--gut),env(safe-area-inset-left,0px))}nav button.on{border-color:var(--acc);color:var(--acc);font-weight:600}
.off{display:none!important}section.closed>*:not(h2){display:none}.fold{margin-left:auto;padding:1px 9px}th[data-k]{cursor:pointer;user-select:none;white-space:nowrap}th[data-k]:hover{color:var(--fg)}
.bar{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 8px}.bar input,.bar select{flex:1 1 160px;min-width:0;font:inherit;padding:7px 10px;border-radius:5px;border:1px solid var(--line);background:var(--card);color:var(--fg)}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px}.tabs button.on{border-color:var(--acc);color:var(--acc)}
#sayform{display:flex;gap:8px;margin-top:8px}#pwnew,#sayform input{flex:1;min-width:0;font:inherit;padding:7px 10px;border-radius:5px;border:1px solid var(--line);background:var(--card);color:var(--fg)}
.lbcols{display:grid;grid-template-columns:minmax(0,3fr) minmax(0,2fr);gap:clamp(10px,1.2vw,16px);align-items:start}@media(max-width:900px){.lbcols{grid-template-columns:minmax(0,1fr)}}.lbcols .col{display:grid;gap:clamp(10px,1.2vw,16px)}td.lbn,th.lbn{text-align:right;font-variant-numeric:tabular-nums}.lbbar{height:4px;border-radius:2px;background:var(--acc);margin-top:3px;opacity:.7}.rk{font-weight:600;color:var(--mut)}.rk.r1{color:#f2c94c}.rk.r2{color:#c4c9d4}.rk.r3{color:#d08a5a}.tag{color:var(--acc);font-weight:600;margin-right:4px}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.65);display:grid;place-items:center;z-index:50;padding:16px}.mbox{max-width:500px;width:100%;display:grid;gap:12px}.mbox h3{margin:0}.mbox .big{font-size:28px;font-weight:600}.mbox .crow{display:flex;gap:8px;flex-wrap:wrap;align-items:center;justify-content:flex-end}.mbox label.chk{display:flex;gap:8px;align-items:flex-start}button.rst{margin-top:8px}button.danger{border-color:var(--bad);color:var(--bad)}button:disabled{opacity:.4;cursor:default}
.sv{min-width:0;background:var(--card);border:1px solid var(--line);border-radius:8px;overflow:hidden;display:flex;flex-direction:column}.sv.open{grid-column:1/-1;display:grid;grid-template-columns:clamp(260px,26vw,340px) minmax(0,1fr)}.svleft{display:flex;flex-direction:column;gap:12px;padding-bottom:12px}.svpic{border-radius:0;margin:0;padding:0}.svtop{position:absolute;left:0;right:0;top:0;z-index:2;display:flex;align-items:center;gap:8px;padding:8px 10px 16px;background:linear-gradient(rgba(0,0,0,.78),transparent);color:#fff;text-align:left}.svtop h3{flex:1;color:#fff}.svmapname{position:absolute;left:0;right:0;bottom:0;z-index:2;padding:18px 10px 8px;background:linear-gradient(transparent,rgba(0,0,0,.82));color:#fff;font-size:13px;font-weight:600;text-align:left}.sv.offline .svpic{filter:grayscale(1);opacity:.6}.svbody{padding:0 12px;display:grid;gap:10px}.svstat{display:flex;align-items:baseline;gap:6px;flex-wrap:wrap}.svstat .pill{margin:0 0 0 auto}.svcount{font-size:30px;font-weight:700;line-height:1;font-variant-numeric:tabular-nums}.svpills{display:flex;flex-wrap:wrap;gap:6px;align-items:center}.svpills .pill{margin:0}.pill.acc{color:var(--acc)}.svact{display:flex;gap:8px;flex-wrap:wrap}.svact button{flex:1 1 auto;margin:0}.svright{padding:12px 14px 14px 2px;display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px;align-content:start}.svsec{background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:10px 12px;min-width:0}.svsec h4{margin:0 0 8px;font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut)}.svsec .kv{margin:0}.svright .wide{grid-column:1/-1}.svcode{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.svcode code,.svsec code{font:12px ui-monospace,monospace;word-break:break-all}.svrun{margin-bottom:8px}.svrun b{font-size:15px}.svraw summary{cursor:pointer;color:var(--mut);font-size:13px;margin-bottom:8px}@media(max-width:700px){.sv.open{display:flex}.svright{padding:0 12px 12px}}
.tm{min-width:0;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;display:grid;gap:10px;align-content:start}.tm.open{grid-column:1/-1}.tmhead{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}.tmhead h3{flex:1;font-size:16px}.tmtag{color:var(--acc);font-weight:700}.tmpts{display:flex;gap:16px;flex-wrap:wrap}.tmpts b{font-size:22px;font-variant-numeric:tabular-nums}.tmedit{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:10px}.tmedit .crow{margin:0}.tmedit input,.tmedit select{flex:1;min-width:0;font:inherit;padding:6px 9px;border-radius:5px;border:1px solid var(--line);background:var(--bg);color:var(--fg)}.tmapp{display:flex;gap:10px;align-items:center;flex-wrap:wrap;background:var(--card);border:1px solid var(--line);border-radius:6px;padding:8px 12px;margin-bottom:8px}.tmapp .who{flex:1;min-width:200px}
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
<div class="bar"><select id="csrv"></select></div>
<div class="cmdstack">
<div class="toprow">
<div class="sv" id="mapcard"></div>
<div class="card consolecard" id="consolecard"><h3>Terminal <span class="mut" id="termsrv"></span></h3><div class="term" id="term"><pre id="cout"></pre><div class="prompt"><span>&rsaquo;</span><input id="cmd" maxlength="300" list="cmdsug" autocomplete="off" spellcheck="false" placeholder="Befehl eingeben, Tab ergänzt, ↑ Verlauf"><datalist id="cmdsug"></datalist></div></div><div class="crow"><select id="cat"></select><button id="termclear">Leeren</button></div></div>
</div>
<div class="twocol">
<div class="col">
<div class="card"><h3>Schalter</h3><div id="cflags" class="flags"></div>
<div id="copts" class="flags"></div>
<div class="crow"><span class="mut">Rotation alle</span><select id="crot"><option>10</option><option>15</option><option>20</option><option selected>30</option><option>45</option><option>60</option><option>90</option><option>120</option></select><span class="mut">Minuten</span></div>
<div id="copts2"></div>
<div class="crow"><input id="pwnew" maxlength="64" placeholder="Neues Passwort" autocomplete="off" style="flex:1;min-width:140px"><button id="pwset">Passwort setzen</button></div>
<div class="crow"><span class="mut">Liga ab nächstem Start</span><select id="lgsel"><option value="unranked">Unranked</option><option value="ranked">Ranked</option></select><button id="lgforget" title="Löscht alles, was das Dashboard für diesen Server gespeichert hat">Gespeichertes verwerfen</button></div>
<div class="mut" id="opthint"></div>
<div class="crow"><button data-c="vote-cancel">Laufenden Vote abbrechen</button></div>
</div>
</div>
<div class="col">
<div class="card"><h3>Info</h3><div class="crow"><button data-c="status">Status</button><button data-c="players">Spieler</button><button data-c="maps">Maps</button><button data-c="net">Netzwerk</button><button data-c="help">Hilfe</button></div></div>
<div class="card"><h3>Map wechseln</h3><div class="crow"><select id="cmap"></select><button id="cmapgo">Wechseln</button></div></div>
<div class="card"><h3>Tageszeit</h3><div class="crow"><select id="ctod"><option value="default">Standard</option><option value="morning">Morgen</option><option value="noon">Mittag</option><option value="afternoon">Nachmittag</option><option value="evening">Abend</option><option value="night">Nacht</option></select><button id="ctodgo">Setzen</button></div></div>
<div class="card"><h3>world-layers.json</h3><div class="mut" id="laystate"></div><label id="laydrop" class="drop"><span id="laydroptxt">world-layers.json hierher ziehen oder klicken</span><input type="file" id="layfile" accept=".json,application/json" hidden></label><div class="crow"><select id="layscope"><option value="one">nur dieser Server</option><option value="*">alle Server</option></select><button id="laygo">Hochladen</button></div></div>
</div>
</div>
<div class="card" id="poolcard"><div class="crow" style="margin:0 0 8px"><h3 style="margin:0">Map-Pool</h3><select id="poolsel" title="Pool dieses Servers"></select><button id="poolnew">Neuer Pool</button><button id="pooldel">Pool löschen</button><button id="poolsync" title="Verlinkt jede Map aus dem gemeinsamen Speicher auf jedem Server, damit sie dort wählbar ist (jeder Server behält seinen eigenen Pool)">Maps neu laden</button><input id="poolfilter" placeholder="Maps filtern" style="flex:1;min-width:120px"></div>
<div class="crow" id="poolnewrow" hidden><input id="poolname" maxlength="40" placeholder="Name des neuen Pools (Kopie des gewählten)" style="flex:1;min-width:160px"><button id="poolmk">Anlegen</button></div>
<div class="mut" id="poolhint"></div><div class="poollist" id="poollist"></div></div>
</div>
</section>
<section data-v="befehle"><h2>Maps suchen (Thunderstore)</h2>
<form class="bar" id="tsform"><input id="tsq" maxlength="60" placeholder="Suche, z. B. plaza, skatepark, Name"><select id="tsrv"></select><select id="tssort" title="Sortierung"><option value="created">Neueste Maps</option><option value="downloads">Meiste Downloads</option><option value="updated">Zuletzt aktualisiert</option><option value="rating">Beste Bewertung</option><option value="name">Name</option></select><select id="tsdir" title="Richtung"><option value="desc">absteigend</option><option value="asc">aufsteigend</option></select><label class="mut" style="display:flex;gap:6px;align-items:center"><input type="checkbox" id="tsall" style="flex:none;min-width:0">alle Pakete</label><button>Suchen</button></form>
<div class="mut" id="tsinfo">Sucht in der Thunderstore-Community reskate. "Auf Server" lädt das Paket, "In den Pool" nimmt die Map zusätzlich in den Map-Pool auf (ohne Neustart, mit neuem Image).</div><div class="tslist" id="tsres"></div></section>
<section data-v="befehle"><h2>Pool &amp; Maps pro Server</h2><div class="grid" id="poolbox"></div></section>
<section data-v="logs"><h2>Live-Log <select id="sel"><option value="">alle Server</option></select> <select id="scope"><option>log</option><option>public</option><option>admin</option><option>chat</option><option>audit</option></select></h2><pre id="events"></pre></section>
<section data-v="logs"><h2>Tool-Log <select id="tool"></select></h2><pre id="toollog">-</pre></section>
<section data-v="teams"><h2>Bewerbungen</h2><div id="tmapps"></div></section>
<section data-v="teams"><h2>Teams <span class="mut" id="tmcount"></span></h2><div class="mut" id="tmstat"></div><div class="grid" id="tmlist"></div></section>
<section data-v="rangliste"><h2>Rangliste</h2><div class="tabs" id="lbleague"></div><div class="bar"><select id="lbboard" title="Zeitraum"></select><select id="lbmode" title="Spielmodus"></select><input id="lbfind" placeholder="Spieler oder Team suchen"><button id="lbre">Aktualisieren</button></div><div class="mut" id="lbstats"></div><div class="lbcols"><div class="tablewrap"><table id="lbtable"></table></div><div class="col"><div class="tablewrap"><table id="lbteams"></table></div><div class="tablewrap"><table id="lbrecent"></table></div></div></div></section>
<section data-v="ansagen"><h2>Ansagen für alle Server</h2><div class="card"><div class="mut">Diese Ansagen laufen auf jedem Server, zusätzlich zu den eigenen. Das Spiel zeigt sie reihum im eingestellten Intervall.</div><div class="annlist" id="annglobal"></div><div class="crow"><input id="annnew-global" maxlength="220" placeholder="Neue Ansage für alle Server" style="flex:1;min-width:200px"><button id="annadd-global">Hinzufügen</button></div><div class="crow"><span class="mut">Standard-Intervall für alle Server</span><input id="anniv-all" type="number" min="1" max="1440" value="10" style="width:80px"><span class="mut">Minuten</span><button id="annivgo-all">Setzen</button></div></div></section>
<section data-v="ansagen"><h2>Ansagen pro Server <select id="annsrv"></select></h2><div class="card"><div class="mut" id="annstate"></div><div class="annlist" id="annserver"></div><div class="crow"><input id="annnew-server" maxlength="220" placeholder="Neue Ansage nur für diesen Server" style="flex:1;min-width:200px"><button id="annadd-server">Hinzufügen</button></div><div class="crow"><span class="mut">Standard-Intervall dieses Servers</span><input id="anniv-server" type="number" min="1" max="1440" style="width:80px"><span class="mut">Minuten</span><button id="annivgo-server">Setzen</button><button id="annre">Neu anwenden</button></div><div class="mut" id="annlive"></div></div></section>
</main>
<script>
const $=id=>document.getElementById(id);
const el=(t,c,x)=>{const e=document.createElement(t);if(c)e.className=c;if(x!==undefined)e.textContent=x;return e};
const RULE={restart:["Neustart ok · ≤ 3 Spieler","ok"],rollover:["Nur Rollover","warn"],empty:["aus","mut"]};
async function api(p){const r=await fetch(p);if(r.status==401){location.reload();throw 0}if(!r.ok)throw new Error((await r.json()).error||r.status);return r.json()}
const openCards=new Set(),infoCache={},infoAt={},shown={};
let drawTimer=0;const drawn=()=>{clearTimeout(drawTimer);drawTimer=setTimeout(()=>servers().catch(()=>{}),50)};
const ago=iso=>{const t=Date.parse(iso);if(!t)return '?';let m=Math.max(0,Math.round((Date.now()-t)/60000));const d=Math.floor(m/1440),h=Math.floor(m%1440/60);m%=60;return (d?d+' T ':'')+(d||h?h+' Std ':'')+m+' Min'};
const pill=(t,c,tip)=>{const p=el('span','pill '+(c||'mut'),t);if(tip)p.title=tip;return p};
const lvl=v=>({kick:['kick','bad'],warn:['warn','warn'],off:['aus','mut']}[v]||[v||'?','mut']);
const sec=(title,...kids)=>{const b=el('div','svsec'+(kids.wide?' wide':''));b.append(el('h4',0,title),...kids);return b};
const kvl=rows=>{const dl=el('dl','kv');for(const [k,v] of rows){dl.append(el('dt',0,k));const d=el('dd');if(v instanceof Node)d.append(v);else d.textContent=v;dl.append(d)}return dl};
const rowOf=(...n)=>{const r=el('div','svpills');r.append(...n);return r};
async function revealPw(s,btn){try{const r=await fetch('/api/secret?server='+s.server,{headers:{'X-Requested-With':'dashboard'}}),d=await r.json();if(!r.ok)throw new Error(d.error||r.status);shown[s.server]={pw:d.password,until:Date.now()+15000};servers()}catch(x){if(btn)btn.title='Fehler: '+x.message}}
function detail(s){const p=el('div','svright'),i=infoCache[s.server];
 if(!i){p.append(el('div','mut','Lade …'));return p}
 if(i.error){p.append(el('div','mut','Keine Details: '+i.error));return p}
 const mx=mapsCache[s.server]||{},fl=mx.flags||{},env=i.env||{};
 // Zugang
 const code=el('span'),codes=i.containers.filter(c=>c.code);
 if(codes.length){for(const c of codes){const r=el('div','svcode');r.append(el('code',0,c.code));const b=el('button',0,'Kopieren');b.onclick=()=>{navigator.clipboard.writeText(c.code).then(()=>{b.textContent='Kopiert'},()=>{b.textContent='Markieren & Strg+C'})};r.append(b);if(i.containers.length>1)r.append(el('span','mut','Slot '+c.slot));code.append(r)}}else code.textContent='noch nicht bekannt';
 const sh=shown[s.server],on=sh&&sh.until>Date.now(),pw=el('span');
 if(i.locked){pw.append(pill('gesperrt','warn'));if(on)pw.append(' ',el('code',0,sh.pw));else{const b=el('button',0,'Anzeigen');b.style.marginLeft='8px';b.onclick=()=>revealPw(s,b);pw.append(b)}}else pw.append(pill('offen','ok'));
 const ports=i.containers.map(c=>c.port).filter(Boolean).join(' / ')+(env.QUERY_PORT?' · Query '+env.QUERY_PORT:'');
 p.append(sec('Zugang',kvl([['Servername',i.name||'–'],['Join-Code',code],['Passwort',pw],['Port',ports||'?'],['Max. Spieler',env.MAX_PLAYERS||(s.max||'?')]])));
 // Betrieb
 const run=el('div');
 for(const c of i.containers){const r=el('div','svrun');r.append(el('b',0,c.version||'?'),c.revision?el('span','mut',' '+c.revision):'',' ',pill(c.health||'?',c.health=='healthy'?'ok':c.health?'warn':'mut'),el('div','mut',(c.slot?'Slot '+c.slot+' · ':'')+'läuft seit '+ago(c.started)));r.title=c.name+' · gestartet '+(c.started?new Date(c.started).toLocaleString():'?');run.append(r)}
 p.append(sec('Betrieb',run,kvl([['Container',i.containers.map(c=>c.name).join(', ')],['Updates',env.UPDATE_MODE||'?']])));
 // Regeln
 const [sc,sk]=lvl(fl.score_check),[sp,sq]=lvl(fl.speed_check),[bd,bk]=lvl(fl.board_check);
 const votes=rowOf(pill('Map-Vote',fl.vote_map?'ok':'mut'),pill('Kick-Vote',fl.vote_kick?'ok':'mut'),pill('Zeit-Vote',fl.vote_tod?'ok':'mut'));
 p.append(sec('Regeln & Rangliste',kvl([['Liga',mx.league?mx.league.charAt(0).toUpperCase()+mx.league.slice(1):'?'],['Ranked-Modus',pill(fl.ranked?'an':'aus',fl.ranked?'ok':'mut')],['Physik',pill(fl.enforce_tuning===false?'frei':'erzwungen',fl.enforce_tuning===false?'warn':'ok')],
  ['Score-Check',pill(sc,sk)],['Speed-Check',pill(sp,sq)],['Board-Check',pill(bd,bk)],['Votes',votes],['Rotation',fl.rotation>0?fl.rotation+' Min':'aus']])));
 // Maps
 const cur=(s.map||mx.map||'').replace(/[.]$/,''),pool=mx.pool||[],mods=mx.mods||[],mb=el('div');
 mb.append(kvl([['Aktuell',cur||'?']]),el('div','mut','Map-Pool ('+(pool.length||'alle')+')'),pool.length?chipRow(pool,cur,false):el('div','mut','Kein Pool: alle Maps erlaubt'),el('div','mut','Map-Pakete ('+mods.length+')'),mods.length?chipRow(mods,'',true):el('div','mut','Keine Mods'));
 if(mx.live===false)mb.append(el('div','mut','Stand vom Containerstart (Live-Daten nicht lesbar)'));
 const maps=sec('Maps',mb);maps.classList.add('wide');p.append(maps);
 // Texte
 const tx=[['Willkommen',env.WELCOME_MESSAGE||'–']];if(env.ANNOUNCEMENTS)tx.push(['Ansagen (Start)',env.ANNOUNCEMENTS.split('|').length+' Texte, siehe Reiter Ansagen']);
 p.append(sec('Texte',kvl(tx)));
 // alles andere
 const raw=el('details','svraw'),ks=Object.keys(env).sort();raw.append(el('summary',0,'Alle Container-Einstellungen ('+ks.length+')'),kvl(ks.map(k=>[k,env[k]||'(leer)'])));
 const rs=sec('Rohdaten',raw);rs.classList.add('wide');p.append(rs);
 p.append(Object.assign(el('div','mut wide'),{textContent:'Einstellungen aus der Umgebung des Containers beim Start (ohne Tokens und Webhooks); Regeln, Liga und Maps zeigen den Stand des laufenden Servers.'}));
 return p}
function svCard(s,d){
 const open=openCards.has(s.server),c=el('div','sv'+(open?' open':'')+(s.running?'':' offline')),[t,k]=RULE[s.rule]||[s.rule,'mut'],mx=mapsCache[s.server],fl=(mx&&mx.flags)||{};
 const left=el('div','svleft'),mapName=(s.map||'').replace(/[.]$/,''),pic=el('div','pic svpic');pic.append(el('span',0,mapName||'Kein Bild'));
 if(s.running&&s.map&&imgState[mapName]!='none'){const im=el('img','mapimg');im.alt='';im.style.opacity=imgState[mapName]=='ok'?1:0;im.onload=()=>{imgState[mapName]='ok';im.style.opacity=1};im.onerror=()=>{imgState[mapName]='none';im.remove()};im.src='/api/mapimg?name='+encodeURIComponent(mapName);pic.append(im)}
 const top=el('div','svtop');top.append(el('span','num','#'+s.server),el('h3',0,s.role||('Server '+s.server)));
 if(s.locked){const sh=shown[s.server],on=sh&&sh.until>Date.now(),lk=el('button','lock',on?'🔓 '+sh.pw:'🔒');lk.title=on?'Passwort (verschwindet nach 15 s)':'Passwortgeschützt, Klick zeigt das Passwort';lk.onclick=e=>{e.stopPropagation();revealPw(s,lk)};top.append(lk)}
 pic.append(top,Object.assign(el('div','svmapname'),{textContent:s.running?(mapName||'?'):'offline'}));
 const body=el('div','svbody'),cnt=s.running?(s.online!=null?s.online:s.players):null,stat=el('div','svstat');
 stat.append(el('span','svcount',s.running?(cnt==null?'?':cnt):'–'),el('span','mut','/ '+(s.running?(s.max||'?'):'?')+' Spieler'),pill(t,k,'Neustart direkt nur bis '+d.cutoff+' Spieler; mehr Spieler oder unbekannt: nur über den Rollover'));
 if(s.running&&s.online!=null&&s.players!=null&&s.players!=s.online)stat.title='Beigetreten: '+s.online+'. Der Status zählt '+s.players+' (Verbindungen, die gerade laden oder noch nicht beigetreten sind).';
 body.append(stat);
 if(s.running&&cnt!=null&&s.max){const f=Math.min(1,cnt/s.max),bar=el('div',f>=1?'pbar full':'pbar'),fill=el('span',f>=.85?'bad':f>=.5?'warn':'ok');fill.style.width=Math.max(f*100,cnt?3:0)+'%';bar.append(fill);bar.title=Math.round(f*100)+' % belegt';body.append(bar)}
 const badges=el('div','svpills');
 if(mx&&mx.league)badges.append(pill(mx.league=='ranked'?'Ranked-Liga':'Unranked-Liga','acc'));
 if(fl.ranked)badges.append(pill('Ranked-Modus','ok'));
 if(fl.enforce_tuning===false)badges.append(pill('Physik frei','warn'));
 for(const [l,x] of Object.entries(s.slots||{}))if(x.running&&x.version)badges.append(pill((Object.keys(s.slots).length>1?l+' · ':'')+x.version,'mut',x.running?'Slot '+l:''));
 if(badges.children.length)body.append(badges);
 const act=el('div','svact'),more=el('button',0,open?'Details ▴':'Details ▾');
 more.onclick=async()=>{if(openCards.has(s.server))openCards.delete(s.server);else{openCards.add(s.server);delete infoAt[s.server]}await servers()};act.append(more);
 if(s.running&&d.mod){const rb=el('button','rst','Neu starten …');rb.title='Server neu starten, mit Spielerzahl und Bestätigung';rb.onclick=()=>restartDialog(s);act.append(rb)}
 body.append(act);left.append(pic,body);c.append(left);
 if(open){c.append(detail(s));if(!(infoAt[s.server]>Date.now()-30000)){infoAt[s.server]=Date.now();Promise.all([api('/api/serverinfo?server='+s.server),api('/api/maps')]).then(([i,m])=>{Object.assign(mapsCache,m);infoCache[s.server]=i;infoAt[s.server]=Date.now();drawn()},x=>{infoCache[s.server]={error:x.message};drawn()})}}
 return c}
let cardMapsAt=0;
const imgState={};
function put(real,nodes){const t=document.createElement('div');t.append(...nodes);const sig=t.innerHTML;if(real.dataset.sig===sig)return;real.dataset.sig=sig;const y=scrollY;real.replaceChildren(...t.childNodes);if(scrollY!=y)scrollTo(0,y)}
function restartDialog(s0){
 const ov=el('div','modal'),box=el('div','card mbox'),slots=Object.entries(s0.slots||{}).filter(x=>x[1].running).map(x=>x[0]);
 let cur=s0,slot=slots[0]||'',timer=0,done=false;
 const close=()=>{clearInterval(timer);ov.remove();servers()};
 const head=el('h3',0,'Server '+s0.server+' '+(s0.role||'')+' neu starten'),count=el('div','big'),info=el('div','mut'),warn=el('div','bad'),
  chk=el('label','chk'),cb=document.createElement('input'),out=el('div','mut'),go=el('button','danger','Jetzt neu starten'),no=el('button',0,'Abbrechen'),row=el('div','crow');
 cb.type='checkbox';chk.append(cb,el('span',0,''));
 const pick=document.createElement('select');for(const k of slots)pick.add(new Option('Slot '+k,k));pick.hidden=slots.length<2;pick.onchange=()=>{slot=pick.value;cb.checked=false;draw()};
 const players=()=>{const x=slots.length>1?(cur.slots||{})[slot]:cur;return x?x.players:null};
 function draw(){if(done)return;const p=players(),busy=!(p!=null&&p<=cur_cutoff);
  count.textContent=p==null?'Spielerzahl unbekannt':p+(p==1?' Spieler online':' Spieler online');
  count.className='big '+(busy?'bad':'ok');
  info.textContent=busy?'Die Regel erlaubt einen Neustart nur bis '+cur_cutoff+' Spieler, sonst über den Rollover.':'Innerhalb der Regel (bis '+cur_cutoff+' Spieler).';
  warn.textContent=busy?(p==null?'Die Spielerzahl ist unbekannt, es könnten Leute drin sein. ':'Alle '+p+' Spieler fliegen raus und verlieren ihre laufende Runde. ')+'Das ist ein erzwungener Neustart.':'';
  chk.hidden=!busy;chk.lastChild.textContent=busy?'Ich weiß das und will '+(p==null?'trotzdem':'die '+p+' Spieler rauswerfen und')+' neu starten.':'';
  go.disabled=busy&&!cb.checked}
 let cur_cutoff=3;
 cb.onchange=()=>draw();
 const refresh=async()=>{try{const d=await api('/api/servers');cur_cutoff=d.cutoff;const n=d.servers.find(x=>x.server==s0.server);if(n){const before=players();cur=n;if(players()!==before)cb.checked=false}draw()}catch(e){}};
 go.onclick=async()=>{go.disabled=true;out.className='mut';out.textContent='Starte neu …';
  try{const p=players();await post('/api/restart',{server:s0.server,slot:slots.length>1?slot:'',force:!(p!=null&&p<=cur_cutoff),players:p});
   done=true;clearInterval(timer);out.className='ok';out.textContent='Neustart ausgelöst. Der Server ist in ein bis zwei Minuten wieder da, der Join-Code ändert sich.';go.hidden=true;chk.hidden=true;warn.textContent='';no.textContent='Schließen'}
  catch(e){out.className='bad';out.textContent=e.message;cb.checked=false;refresh()}};
 no.onclick=close;ov.onclick=e=>{if(e.target==ov)close()};
 row.append(pick,no,go);box.append(head,count,info,warn,chk,out,row);ov.append(box);document.body.append(ov);
 draw();refresh();timer=setInterval(refresh,3000)}
let lastServers=[];
async function servers(){
 const d=await api('/api/servers'),real=$('servers'),box=document.createElement('div');
 const sel=$('sel');if(sel.options.length==1)d.servers.forEach(s=>sel.add(new Option(s.server+' '+s.role,s.server)));
 tabs(d);mod(d);lastServers=d.servers;
 if(Date.now()-cardMapsAt>25000){cardMapsAt=Date.now();try{Object.assign(mapsCache,await api('/api/maps'))}catch(e){}}
 for(const s of d.servers)box.append(svCard(s,d))
 put(real,[...box.childNodes])
}
function diskWarn(k){const w=$('diskwarn');if(!k){w.hidden=true;return}
 const gb=k.free/1e9,pct=k.free/k.total*100,low=gb<20||pct<10;w.hidden=!low;
 w.textContent='Speicherplatz knapp: nur noch '+gb.toFixed(1)+' GB ('+pct.toFixed(0)+' %) auf der Host-Platte frei'+(gb<5?'. Leaderboards und Settings können nicht mehr schreiben.':'.');
 w.style.borderColor=gb<5?'var(--bad)':'var(--warn)'}
async function containers(){
 const d=await api('/api/containers'),rs=$('containers'),rt=$('tools'),srv=document.createElement('div'),tl=document.createElement('div');diskWarn(d.disk);const fin=()=>{put(rs,[...srv.childNodes]);put(rt,[...tl.childNodes])};
 if(!d.docker){srv.append(el('div','mut','Docker-Socket nicht eingebunden (DASHBOARD_DOCKER).'));fin();return}
 const t=$('tool');if(!t.options.length)d.containers.filter(c=>c.tool).forEach(c=>t.add(new Option(c.name)));
 const order=c=>(c.state=='running'?0:1);
 for(const c of d.containers.slice().sort((a,b)=>order(a)-order(b)||a.name.localeCompare(b.name))){const e=el('div','card'),run=c.state=='running';
  const hd=el('div','head');hd.append(el('h3',0,c.name),el('span','pill '+(run?'ok':'bad'),c.state));
  e.append(hd,el('div','mut',c.image.replace(/^sha256:(.{12}).*/,'$1')+' · '+c.status));(c.tool?tl:srv).append(e)}
 if(!tl.children.length)tl.append(el('div','mut','Keine Tools gefunden.'));
 if(!srv.children.length)srv.append(el('div','mut','Keine Server-Container gefunden.'));
 fin()
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
const TL=[],thist=[];let tlast=0,tsrv='',thi=0;
function tdraw(){const p=$('cout'),b=p.scrollTop+p.clientHeight>=p.scrollHeight-24;p.textContent=TL.join('\\n');if(b)p.scrollTop=p.scrollHeight}
function tprint(t){for(const l of String(t).split('\\n'))TL.push(l);if(TL.length>500)TL.splice(0,TL.length-500);tdraw()}
async function termPoll(){if(view!='befehle')return;const sv=$('csrv').value;if(!sv)return;
 if(sv!=tsrv){tsrv=sv;tlast=0;TL.length=0;$('termsrv').textContent='· Server '+sv;tprint('— Log von Server '+sv+' —')}
 const d=await api('/api/events?'+new URLSearchParams({after:tlast,server:sv,scope:'log'}));if(sv!=$('csrv').value)return;
 const first=tlast==0;tlast=d.last;const ev=first?d.events.slice(-60):d.events;
 if(ev.length)tprint(ev.map(e=>(e.time||'').slice(11,19)+' '+e.text).join('\\n'))}
setInterval(()=>termPoll().catch(()=>{}),1500);
async function sendCmd(c){c=c.trim();if(!c)return;tprint('> '+c);if(thist[thist.length-1]!=c)thist.push(c);thi=thist.length;
 const r=await fetch('/api/mod',{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'dashboard'},body:JSON.stringify({server:$('csrv').value,action:'console',text:c})});
 const d=await r.json().catch(()=>({}));if(!r.ok){tprint('Fehler: '+(d.error||r.status));return}
 setTimeout(()=>termPoll().catch(()=>{}),700)}
let cmdAt=0;
async function cmdMaps(force){const sv=$('csrv').value;if(!sv)return;
 if(force||!mapsCache[sv]||!POOLS||Date.now()-cmdAt>20000){cmdAt=Date.now();await loadPools();try{Object.assign(mapsCache,await api('/api/maps'+(force?'?fresh=1&server='+encodeURIComponent(sv):'')))}catch(e){}}
 const x=mapsCache[sv]||{pool:[],available:[],map:''},pool=x.pool||[],av=x.available||[],lower=new Set(pool.map(n=>n.toLowerCase())),rest=av.filter(n=>!lower.has(n.toLowerCase()));
 const fillSel=(sel,groups)=>{const sig=JSON.stringify(groups);if(sel.dataset.sig===sig&&sel.options.length)return;sel.dataset.sig=sig;const keep=sel.value;sel.replaceChildren();for(const [label,list] of groups){if(!list.length)continue;const o=document.createElement('optgroup');o.label=label;for(const n of list)o.append(new Option(n,n));sel.append(o)}
  if(!sel.options.length)sel.add(new Option('(keine)',''));if([...sel.options].some(o=>o.value==keep))sel.value=keep};
 fillSel($('cmap'),[['Im Pool',pool],['Installiert, nicht im Pool',rest]]);drawPool(sv);drawFlags(sv,x.flags);
 drawMapCard((x.map||'').replace(/[.]$/,''),pool.length,av.length)}
function drawMapCard(name,np,na){const sv=$('csrv').value,card=$('mapcard'),s=(lastServers.find(x=>x.server==sv))||{server:sv,running:true},mx=mapsCache[sv]||{},fl=mx.flags||{};
 const sig=sv+'|'+name+'|'+(s.role||'')+'|'+(s.running?1:0)+'|'+(s.locked?1:0);
 if(card.dataset.sig!=sig){card.dataset.sig=sig;card.classList.toggle('offline',s.running===false);
  const pic=el('div','pic svpic');pic.append(el('span',0,name||'Kein Bild'));
  if(name&&s.running!==false&&imgState[name]!='none'){const im=el('img','mapimg');im.alt='';im.style.opacity=imgState[name]=='ok'?1:0;im.onload=()=>{imgState[name]='ok';im.style.opacity=1};im.onerror=()=>{imgState[name]='none';im.remove()};im.src='/api/mapimg?name='+encodeURIComponent(name);pic.append(im)}
  const top=el('div','svtop');top.append(el('span','num','#'+sv),el('h3',0,s.role||('Server '+sv)));
  pic.append(top,Object.assign(el('div','svmapname'),{textContent:s.running===false?'offline':(name||'?')}));
  const body=el('div','svbody');body.id='mapbody';card.replaceChildren(el('div','svleft'));card.firstChild.append(pic,body)}
 const body=$('mapbody'),cnt=s.running===false?null:(s.online!=null?s.online:s.players),stat=el('div','svstat');
 stat.append(el('span','svcount',s.running===false?'–':(cnt==null?'?':cnt)),el('span','mut','/ '+(s.running===false?'?':(s.max||'?'))+' Spieler'),pill('Läuft gerade','ok'));
 const kids=[stat];
 if(s.running!==false&&cnt!=null&&s.max){const f=Math.min(1,cnt/s.max),bar=el('div',f>=1?'pbar full':'pbar'),fill=el('span',f>=.85?'bad':f>=.5?'warn':'ok');fill.style.width=Math.max(f*100,cnt?3:0)+'%';bar.append(fill);bar.title=Math.round(f*100)+' % belegt';kids.push(bar)}
 const badges=el('div','svpills');badges.append(pill('Pool: '+(np||'alle'),'acc'),pill('installiert: '+na,'mut'));
 if(mx.league)badges.append(pill(mx.league=='ranked'?'Ranked-Liga':'Unranked-Liga','acc'));
 if(fl.ranked)badges.append(pill('Ranked-Modus','ok'));
 kids.push(badges);put(body,kids)}
const armed=(b,f)=>{if(!b.classList.contains('arm')){b.dataset.t=b.textContent;b.classList.add('arm');b.textContent='Sicher?';setTimeout(()=>{if(b.classList.contains('arm')){b.classList.remove('arm');b.textContent=b.dataset.t}},3000);return}b.classList.remove('arm');b.textContent=b.dataset.t;f()};
$('csrv').onchange=()=>cmdMaps(true);
$('cmapgo').onclick=e=>{const m=$('cmap').value;if(m)armed(e.target,()=>sendCmd('map '+m))};
async function fire(c,sv){tprint('> '+c+(sv&&sv!=$('csrv').value?'  (Server '+sv+')':''));
 const r=await fetch('/api/mod',{method:'POST',headers:HDR,body:JSON.stringify({server:sv||$('csrv').value,action:'console',text:c})});
 if(!r.ok){const d=await r.json().catch(()=>({}));throw new Error(d.error||r.status)}}
const pend={},qs={};
const wanted=(sv,k,cur)=>{const p=pend[sv+'|'+k];return p&&Date.now()-p.at<12000?p.want:cur};
function toggle(sv,k,val,cmd,check){const key=sv+'|'+k;pend[key]={want:val,at:Date.now()};
 const sent=(qs[sv]||Promise.resolve()).then(()=>typeof cmd=='function'?cmd():fire(cmd,sv)).catch(e=>{delete pend[key];tprint('Fehler: '+e.message);throw e});
 qs[sv]=sent.catch(()=>{});
 sent.then(async()=>{for(let i=0;i<14;i++){await sleep(i?350:200);delete mapsCache[sv];if($('csrv').value==sv)await cmdMaps(true);else Object.assign(mapsCache,await api('/api/maps?fresh=1&server='+encodeURIComponent(sv)));if(check(mapsCache[sv]||{})==val)break}
  if(pend[key]&&pend[key].want===val)delete pend[key];if($('csrv').value==sv)await cmdMaps(true)}).catch(()=>{if($('csrv').value==sv)cmdMaps(true)})}
const FIT={unloaded:['bad','liegt auf dem Server, wurde beim Start aber nicht geladen (das passiert nur beim Start). Layer-Sync geht erst nach dem nächsten Neustart'],ok:['ok','passt zur Spielversion dieses Servers'],old:['bad','stammt von einer anderen Version als dieser Server, bitte neu hochladen'],unknown:['warn','vorhanden, aber ohne Versionsangabe (von Hand kopiert), bitte neu hochladen'],missing:['bad','fehlt (nötig für Layer-Sync und Zeit-Vote)']};
function drawLayFit(fl){const t=$('laystate');t.textContent='';if(!fl)return;const [c,m]=FIT[fl.layers_fit]||FIT.missing,b=el('span','fit '+c,c=='ok'?'✓':c=='warn'?'?':'✗');t.append(b,' world-layers.json '+m)}
const FLAGS=[['vote_map','Map-Vote','votes map on','votes map off'],['vote_kick','Kick-Vote','votes kick on','votes kick off'],['vote_tod','Zeit-Vote','votes tod on','votes tod off'],['rotation','Map-Rotation',null,'rotation off'],['layer_sync','Layer-Sync','layer-sync on','layer-sync off']];
function drawFlags(sv,fl){const real=$('cflags'),box=document.createElement('div');drawLayFit(fl);
 if(!fl){box.append(el('div','mut','Zustand unbekannt (Server nicht erreichbar).'));put(real,[...box.childNodes]);return}
 if(fl.rotation>0&&[...$('crot').options].some(o=>o.value==fl.rotation))$('crot').value=fl.rotation;
 for(const [k,label,onc,offc] of FLAGS){const cur=k=='rotation'?fl.rotation>0:fl[k],on=wanted(sv,'flag:'+k,cur),lb=el('label','tgl'+(on?' on':'')),cb=document.createElement('input');cb.type='checkbox';cb.checked=on;
  cb.onchange=()=>{lb.classList.toggle('on',cb.checked);toggle(sv,'flag:'+k,cb.checked,cb.checked?(onc||'rotation '+$('crot').value):offc,x=>{const f=x.flags||{};return k=='rotation'?f.rotation>0:!!f[k]})};
  lb.append(cb,el('span','sw'),el('span','tn',label+(k=='rotation'&&cur?' ('+fl.rotation+' min)':'')));box.append(lb)}
put(real,[...box.childNodes]);drawOpts(sv,fl)}
const optBody=(sv,b)=>post('/api/option',Object.assign({server:sv,persist:true},b));
function drawOpts(sv,fl){const real=$('copts'),box=document.createElement('div');
 for(const [k,label,tip,body] of [['locked','Gesperrt (Passwort aktiv)','Ein: das gemerkte Passwort wird wieder gesetzt. Aus: Passwort wird entfernt und gemerkt.',on=>({action:on?'lock':'unlock'})],
   ['ranked','Ranked-Modus','Ein: Physik erzwungen, Score-Check und Speed-Check auf kick. Aus: Score-Check und Speed-Check auf warn.',on=>({action:'ranked',on})]]){
  const cur=!!fl[k],on=wanted(sv,'opt:'+k,cur),lb=el('label','tgl'+(on?' on':'')),cb=document.createElement('input');cb.type='checkbox';cb.checked=on;lb.title=tip;
  cb.onchange=()=>{lb.classList.toggle('on',cb.checked);toggle(sv,'opt:'+k,cb.checked,()=>optBody(sv,body(cb.checked)),x=>!!(x.flags||{})[k])};
  lb.append(cb,el('span','sw'),el('span','tn',label));box.append(lb)}
 const tl=wanted(sv,'opt:tuning',fl.enforce_tuning),tb=el('label','tgl'+(tl?' on':'')),tc=document.createElement('input');tc.type='checkbox';tc.checked=tl;tb.title='Ein: alle spielen mit der Standard-Physik des Spiels. Aus: Spieler dürfen ihre eigene Physik-Einstellung nutzen (dann zählt niemand als Ranked).';
 tc.onchange=()=>{tb.classList.toggle('on',tc.checked);toggle(sv,'opt:tuning',tc.checked,()=>optBody(sv,{action:'tuning',on:tc.checked}),x=>!!(x.flags||{}).enforce_tuning)};
 tb.append(tc,el('span','sw'),el('span','tn','Physik erzwungen'));box.append(tb);
 put(real,[...box.childNodes]);
 const cs=$('copts2'),rows=document.createElement('div');
 for(const [k,label,key,tip] of [['score','Score-Check','score_check','Spieler mit Mods, die die Punkte verändern'],['speed','Speed-Check','speed_check','Spieler mit beschleunigtem Spiel'],['board','Board-Check','board_check','Spieler mit angepinntem Board']]){
  const cur=fl[key]||'',val=wanted(sv,'opt:'+k,cur),row=el('div','crow'),sel=document.createElement('select');row.dataset.v=val;
  for(const o of ['off','warn','kick'])sel.add(new Option(o,o));sel.value=val;sel.title=tip+': off = ignorieren, warn = nur melden, kick = rauswerfen';
  sel.onchange=()=>toggle(sv,'opt:'+k,sel.value,()=>optBody(sv,{action:k,value:sel.value}),x=>(x.flags||{})[key]);
  row.append(el('span','mut',label),sel);rows.append(row)}
 put(cs,[...rows.childNodes]);

 const mc=mapsCache[sv]||{},sv_=mc.saved||{},names={SERVER_PASSWORD:'Passwort',ENFORCE_TUNING:'Physik',SCORE_CHECK:'Score-Check',SPEED_CHECK:'Speed-Check',BOARD_CHECK:'Board-Check',LEADERBOARD_LEAGUE:'Liga'},keys=Object.keys(sv_);
 const lg=$('lgsel');if(document.activeElement!==lg)lg.value=sv_.LEADERBOARD_LEAGUE||mc.league||'ranked';lg.onchange=()=>optBody(sv,{action:'league',value:lg.value,persist:true}).then(()=>cmdMaps(true)).catch(e=>tprint('Fehler: '+e.message));
 $('lgforget').disabled=!keys.length;$('lgforget').onclick=()=>armed($('lgforget'),()=>optBody(sv,{action:'forget'}).then(()=>cmdMaps(true)).catch(e=>tprint('Fehler: '+e.message)));
 $('opthint').textContent=(keys.length?'Für den Neustart gespeichert: '+keys.map(k=>names[k]+' '+({ENFORCE_TUNING:v=>v=='false'?'frei':'erzwungen',SERVER_PASSWORD:v=>v=='set'?'gesetzt':'aus'}[k]||(v=>v))(sv_[k])).join(' · ')+'. ':'Nichts gespeichert: nach einem Neustart gelten die Einstellungen des Containers. ')+'Die Liga (in welche Rangliste Punkte laufen) wird nur beim Start gelesen, ein Wechsel braucht also einen Neustart. Laufende Liga: '+(mc.league||'?')+'.'}
$('pwset').onclick=async()=>{const sv=$('csrv').value,w=$('pwnew').value.trim();if(!sv||!w)return;
 try{await optBody(sv,{action:'password',value:w});$('pwnew').value='';tprint('Passwort von Server '+sv+' gesetzt');await sleep(600);await cmdMaps(true)}catch(e){tprint('Fehler: '+e.message)}};
let POOLS=null;
const loadPools=async()=>{try{POOLS=await api('/api/pools')}catch(e){}};
const ci=(a,b)=>a.toLowerCase().localeCompare(b.toLowerCase());
let pq=Promise.resolve(),pqn=0;
function poolPost(body){pqn++;const p=pq.then(()=>post('/api/pools',body)).catch(e=>{tprint('Fehler: '+e.message)}).finally(async()=>{if(--pqn==0){await loadPools();await cmdMaps(true)}});pq=p.catch(()=>{});return p}
function drawPool(sv){if(!POOLS)return;const real=$('poollist'),box=document.createElement('div'),f=$('poolfilter').value.trim().toLowerCase(),cur=POOLS.assign[sv]||'Alle Maps',isAll=cur=='Alle Maps',on=new Set((POOLS.pools[cur]||[]).map(n=>n.toLowerCase()));
 const sel=$('poolsel'),opts=[['Alle Maps','Alle Maps ('+POOLS.library.length+')'],...Object.keys(POOLS.pools).sort(ci).map(k=>[k,k+' ('+POOLS.pools[k].length+')'])],sig=JSON.stringify(opts);
 if(sel.dataset.sig!=sig){sel.dataset.sig=sig;sel.replaceChildren(...opts.map(o=>new Option(o[1],o[0])))}
 sel.value=cur;$('pooldel').disabled=isAll;
 const users=Object.keys(POOLS.assign).filter(n=>POOLS.assign[n]==cur).sort((a,b)=>a-b),h=$('poolhint');
 h.textContent=(isAll?'Alle Maps ist fest: jede installierte Map ist erlaubt. Wähle einen eigenen Pool oder lege einen neuen an, um Maps abzuschalten.':on.size+' von '+POOLS.library.length+' Maps im Pool. Nur Maps im Pool kommen in die Rotation und in den Vote.')+' Genutzt von Server '+users.join(', ')+'.'+(POOLS.drift[sv]?' Der laufende Server weicht gerade ab.':'');
 if(!isAll&&POOLS.drift[sv]){const b=el('button',0,'Neu anwenden');b.onclick=()=>poolPost({action:'reapply',server:sv});h.append(' ',b)}
 for(const n of POOLS.library){if(f&&!n.toLowerCase().includes(f))continue;
  const low=n.toLowerCase(),isOn=isAll||on.has(low),lb=el('label','tgl'+(isOn?' on':'')),cb=document.createElement('input');cb.type='checkbox';cb.checked=isOn;cb.disabled=isAll;
  cb.onchange=()=>{lb.classList.toggle('on',cb.checked);const l=(POOLS.pools[cur]||[]).filter(m=>m.toLowerCase()!=low);if(cb.checked)l.push(n);POOLS.pools[cur]=l;poolPost({action:'set',server:sv,name:cur,map:n,on:cb.checked})};
  lb.append(cb,el('span','sw'),el('span','tn',n));box.append(lb)}
 if(!box.children.length)box.append(el('div','mut','Keine Treffer.'));
 put(real,[...box.childNodes])}
$('poolsel').onchange=()=>{const sv=$('csrv').value;POOLS.assign[sv]=$('poolsel').value;drawPool(sv);poolPost({action:'assign',server:sv,name:$('poolsel').value})};
$('poolnew').onclick=()=>{$('poolnewrow').hidden=!$('poolnewrow').hidden;if(!$('poolnewrow').hidden)$('poolname').focus()};
$('poolmk').onclick=()=>{const n=$('poolname').value.trim();if(!n)return;$('poolname').value='';$('poolnewrow').hidden=true;poolPost({action:'create',server:$('csrv').value,name:n})};
$('poolname').onkeydown=e=>{if(e.key=='Enter')$('poolmk').click()};
$('poolsync').onclick=e=>armed(e.target,()=>poolPost({action:'sync'}));
$('pooldel').onclick=e=>{const sv=$('csrv').value,cur=POOLS&&POOLS.assign[sv];if(cur&&cur!='Alle Maps')armed(e.target,()=>poolPost({action:'delete',server:sv,name:cur}))};
const layName=()=>{const f=$('layfile').files[0];$('laydroptxt').textContent=f?f.name+' ('+Math.ceil(f.size/1024)+' KB), bereit zum Hochladen':'world-layers.json hierher ziehen oder klicken'};
$('layfile').onchange=layName;
for(const ev of['dragenter','dragover'])$('laydrop').addEventListener(ev,e=>{e.preventDefault();$('laydrop').classList.add('over')});
for(const ev of['dragleave','drop'])$('laydrop').addEventListener(ev,e=>{e.preventDefault();$('laydrop').classList.remove('over')});
$('laydrop').addEventListener('drop',e=>{if(e.dataTransfer&&e.dataTransfer.files.length){$('layfile').files=e.dataTransfer.files;layName()}});
$('laygo').onclick=async()=>{const f=$('layfile').files[0];if(!f){tprint('Erst eine world-layers.json auswählen.');return}
 try{const r=await post('/api/layers',{server:$('layscope').value=='*'?'*':$('csrv').value,content:await f.text()});tprint(r.text+'\\nDanach "Layer-Sync" einschalten.');delete mapsCache[$('csrv').value];await cmdMaps(true)}catch(e){tprint('Fehler: '+e.message)}};
$('poolfilter').oninput=()=>drawPool($('csrv').value);
$('crot').onchange=()=>{const x=mapsCache[$('csrv').value];if(x&&x.flags&&x.flags.rotation>0)sendCmd('rotation '+$('crot').value).then(()=>sleep(1200)).then(()=>cmdMaps(true))};
$('ctodgo').onclick=()=>sendCmd('tod '+$('ctod').value);
document.addEventListener('DOMContentLoaded',()=>{});
let knownAt=0;
async function who(){if(!modOn)return;people=await api('/api/players');if(Date.now()-knownAt>60000){knownAt=Date.now();try{known=await api('/api/knownplayers')}catch(e){}}draw()}
function say_(m){$('mres').textContent=m}
async function act(body,b){
 if(b&&!b.classList.contains('arm')&&/^(kick|ban)$/.test(body.action)){b.classList.add('arm');const t=b.textContent;b.textContent='Sicher?';setTimeout(()=>{b.classList.remove('arm');b.textContent=t},3000);return}
 const r=await fetch('/api/mod',{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'dashboard'},body:JSON.stringify(body)});
 const d=await r.json();say_(r.ok?'Gesendet: '+body.action+(body.player?' '+body.player:''):'Fehler: '+(d.error||r.status));if(r.ok)setTimeout(who,1500)}
let sortK='name',sortD=1;
function cmp(a,b){let x,y;if(sortK=='server'){x=(parseInt(a.server)||0)*10+(a.slot=='B'?1:0);y=(parseInt(b.server)||0)*10+(b.slot=='B'?1:0)}
 else if(sortK=='team'){x=(a.team||'\\uffff').toLowerCase();y=(b.team||'\\uffff').toLowerCase()}else{x=a.name.toLowerCase();y=b.name.toLowerCase()}
 return (x<y?-1:x>y?1:0)*sortD}
const opened=new Set(),profs={},picks={};let known=[],offShown=100;
const PKIND=[['general','Gesamt'],['year','Jahr'],['month','Monat'],['week','Woche']];
function link(href,text){const a=el('a',0,text);a.href=href;a.target='_blank';a.rel='noopener noreferrer';return a}
const steams={};
async function loadSteam(id){if(steams[id])return;steams[id]={};try{steams[id]=await api('/api/steam?steam_id='+id)}catch(e){steams[id]={error:e.message}}draw()}
function steamRow(id){const d=steams[id],row=el('div','crow');if(!d||!Object.keys(d).length){row.append(el('span','mut','Steam-Status wird geladen …'));return row}
 if(d.error){row.append(el('span','mut','Steam-Status nicht verfügbar: '+d.error));return row}
 if(d.avatar){const i=el('img','avatar');i.src=d.avatar;i.alt='';i.width=32;i.height=32;i.referrerPolicy='no-referrer';row.append(i)}
 const pill=(t,c)=>row.append(el('span','pill '+c,t));
 row.append(el('b',0,d.name||'?'));
 pill(d.vac?'VAC-Ban':'Kein VAC-Ban',d.vac?'bad':'ok');
 if(d.trade_ban&&d.trade_ban!='None')pill('Trade-Ban: '+d.trade_ban,'warn');
 if(d.limited)pill('Limitiertes Konto','warn');
 pill(d.privacy=='public'?'Profil öffentlich':'Profil '+(d.privacy||'privat'),d.privacy=='public'?'':'warn');
 if(d.since)row.append(el('span','mut','Steam seit '+d.since));
 return row}
const pk=p=>p.steam_id||('name:'+p.name);
async function openProfile(p,fresh){const id=pk(p);if(p.steam_id)loadSteam(p.steam_id);if(!fresh&&profs[id])return;
 try{profs[id]=await api('/api/player?steam_id='+encodeURIComponent(p.steam_id||'')+'&name='+encodeURIComponent(p.name||''))}catch(e){profs[id]={error:e.message}}draw()}
function toggleProfile(p){if(opened.has(pk(p)))opened.delete(pk(p));else{opened.add(pk(p));openProfile(p)}draw()}
async function setPlayerTeam(p,tag){say_('…');
 try{const r=await post('/api/playerteam',{steam_id:p.steam_id,name:p.name,team:tag});say_('Team: '+(r.text||'ok'));delete picks[p.steam_id]}catch(e){say_('Fehler: '+e.message)}
 await openProfile(p,true);who()}
function statsTable(st){
 const t=el('table'),hd=el('tr');hd.append(el('th',0,'Modus'));for(const [k,l] of PKIND){const per=st.periods.find(x=>x.kind==k);if(per)hd.append(el('th','lbn',l+(k=='general'?'':' · '+per.name)))}
 t.append(hd);
 for(const m of st.modes){const cells=st.cells[m.mode]||{};if(m.mode&&!Object.keys(cells).length)continue;const tr=el('tr');tr.append(el('td',0,m.name));
  for(const [k] of PKIND){if(!st.periods.some(x=>x.kind==k))continue;const c=cells[k],td=el('td','lbn');
   if(c){td.textContent=c.points+' P · '+c.wins+'/'+c.played+' · #'+c.rank;td.title=c.points+' Punkte, '+c.wins+' Siege in '+c.played+' Spielen, Platz '+c.rank+' von '+c.of}else td.textContent='–';tr.append(td)}
  t.append(tr)}
 return t}
function profileBox(p){const d=profs[pk(p)],box=el('div','pdet');
 if(!d){box.append(el('span','mut','Lade …'));return box}
 if(d.error){box.append(el('span','mut','Fehler: '+d.error));return box}
 if(!p.steam_id){box.append(el('span','mut','Keine Steam-ID bekannt: der Spieler steht nur mit Namen in der Unranked-Rangliste. Team und Steam-Profil gibt es erst, wenn er wieder auf einem Server war.'));for(const league of ['unranked','ranked']){const st=d.stats[league];if(Object.keys(st.cells).length){box.append(el('h4',0,league=='ranked'?'Ranked':'Unranked'));const w=el('div','tablewrap');w.append(statsTable(st));box.append(w)}}return box}
 const top=el('div','crow');top.append(link(d.steam,'Steam-Profil'));if(d.discord)top.append(link(d.discord,'Discord-Profil'));else top.append(el('span','mut','Discord nicht verknüpft'));
 const sel=el('select');sel.append(new Option('Kein Team',''));for(const t of d.teams)sel.append(new Option('['+t.tag+'] '+t.name+' ('+t.members+')',t.tag));
 sel.value=picks[p.steam_id]!==undefined?picks[p.steam_id]:(d.team?d.team.tag:'');sel.onchange=()=>{picks[p.steam_id]=sel.value};
 const go=el('button',0,'Team setzen');go.onclick=()=>setPlayerTeam(p,sel.value);
 const tr=el('div','crow');tr.append(el('span','mut','Team'),sel,go);
 if(d.team)tr.append(el('span','mut','aktuell: ['+d.team.tag+'] '+d.team.name+' · '+d.team.role));
 box.append(top,steamRow(p.steam_id),tr);
 for(const league of ['unranked','ranked']){const st=d.stats[league],h=el('h4',0,league=='ranked'?'Ranked':'Unranked');box.append(h);
  const none=!Object.keys(st.cells).length;if(!st.exists||none){box.append(el('div','mut',st.exists?'Noch keine Punkte.':'Keine Datenbank.'));continue}
  const w=el('div','tablewrap');w.append(statsTable(st));box.append(w)}
 return box}
function draw(){const q=$('q').value.trim().toLowerCase(),real=document.querySelector('#plist tbody'),tb=document.createElement('tbody');
 const rows=people.filter(p=>!q||(p.name+' '+p.steam_id+' '+(p.team||'')).toLowerCase().includes(q)).sort(cmp);
 for(const th of document.querySelectorAll('#plist th[data-k]'))th.textContent=({name:'Spieler',team:'Team',server:'Server'})[th.dataset.k]+(th.dataset.k==sortK?(sortD>0?' ▲':' ▼'):'');
 document.querySelector('#modsec h2').firstChild.textContent='Moderator · '+people.length+' online ';
 const online=new Set(people.map(p=>p.steam_id)),offAll=q.length<2?[]:known.filter(o=>!online.has(o.steam_id)&&(o.name+' '+o.steam_id+' '+(o.team||'')).toLowerCase().includes(q)),off=offAll.slice(0,offShown);
 if(!rows.length&&!off.length){const tr=el('tr'),td=el('td','mut',people.length?'Kein Treffer.':'Niemand online.');td.colSpan=4;tr.append(td);tb.append(tr);put(real,[...tb.childNodes]);return}
 const detail=p=>{const tr=el('tr'),td=el('td');td.colSpan=4;td.append(profileBox(p));tr.append(td);return tr};
 for(const p of rows){const tr=el('tr'),n=el('td'),tm=el('td','mut',p.team||''),s=el('td','mut'),a=el('td'),w=el('div','act');
  n.append(el('div',0,p.name),el('div','mut',p.steam_id));s.textContent=p.server+(p.slot||'')+' '+p.role;
  const base={server:p.server,slot:p.slot,player:p.name,steam_id:p.steam_id};
  const add=(l,f)=>{const b=el('button',0,l);b.onclick=()=>f(b);w.append(b)};
  add(opened.has(pk(p))?'Details ▲':'Details',()=>toggleProfile(p));
  add('Nachricht',()=>act({...base,action:'msg',text:$('mtext').value}));
  add('Kick',b=>act({...base,action:'kick'},b));
  add('Ban',b=>act({...base,action:'ban',text:$('mtext').value},b));
  if(tpOn)add('Teleport',()=>act({...base,action:'tp',target:$('mtext').value}));
  a.append(w);tr.append(n,tm,s,a);tb.append(tr);if(opened.has(pk(p)))tb.append(detail(p))}
 if(off.length){const tr=el('tr'),td=el('td','mut','Nicht online · '+offAll.length+' Treffer (aus der Suche)');td.colSpan=4;tr.append(td);tb.append(tr)}
 for(const p of off){const tr=el('tr'),n=el('td'),a=el('td'),b=el('button',0,opened.has(pk(p))?'Details ▲':'Details');
  n.append(el('div',0,p.name),el('div','mut',p.steam_id));b.onclick=()=>toggleProfile(p);a.append(b);
  tr.append(n,el('td','mut',p.team||''),el('td','mut',p.seen?'zuletzt '+new Date(p.seen*1000).toLocaleString('de-DE',{dateStyle:'short',timeStyle:'short'}):'offline'),a);tb.append(tr);if(opened.has(pk(p)))tb.append(detail(p))}
 if(offAll.length>off.length){const tr=el('tr'),td=el('td');td.colSpan=4;const b=el('button',0,'Mehr anzeigen ('+(offAll.length-off.length)+')');b.onclick=()=>{offShown+=200;draw()};td.append(b);tr.append(td);tb.append(tr)}
 put(real,[...tb.childNodes])}
for(const th of document.querySelectorAll('#plist th[data-k]'))th.onclick=()=>{if(sortK==th.dataset.k)sortD=-sortD;else{sortK=th.dataset.k;sortD=1}draw()};
$('q').oninput=()=>{offShown=100;draw()};$('find').onsubmit=e=>e.preventDefault();
for(const b of document.querySelectorAll('#srvtools button'))b.onclick=()=>act({server:$('msrv').value,action:b.dataset.a,text:$('stext').value}).then(()=>{$('stext').value=''});
$('cat').onchange=()=>{const v=$('cat').value;if(!v)return;$('cmd').value=v;$('cmd').focus();const i=v.search(/[<\\[]/);if(i>0)$('cmd').setSelectionRange(i,v.length);$('cat').value=''};
let cmdNames=null;
const cmdList=()=>cmdNames||(cmdNames=[...new Set(Object.values(CATALOG).flat().map(x=>x[0].replace(/\\s*[<\\[].*$/,'')))].sort());
$('cmd').onkeydown=e=>{const i=$('cmd');
 if(e.key=='Enter'){const v=i.value;i.value='';sendCmd(v)}
 else if(e.key=='ArrowUp'){e.preventDefault();if(thi>0)i.value=thist[--thi]}
 else if(e.key=='ArrowDown'){e.preventDefault();thi=Math.min(thi+1,thist.length);i.value=thist[thi]||''}
 else if(e.key=='Tab'){e.preventDefault();const v=i.value,hit=cmdList().filter(n=>n.startsWith(v));if(!hit.length)return;
  let p=hit[0];for(const n of hit)while(!n.startsWith(p))p=p.slice(0,-1);if(p.length>v.length)i.value=p;else if(hit.length>1)tprint(hit.join('   '))}};
$('term').onclick=e=>{if(!getSelection().toString())$('cmd').focus()};
$('termclear').onclick=()=>{TL.length=0;tdraw()};
for(const b of document.querySelectorAll('[data-c]'))b.onclick=()=>{const c=b.dataset.c;if(c=='map-pool clear')return armed(b,()=>sendCmd(c).then(()=>cmdMaps(true)));sendCmd(c)};
const HDR={'Content-Type':'application/json','X-Requested-With':'dashboard'};
const post=(u,b)=>fetch(u,{method:'POST',headers:HDR,body:JSON.stringify(b)}).then(async r=>{const d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(d.error||r.status);return d});
const sleep=ms=>new Promise(x=>setTimeout(x,ms));
async function tsAdd(r,pool,note,btns){
 btns.forEach(b=>b.disabled=true);const sc=$('tsrv').value,srvs=sc=='*'?[...$('csrv').options].map(x=>x.value):[sc];
 try{note.textContent='Schreibe Settings …';await post('/api/addmap',{server:sc,package:r.package,pool:''});
  note.textContent='Lade das Paket und verlinke es auf '+srvs.length+' Server, das kann eine Minute dauern …';
  const d=await post('/api/installmap',{server:sc,package:r.package,version:r.version});
  const names=d.names&&d.names.length?d.names:[r.name];let done='Auf '+srvs.length+' Server geladen.';
  if(pool){await loadPools();const seen=new Set();
   for(const sv of srvs){const cur=POOLS&&POOLS.assign[sv];if(!cur||cur=='Alle Maps'||seen.has(cur))continue;seen.add(cur);
    for(const n of names)await post('/api/pools',{action:'set',server:sv,name:cur,map:n,on:true})}
   done=seen.size?'Im Pool '+[...seen].join(', ')+': '+names.join(', ')+'.':'Geladen. Die Server mit „Alle Maps" haben die Map automatisch im Pool.'}
  r.stored='current';r.on=[...new Set([...(r.on||[]),...srvs])];if(r.dot){r.dot.className='dot current';r.dot.title=dotTip(r)}
  note.textContent=done}
 catch(e){note.textContent='Fehler: '+e.message}
 btns.forEach(b=>b.disabled=false);for(const k of Object.keys(mapsCache))delete mapsCache[k];await loadPools();mapsAt=0;cmdMaps(true).catch(()=>{})}
const dotTip=r=>(r.stored=='current'?'Heruntergeladen (diese Version liegt im Store)':r.stored=='old'?'Eine andere Version liegt im Store':'Nicht heruntergeladen')+(r.on&&r.on.length?' · verlinkt auf Server '+r.on.join(', '):'');
const tsRun=async()=>{const real=$('tsres'),box=document.createElement('div'),info=$('tsinfo');info.textContent='Suche …';
 let d;try{d=await api('/api/ts?q='+encodeURIComponent($('tsq').value)+'&sort='+$('tssort').value+'&dir='+$('tsdir').value+($('tsall').checked?'&all=1':''))}catch(x){info.textContent='Thunderstore nicht erreichbar: '+x.message;return}
 info.textContent=($('tsq').value.trim()?d.total+' Treffer':d.total+' Maps auf Thunderstore')+(d.total>d.results.length?' (die ersten '+d.results.length+' nach Sortierung)':'');
 for(const r of d.results){const row=el('div','tsrow'),pic=el('div','tspic');pic.append(el('span',0,r.name.slice(0,2)));
  if(r.icon){const im=el('img','mapimg');im.alt='';im.loading='lazy';im.onerror=()=>im.remove();im.src='/api/tsimg?u='+encodeURIComponent(r.icon);pic.append(im)}
  const main=el('div','tsmain'),a=el('a',0,r.name);a.href=r.url;a.target='_blank';a.rel='noopener noreferrer';
  const t=el('div','tsname'),dot=el('span','dot '+(r.stored||'none'));dot.title=dotTip(r);r.dot=dot;t.append(dot,a,el('span','mut',' '+r.owner+' · v'+r.version));
  const note=el('div','mut');main.append(t,el('div','mut',r.description),note);
  const st=el('div','tsstat');st.append(el('div',0,r.downloads.toLocaleString('de-DE')+' ↓'),el('div','mut','★ '+r.rating),el('div','mut','neu '+(r.created||'?')),el('div','mut','Update '+r.updated));
  const w=el('div','act'),b1=el('button',0,'Auf Server'),b2=el('button',0,'In den Pool');w.append(b1,b2);
  b1.onclick=()=>tsAdd(r,false,note,[b1,b2]);b2.onclick=()=>tsAdd(r,true,note,[b1,b2]);
  row.append(pic,main,st,w);box.append(row)}
 put(real,[...box.childNodes])};
$('tsform').onsubmit=e=>{e.preventDefault();tsRun()};$('tssort').onchange=$('tsdir').onchange=tsRun;
const mapsCache={};
function chipRow(list,cur,mod){const box=el('div','chips');
 for(const n of list){let t=n,tip='';if(mod){const m=n.match(/^(.*)-(\\d+[.]\\d+[.]\\d+)$/);t=m?m[1]:n;tip=n}
  const ch=el('span','chip'+(mod?' mod':'')+(!mod&&cur&&n.toLowerCase()==cur.toLowerCase()?' on':''),t);if(tip)ch.title=tip;box.append(ch)}
 return box}
let mapsAt=0;
async function poolmaps(){if(view!='befehle'||Date.now()-mapsAt<20000)return;mapsAt=Date.now();
 const [d,m]=await Promise.all([api('/api/servers'),api('/api/maps')]),real=$('poolbox'),box=document.createElement('div');
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
 if(!box.children.length)box.append(el('div','mut','Keine Server-Daten (Docker-Zugriff nötig).'));
 put(real,[...box.childNodes])}
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

let ANN=null;
const annErr=e=>{tprint('Fehler: '+e.message)};
async function annLoad(){try{ANN=await api('/api/ann');annDraw()}catch(e){annErr(e)}}
const annPost=b=>post('/api/ann',b).then(d=>{ANN=d;annDraw();if(d.errors&&d.errors.length)alert_(d.errors.join(' · '))}).catch(annErr);
const alert_=t=>{$('annlive').textContent=t};
function annRows(scope,list){const box=document.createElement('div');
 list.forEach((m,i)=>{const row=el('div','annrow'),lb=el('label','tgl'+(m.on?' on':'')),cb=document.createElement('input');cb.type='checkbox';cb.checked=m.on;
  cb.onchange=()=>annPost({action:'edit',scope,index:i,on:cb.checked});lb.append(cb,el('span','sw'));
  const t=document.createElement('input');t.value=m.text;t.maxLength=220;t.onchange=()=>annPost({action:'edit',scope,index:i,text:t.value});
  const num=(key,ph,tip)=>{const n=document.createElement('input');n.type='number';n.min=0;n.max=1440;n.className='annnum';n.placeholder=ph;n.title=tip;n.value=m[key]||'';n.onchange=()=>annPost({action:'edit',scope,index:i,[key]:+n.value||0});return n};
  const ev=num('every','Std.','Eigener Takt in Minuten (leer = Standard-Intervall des Servers)'),of=num('offset','0','Versatz in Minuten: wann im Ablauf diese Ansage zuerst kommt, damit sie nicht gleichzeitig mit anderen läuft');
  const up=el('button',0,'↑'),dn=el('button',0,'↓'),del=el('button',0,'✕');up.disabled=i==0;dn.disabled=i==list.length-1;
  up.onclick=()=>annPost({action:'move',scope,index:i,dir:'up'});dn.onclick=()=>annPost({action:'move',scope,index:i,dir:'down'});del.onclick=()=>armed(del,()=>annPost({action:'delete',scope,index:i}));
  row.append(lb,t,el('span','mut','alle'),ev,el('span','mut','Min · Versatz'),of,up,dn,del);box.append(row)});
 if(!list.length)box.append(el('div','mut','Noch keine Ansagen.'));return [...box.childNodes]}
function annDraw(){if(!ANN)return;const sel=$('annsrv'),nums=Object.keys(ANN.servers).sort((a,b)=>a-b),keep=sel.value,sig=JSON.stringify(nums.map(n=>[n,ANN.roles[n]]));
 if(sel.dataset.sig!=sig){sel.dataset.sig=sig;sel.replaceChildren(...nums.map(n=>new Option(n+' '+(ANN.roles[n]||''),n)))}
 if(nums.includes(keep))sel.value=keep;const sv=sel.value;
 put($('annglobal'),annRows('global',ANN.global));
 if(!sv)return;const S=ANN.servers[sv],live=ANN.live[sv],plan=ANN.plans[sv],eff=plan.messages;
 put($('annserver'),annRows(sv,S.messages));
 if(document.activeElement!==$('anniv-server'))$('anniv-server').value=S.interval||(live?live.interval:'')||'';
 $('annstate').textContent='Auf diesem Server laufen '+(ANN.global.filter(m=>m.on).length+S.messages.filter(m=>m.on).length)+' Ansagen ('+ANN.global.filter(m=>m.on).length+' von allen + '+S.messages.filter(m=>m.on).length+' eigene). Der Server zeigt alle '+(plan.interval||(live?live.interval:'?'))+' Minuten eine Ansage reihum'+(new Set(eff).size<eff.length?' (Takte als Ablauf mit '+eff.length+' Plätzen eingebaut)':'')+'.'+(plan.error?' Achtung: '+plan.error+'.':plan.deviation>0.02?' Die Takte passen nicht ganz ineinander (Abweichung bis '+Math.round(plan.deviation*100)+' %, weil der Server nur ganze Minuten kann).':'');
 const same=live&&JSON.stringify(live.messages)==JSON.stringify(eff)&&(!S.interval||S.interval==live.interval);
 $('annlive').textContent=live?(same?'Der laufende Server stimmt überein.':'Der laufende Server weicht ab (geändert per Konsole?). Mit „Neu anwenden“ gleichst du ihn an.'):'Server nicht lesbar.'}
$('annsrv').onchange=annDraw;
for(const k of ['global','server'])$('annadd-'+k).onclick=()=>{const t=$('annnew-'+k).value.trim();if(!t)return;$('annnew-'+k).value='';annPost({action:'add',scope:k=='global'?'global':$('annsrv').value,text:t})};
for(const k of ['global','server'])$('annnew-'+k).onkeydown=e=>{if(e.key=='Enter')$('annadd-'+k).click()};
$('annivgo-all').onclick=()=>armed($('annivgo-all'),()=>annPost({action:'interval',server:'*',minutes:+$('anniv-all').value}));
$('annivgo-server').onclick=()=>annPost({action:'interval',server:$('annsrv').value,minutes:+$('anniv-server').value});
$('annre').onclick=()=>annPost({action:'reapply',scope:$('annsrv').value});
let LB={league:'unranked',board:'general',data:null};
const lbFmt=t=>t?new Date(t*1000).toLocaleString('de-DE',{dateStyle:'short',timeStyle:'short'}):'-';
async function lbLoad(){try{LB.data=await api('/api/leaderboard?league='+LB.league+'&board='+encodeURIComponent(LB.board));LB.board=LB.data.board;lbDraw()}catch(e){$('lbstats').textContent='Fehler: '+(e.message||e)}}
function lbDraw(){const d=LB.data;if(!d)return;
 const lg=$('lbleague');lg.textContent='';
 for(const [k,l] of [['unranked','Unranked · Server 1–5'],['ranked','Ranked · Server 6']]){const b=el('button',k==LB.league?'on':'',l);b.onclick=()=>{LB.league=k;LB.board='general';lbLoad()};lg.append(b)}
 const base=LB.board.split(':')[0],mode=LB.board.split(':')[1]||'';
 const periods=d.boards.filter(b=>!b.mode),sel=$('lbboard');sel.textContent='';
 const KN={general:'Gesamt',year:'Jahr',month:'Monat',week:'Woche',event:'Event'};let grp=null,gk='';
 for(const b of periods){if(b.kind!=gk){gk=b.kind;grp=document.createElement('optgroup');grp.label=KN[gk]||gk;sel.append(grp)}
  const o=el('option',0,b.name+(b.closed_at?' (beendet)':'')+' · '+b.players);o.value=b.id;if(b.id==base)o.selected=true;grp.append(o)}
 const ms=$('lbmode');ms.textContent='';const all=el('option',0,'Alle Modi');all.value='';ms.append(all);
 const bn=(periods.find(b=>b.id==base)||{name:''}).name;
 for(const b of d.boards.filter(b=>b.base==base&&b.mode)){const o=el('option',0,b.name.replace(bn+' - ','')+' · '+b.players);o.value=b.mode;if(b.mode==mode)o.selected=true;ms.append(o)}
 const q=$('lbfind').value.trim().toLowerCase();
 $('lbstats').textContent=d.exists?d.total+' Spieler mit Punkten auf diesem Board · '+(d.stats.players||0)+' Spieler insgesamt · '+(d.stats.awards||0)+' Vergaben · letzte Vergabe '+lbFmt(d.stats.last):'Für diese Liga gibt es noch keine Datenbank.';
 const t=$('lbtable');t.textContent='';const hr=el('tr');
 for(const [l,n] of [['#',0],['Spieler',0],['Punkte',1],['Siege',1],['Spiele',1]]){const h=el('th',n?'lbn':'',l);hr.append(h)}t.append(hr);
 const top=d.rows.length?d.rows[0].points:1;let shown=0;
 for(const r of d.rows){if(q&&!(r.name+' '+r.tag).toLowerCase().includes(q))continue;shown++;
  const tr=el('tr'),nm=el('td'),pt=el('td','lbn',r.points);
  nm.title=r.steam_id;if(r.tag)nm.append(el('span','tag','['+r.tag+']'));nm.append(r.name);
  const bar=el('div','lbbar');bar.style.width=Math.max(2,Math.round(100*r.points/top))+'%';nm.append(bar);
  tr.append(el('td','rk'+(r.rank<4?' r'+r.rank:''),r.rank),nm,pt,el('td','lbn',r.wins),el('td','lbn',r.played));t.append(tr)}
 if(!shown){const tr=el('tr'),td=el('td','mut','Keine Einträge.');td.colSpan=5;tr.append(td);t.append(tr)}
 else if(!q&&d.total>d.rows.length){const tr=el('tr'),td=el('td','mut','… und '+(d.total-d.rows.length)+' weitere');td.colSpan=5;tr.append(td);t.append(tr)}
 const tt=$('lbteams');tt.textContent='';const th=el('tr');for(const [l,n] of [['Team',0],['Mitglieder',1],['Punkte',1]])th.append(el('th',n?'lbn':'',l));tt.append(th);
 for(const x of d.teams){if(q&&!(x.name+' '+x.tag).toLowerCase().includes(q))continue;const tr=el('tr'),c=el('td');c.append(el('span','tag','['+x.tag+']'),x.name);tr.append(c,el('td','lbn',x.members),el('td','lbn',x.points));tt.append(tr)}
 if(!d.teams.length){const tr=el('tr'),td=el('td','mut','Noch keine Team-Punkte auf diesem Board.');td.colSpan=3;tr.append(td);tt.append(tr)}
 const rc=$('lbrecent');rc.textContent='';const rh=el('tr');for(const [l,n] of [['Letzte Vergaben',0],['Punkte',1]])rh.append(el('th',n?'lbn':'',l));rc.append(rh);
 for(const r of d.recent){const tr=el('tr'),c=el('td');c.append(r.name||'?',el('div','mut',lbFmt(r.at)+' · '+r.reason+(r.server?' · '+(/YARD ([0-9]+)/.exec(r.server)?'Server '+/YARD ([0-9]+)/.exec(r.server)[1]:r.server):'')));tr.append(c,el('td','lbn',(r.points>0?'+':'')+r.points));rc.append(tr)}
 if(!d.recent.length){const tr=el('tr'),td=el('td','mut','Noch nichts vergeben.');td.colSpan=2;tr.append(td);rc.append(tr)}}
$('lbboard').onchange=()=>{LB.board=$('lbboard').value;lbLoad()};
$('lbmode').onchange=()=>{const b=$('lbboard').value,m=$('lbmode').value;LB.board=m?b+':'+m:b;lbLoad()};
$('lbfind').oninput=lbDraw;$('lbre').onclick=lbLoad;
let TM=null;const openTeams=new Set();
const tmSay=(t,bad)=>{const e=$('tmstat');e.textContent=t;e.className=bad?'bad':'mut'};
async function tmLoad(){try{TM=await api('/api/teams');tmDraw()}catch(e){tmSay('Fehler: '+(e.message||e),true)}}
const tmPost=b=>post('/api/team',b).then(d=>{tmSay(d.text);return tmLoad()}).catch(e=>tmSay('Fehler: '+e.message,true));
function tmDraw(){const d=TM;if(!d)return;
 $('tmcount').textContent=d.exists?'('+d.teams.length+')':'';
 const apps=$('tmapps');apps.textContent='';
 if(!d.exists){apps.append(el('div','mut','Es gibt noch keine Teams-Datenbank. Teams laufen über den Discord-Bot und die Bridge (TEAMS=true), siehe /shared/teams.db.'))}
 for(const a of d.pending){const r=el('div','tmapp'),w=el('div','who');w.append(el('span','tmtag','['+a.tag+']'),' '+a.name,el('div','mut','von '+a.applicant+' · '+lbFmt(a.created)));
  const ok=el('button',0,'Annehmen'),no=el('button',0,'Ablehnen');ok.onclick=()=>armed(ok,()=>tmPost({action:'decide',id:a.id,accept:true}));no.onclick=()=>armed(no,()=>tmPost({action:'decide',id:a.id,accept:false}));
  r.append(w,ok,no);apps.append(r)}
 if(d.exists&&!d.pending.length)apps.append(el('div','mut','Keine offenen Bewerbungen.'));
 if(d.recent.length){const m=el('div','mut','Zuletzt entschieden: '),st={accepted:'angenommen',denied:'abgelehnt',cancelled:'zurückgezogen'};m.append(d.recent.map(a=>'['+a.tag+'] '+a.name+' ('+(st[a.status]||a.status)+')').join(' · '));apps.append(m)}
 const box=document.createElement('div');
 for(const t of d.teams){const open=openTeams.has(t.tag),c=el('div','tm'+(open?' open':'')),h=el('div','tmhead');
  h.append(el('span','tmtag','['+t.tag+']'),el('h3',0,t.name),pill(t.open?'offen':'nur Einladung',t.open?'ok':'warn'));
  if(t.league)h.append(pill(t.league=='both'?'beide Ligen':t.league=='ranked'?'Ranked':'Unranked','acc'));if(t.category)h.append(pill(t.category,'mut'));
  const pts=el('div','tmpts');for(const [l,n] of [['unranked','Unranked'],['ranked','Ranked']]){const x=el('div');x.append(el('b',0,t.points[l]),el('div','mut',n+' · Gesamt'));pts.append(x)}
  const mm=el('div');mm.append(el('b',0,t.members.length),el('div','mut','Mitglieder'+(t.invites?' · '+t.invites+' eingeladen':'')));pts.append(mm);
  c.append(h);if(t.motto)c.append(el('div','mut','„'+t.motto+'“'));c.append(pts);
  const more=el('button',0,open?'Schließen ▴':'Verwalten ▾');more.onclick=()=>{if(openTeams.has(t.tag))openTeams.delete(t.tag);else openTeams.add(t.tag);tmDraw()};c.append(more);
  if(open)c.append(tmManage(t));box.append(c)}
 if(d.exists&&!d.teams.length)box.append(el('div','mut','Noch keine Teams. Bewerbungen kommen aus dem Spiel mit /team apply.'));
 $('tmlist').replaceChildren(...box.childNodes)}
function tmManage(t){const w=el('div');w.style.display='grid';w.style.gap='12px';
 const ed=el('div','tmedit'),field=(label,key,val,ph)=>{const r=el('div','crow'),i=document.createElement('input');i.value=val||'';i.placeholder=ph||'';i.maxLength=key=='description'?300:60;const b=el('button',0,'Speichern');b.onclick=()=>tmPost({action:'set',team:t.tag,key,value:i.value});r.append(el('span','mut',label),i,b);return r};
 ed.append(field('Motto','motto',t.motto),field('Beschreibung','description',t.description),field('Kategorie','category',t.category,'z. B. Casual'),field('Tag','tag',t.tag),field('Logo-Link','logo',t.logo,'https://…'));
 const lr=el('div','crow'),ls=document.createElement('select');for(const [v,l] of [['','keine Angabe'],['ranked','Ranked'],['unranked','Unranked'],['both','beide']])ls.add(new Option(l,v));ls.value=t.league||'';ls.onchange=()=>tmPost({action:'set',team:t.tag,key:'league',value:ls.value});lr.append(el('span','mut','Liga'),ls);
 const or=el('div','crow'),ob=el('button',0,t.open?'Auf „nur Einladung“ stellen':'Für alle öffnen');ob.onclick=()=>tmPost({action:'set',team:t.tag,key:'open',value:t.open?'no':'yes'});or.append(el('span','mut','Beitritt'),ob);
 ed.append(lr,or);w.append(ed);
 const tw=el('div','tablewrap'),tb=el('table'),hr=el('tr');for(const [l,n] of [['Mitglied',0],['Rolle',0],['Unranked',1],['Ranked',1],['',0]])hr.append(el('th',n?'lbn':'',l));tb.append(hr);
 const RN={owner:'Besitzer',leader:'Leader',member:'Mitglied'};
 for(const m of t.members){const tr=el('tr'),nm=el('td',0,m.name);nm.title=m.steam_id;const act=el('td'),box=el('div','act');
  if(m.role!='owner'){const ld=el('button',0,m.role=='leader'?'Zu Mitglied':'Zu Leader'),ow=el('button',0,'Zum Besitzer'),kk=el('button',0,'Entfernen');
   ld.onclick=()=>tmPost({action:'leader',team:t.tag,player:m.steam_id});ow.onclick=()=>armed(ow,()=>tmPost({action:'owner',team:t.tag,player:m.steam_id}));kk.onclick=()=>armed(kk,()=>tmPost({action:'kick',team:t.tag,player:m.steam_id}));box.append(ld,ow,kk)}
  act.append(box);tr.append(nm,el('td',0,RN[m.role]||m.role),el('td','lbn',m.points.unranked),el('td','lbn',m.points.ranked),act);tb.append(tr)}
 tw.append(tb);w.append(tw);
 const dr=el('div','crow'),db=el('button','danger','Team auflösen');db.onclick=()=>armed(db,()=>tmPost({action:'disband',team:t.tag}).then(()=>openTeams.delete(t.tag)));dr.append(db,el('span','mut','Entfernt das Team für alle Mitglieder. Änderungen laufen wie vom Besitzer ausgeführt, die Spieler bekommen die Meldung im Spiel.'));w.append(dr);return w}
const VIEWS=[['uebersicht','Übersicht'],['chat','Chat'],['moderator','Moderator'],['befehle','Befehle & Maps'],['logs','Logs'],['ansagen','Ansagen'],['rangliste','Rangliste'],['teams','Teams']];
let tsLoaded=false;
let view='uebersicht';
try{const h=location.hash.slice(1),v=localStorage.getItem('rv');if(VIEWS.some(x=>x[0]==h))view=h;else if(VIEWS.some(x=>x[0]==v))view=v}catch(e){}
function show(v){view=v;mapsAt=0;if(v=='ansagen')annLoad();if(v=='rangliste')lbLoad();if(v=='teams')tmLoad();if(v=='befehle'){if(!tsLoaded){tsLoaded=true;setTimeout(()=>tsRun(),0)}setTimeout(()=>cmdMaps(),0)}try{localStorage.setItem('rv',v)}catch(e){}try{history.replaceState(null,'','#'+v)}catch(e){}
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

LOGIN = """<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="darkreader-lock">
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
                    found = ts_search(q.get("q", ""), q.get("all") != "1", q.get("sort", "downloads"), q.get("dir") == "asc")
                    found["results"] = ts_state(found["results"])
                    return self.json(200, found)
                except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as e:
                    return self.json(502, {"error": f"Thunderstore: {type(e).__name__}: {e}"})
            if url.path == "/api/tsimg":
                kind, data = ts_image(q.get("u", ""))
                return self.send(200, data, kind, headers=[("Cache-Control", "private, max-age=86400")])
            if url.path == "/api/mapname":
                return self.json(200, {"names": store_names(q.get("package", ""))})
            if url.path == "/api/maps":
                return self.json(200, server_maps(q.get("fresh") == "1", q.get("server") or None))
            if url.path == "/api/pools":
                with POOLS_LOCK:
                    return self.json(200, pools_snapshot(*pools_state()))
            if url.path == "/api/ann":
                with ANN_LOCK:
                    return self.json(200, ann_snapshot(ann_load()))
            if url.path == "/api/teams":
                return self.json(200, teams_snapshot())
            if url.path == "/api/leaderboard":
                return self.json(200, lb_snapshot(q.get("league", "unranked"), q.get("board", "general"), q.get("n", "100")))
            if url.path == "/api/serverinfo":
                return self.json(200, server_info(q.get("server", "")))
            if url.path == "/api/secret":
                if self.headers.get("X-Requested-With") != "dashboard" or not MOD:
                    return self.json(403, {"error": "forbidden"})
                say(f"dashboard: password of server {q.get('server', '')} shown")
                return self.json(200, server_secret(q.get("server", "")))
            if url.path == "/api/players":
                return self.json(200, players() if MOD else [])
            if url.path == "/api/player":
                if not MOD:
                    return self.json(403, {"error": "forbidden"})
                return self.json(200, player_profile(q.get("steam_id", ""), q.get("name", "")))
            if url.path == "/api/steam":
                if not MOD:
                    return self.json(403, {"error": "forbidden"})
                return self.json(200, steam_info(q.get("steam_id", "")))
            if url.path == "/api/knownplayers":
                return self.json(200, players_known() if MOD else [])
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

    def installmap(self):
        if not self.logged_in():
            return self.json(401, {"error": "login"})
        if not MOD or self.headers.get("X-Requested-With") != "dashboard":
            return self.json(403, {"error": "moderation is off" if not MOD else "forbidden"})
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 4096)) or b"{}")
            result = install_package(body)
        except (ValueError, TypeError, AttributeError, zipfile.BadZipFile) as e:
            return self.json(400, {"error": str(e)})
        except (RuntimeError, OSError) as e:
            return self.json(502, {"error": f"{type(e).__name__}: {e}"})
        say(f"dashboard: {result['text']}")
        self.json(200, result)

    def layers(self):
        if not self.logged_in():
            return self.json(401, {"error": "login"})
        if not MOD or self.headers.get("X-Requested-With") != "dashboard":
            return self.json(403, {"error": "moderation is off" if not MOD else "forbidden"})
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), LAYERS_MAX + 4096)) or b"{}")
            text = upload_layers(str(body.get("server", "")), str(body.get("content", "")))
        except (ValueError, TypeError, AttributeError) as e:
            return self.json(400, {"error": str(e)})
        except (RuntimeError, OSError) as e:
            return self.json(502, {"error": f"docker: {e}"})
        say(f"dashboard: {text}")
        self.json(200, {"ok": True, "text": text})

    def pools(self):
        if not self.logged_in():
            return self.json(401, {"error": "login"})
        if not MOD or self.headers.get("X-Requested-With") != "dashboard":
            return self.json(403, {"error": "moderation is off" if not MOD else "forbidden"})
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 8192)) or b"{}")
            result = pools_action(body)
        except (ValueError, TypeError, AttributeError) as e:
            return self.json(400, {"error": str(e)})
        except (RuntimeError, OSError) as e:
            return self.json(502, {"error": str(e)})
        say(f"dashboard: pools {body.get('action')} {body.get('name', '')} server {body.get('server', '')}")
        self.json(200, result)

    def ann(self):
        if not self.logged_in():
            return self.json(401, {"error": "login"})
        if not MOD or self.headers.get("X-Requested-With") != "dashboard":
            return self.json(403, {"error": "moderation is off" if not MOD else "forbidden"})
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 8192)) or b"{}")
            result = ann_action(body)
        except (ValueError, TypeError, AttributeError) as e:
            return self.json(400, {"error": str(e)})
        except (RuntimeError, OSError) as e:
            return self.json(502, {"error": str(e)})
        say(f"dashboard: announcements {body.get('action')} {body.get('scope', body.get('server', ''))}")
        self.json(200, result)

    def team(self):
        if not self.logged_in():
            return self.json(401, {"error": "login"})
        if not MOD or self.headers.get("X-Requested-With") != "dashboard":
            return self.json(403, {"error": "moderation is off" if not MOD else "forbidden"})
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 2048)) or b"{}")
            text = team_action(body)
        except (ValueError, TypeError, AttributeError) as e:
            return self.json(400, {"error": str(e)})
        except urllib.error.HTTPError as e:
            return self.json(502, {"error": f"bridge: {e.code} {e.read()[:200].decode(errors='replace')}"})
        except (RuntimeError, OSError, urllib.error.URLError) as e:
            return self.json(502, {"error": str(e)})
        say(f"dashboard: team {body.get('action')} {body.get('team', body.get('id', ''))} {body.get('key', body.get('player', ''))}")
        self.json(200, {"ok": True, "text": text})

    def playerteam(self):
        if not self.logged_in():
            return self.json(401, {"error": "login"})
        if not MOD or self.headers.get("X-Requested-With") != "dashboard":
            return self.json(403, {"error": "moderation is off" if not MOD else "forbidden"})
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 2048)) or b"{}")
            text = player_team(body)
        except (ValueError, TypeError, AttributeError) as e:
            return self.json(400, {"error": str(e)})
        except urllib.error.HTTPError as e:
            return self.json(502, {"error": f"bridge: {e.code} {e.read()[:200].decode(errors='replace')}"})
        except (RuntimeError, OSError, urllib.error.URLError) as e:
            return self.json(502, {"error": str(e)})
        say(f"dashboard: player {body.get('steam_id')} team '{body.get('team', '')}'")
        self.json(200, {"ok": True, "text": text})

    def option(self):
        if not self.logged_in():
            return self.json(401, {"error": "login"})
        if not MOD or self.headers.get("X-Requested-With") != "dashboard":
            return self.json(403, {"error": "moderation is off" if not MOD else "forbidden"})
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 2048)) or b"{}")
            result = server_option(body)
        except (ValueError, TypeError, AttributeError) as e:
            return self.json(400, {"error": str(e)})
        except urllib.error.HTTPError as e:
            return self.json(502, {"error": f"bridge: {e.code} {e.read()[:200].decode(errors='replace')}"})
        except (RuntimeError, OSError, urllib.error.URLError) as e:
            return self.json(502, {"error": str(e)})
        say(f"dashboard: option {body.get('action')} server {body.get('server')}")
        self.json(200, result)

    def restart(self):
        if not self.logged_in():
            return self.json(401, {"error": "login"})
        if not MOD or self.headers.get("X-Requested-With") != "dashboard":
            return self.json(403, {"error": "moderation is off" if not MOD else "forbidden"})
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 2048)) or b"{}")
            result = restart_server(body)
        except (ValueError, TypeError, AttributeError) as e:
            return self.json(409, {"error": str(e)})
        except (RuntimeError, OSError, urllib.error.URLError) as e:
            return self.json(502, {"error": str(e)})
        say(f"dashboard: restart server {body.get('server')} ({result['container']}, {result['players']} players"
            f"{', FORCED over the player limit' if result['forced'] else ''})")
        self.json(200, result)

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
        if path == "/api/layers":
            return self.layers()
        if path == "/api/installmap":
            return self.installmap()
        if path == "/api/pools":
            return self.pools()
        if path == "/api/ann":
            return self.ann()
        if path == "/api/restart":
            return self.restart()
        if path == "/api/option":
            return self.option()
        if path == "/api/team":
            return self.team()
        if path == "/api/playerteam":
            return self.playerteam()
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
