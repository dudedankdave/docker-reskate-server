"""Brings a second container up when a ReSkate release comes out while players are on the old one.

Each server has two slots, A and B, each with its own ports, Steam token and /data volume. One of
them runs the server. When a newer ReSkate release is out:

* nobody on the running server: it is replaced in its own slot (same token, so the same Steam ID).
* players on it: a copy on the new version starts in the other slot. With fewer than
  copy_min_players on, it first waits up to copy_wait_minutes for them to leave. The old server keeps
  running until it has been empty for DRAIN_GRACE minutes, then it is stopped (not removed,
  so it can be started again by hand).

A newer version of an unpinned MODS entry (a map) is handled the same way: nobody on -> restart
(mods install on start), players on -> a copy on the same ReSkate version in the other slot.

Settings from /shared/settings (settings_dir; see app/settings.py in the image) are read when a
server starts. When the files now say something else for a server than what it started with (the
"[settings] digest" line in its log), it is restarted once nobody is on it; nobody is kicked.
Every container this tool creates gets RESKATE_SERVER (its number), so it finds its server-<n>.env.

With image_updates on, a newer build of the image for the same ReSkate version (the tag
<version>-r<n> on Docker Hub, RESKATE_IMAGE_REVISION in the image) replaces a server in its own
slot once nobody is on it: image changes are never worth disconnecting anyone.

A restart that changes no version (settings the console cannot take, a missing mount or console, a restart request)
happens once at most restart_max_players (default 3) are on it; those players are reconnected by the
restart, nobody else is touched. restart_max_players: 0 means only when empty.

A restart can be asked for without any change: touching /shared/settings/.restart/server-<n> (the
dashboard does) restarts that server the same way, once, if the file is newer than the server's start.

The copy is a clone of the running container (env, mounts, restart policy) with the slot's
ports, token, volume and a new SERVER_NAME. If Docker Hub has an image tag for the release it
runs that; otherwise it runs the current image with the release installed into its volume
beforehand (UPDATE_MODE=auto, see updater.py in the image), so no new image is needed.

Players are counted from the server log (joins and leaves since the last "is up on"), because
the server's A2S query reports 0 players. If the log can't be read the server counts as busy.

Talks to Docker through /var/run/docker.sock; Python standard library only.
"""
import http.client
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from calendar import timegm
from datetime import datetime, timezone

sys.path[:0] = [os.path.dirname(os.path.abspath(__file__)), os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib")]
try:
    import settings          # app/settings.py, in the image's /app/tools/lib
except ImportError:
    settings = None

CONFIG = os.environ.get("CONFIG", "/config/servers.json")
SOCKET = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
DOCKER_API = os.environ.get("DOCKER_API", "")   # instead of the socket, e.g. Portainer's .../api/endpoints/3/docker
LAUNCHER = "https://github.com/Dingo-Shenanigans/ReSkate/releases/latest/download/launcher.json"
LABEL = "reskate.rollover"            # set on every container this tool creates
LINE = re.compile(r"^\[\d\d:\d\d:\d\d\] (.*)$")
JOINED = re.compile(r"^.+ joined \(\d+(?:, [a-z]+)?\), (\d+)/\d+ players, loaded in \d+ s$")
LEFT = re.compile(r"^.+ left \(.*\)( \[.*\])?$")
NETWORK = re.compile(r"^\[network\] (\d+) players, ")
UP = re.compile(r"^.+ is up on .+ for \d+ players\.$")
DIGEST = re.compile(r"^\[settings\] (digest|restart-digest) ([0-9a-f]+)$")


def say(message):
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} {message}", flush=True)


def numbers(version):
    return tuple(int(n) for n in re.findall(r"\d+", version or "")[:4])


# ---- Docker API --------------------------------------------------------------------------
class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=60):
        super().__init__("localhost", timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def docker(method, path, body=None, raw=False, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    if DOCKER_API:
        req = urllib.request.Request(DOCKER_API.rstrip("/") + "/v1.41" + path, data=data, method=method,
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                status, out = res.status, res.read()
        except urllib.error.HTTPError as e:
            status, out = e.code, e.read()
    else:
        conn = UnixConnection(SOCKET, timeout)
        try:
            conn.request(method, "/v1.41" + path, body=data, headers=headers)
            res = conn.getresponse()
            status, out = res.status, res.read()
        finally:
            conn.close()
    if status >= 400:
        raise RuntimeError(f"docker {method} {path}: {status} {out[:300].decode('utf-8', 'replace')}")
    if raw:
        return out
    return json.loads(out) if out.strip() else None


def env_of(info):
    return dict(e.split("=", 1) if "=" in e else (e, "") for e in info["Config"].get("Env") or [])


def started_at(info):
    started = info["State"].get("StartedAt", "")[:19]
    return timegm(time.strptime(started, "%Y-%m-%dT%H:%M:%S")) if started and not started.startswith("0001") else 0


def finished_at(info):
    """When the container stopped (epoch seconds), 0 if it never ran."""
    stopped = info["State"].get("FinishedAt", "")[:19]
    return timegm(time.strptime(stopped, "%Y-%m-%dT%H:%M:%S")) if stopped and not stopped.startswith("0001") else 0


LEFTOVER = re.compile(r"^/reskate-(?:bot|bot-admin|bot-chat|bridge|leaderboard|rollover|status|webhooks|settings|announcer)"
                      r"-(?:prev(?:-[A-Za-z0-9.-]+)?|old)$")


def logs(info, since=None, timestamps=False):
    """The container's output since it last started (or since `since`, Unix seconds), as text lines."""
    if since is None:
        since = max(started_at(info) - 5, 0)
    out = docker("GET", f"/containers/{info['Id']}/logs?stdout=1&stderr=1&since={since}"
                        + ("&timestamps=1" if timestamps else ""), raw=True, timeout=120)
    if not info["Config"].get("Tty"):                  # multiplexed: 8-byte frame headers
        frames, i = [], 0
        while i + 8 <= len(out):
            size = int.from_bytes(out[i + 4:i + 8], "big")
            frames.append(out[i + 8:i + 8 + size])
            i += 8 + size
        out = b"".join(frames)
    return out.decode("utf-8", "replace").replace("\r", "").split("\n")


def players(info):
    """How many players are on: the count from the last join or [network] line, minus the leaves
    after it, since the server last came up."""
    on = 0
    for line in logs(info):
        m = LINE.match(line)
        text = m.group(1) if m else line
        if UP.match(text):
            on = 0
        elif j := JOINED.match(text) or NETWORK.match(text):
            on = int(j.group(1))
        elif LEFT.match(text):
            on = max(on - 1, 0)
    return on


# ---- releases ----------------------------------------------------------------------------
def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "reskate-rollover"})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return res.read()


def latest_release():
    if os.environ.get("PRETEND_LATEST"):          # for trying a dry run against a release that isn't out
        return os.environ["PRETEND_LATEST"]
    info = json.loads(fetch(LAUNCHER).decode("utf-8-sig"))["server_linux"]
    if not re.fullmatch(r"\d+(\.\d+)+", info.get("version", "")):
        raise RuntimeError(f"launcher.json: unexpected version {info.get('version')!r}")
    return info["version"]


def hub_revision(repo, version):
    """The newest image revision on Docker Hub for a ReSkate version (<version>-r<n>), 0 if none."""
    url = (f"https://hub.docker.com/v2/repositories/{repo}/tags?page_size=100"
           f"&name={urllib.parse.quote(version + '-r')}")
    best = 0
    while url:
        page = json.loads(fetch(url, 20))
        for t in page.get("results") or []:
            if m := re.fullmatch(re.escape(version) + r"-r(\d+)", t.get("name", "")):
                best = max(best, int(m.group(1)))
        url = page.get("next")
    return best


def revision_of(env):
    """The image revision a container runs (RESKATE_IMAGE_REVISION "r12" -> 12), 0 when unknown."""
    m = re.fullmatch(r"r?(\d+)", env.get("RESKATE_IMAGE_REVISION", "").strip())
    return int(m.group(1)) if m else 0


def hub_has(repo, tag):
    try:
        fetch(f"https://hub.docker.com/v2/repositories/{repo}/tags/{urllib.parse.quote(tag)}", 20)
        return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise


# ---- servers -----------------------------------------------------------------------------
class Instance:
    """One container of a server (one slot of it)."""

    def __init__(self, info, slot):
        self.info, self.slot = info, slot
        self.env = env_of(info)
        self.name = info["Name"].lstrip("/")
        labels = info["Config"].get("Labels") or {}
        self.version = labels.get("reskate.version") or self.env.get("RESKATE_IMAGE_VERSION", "")
        self.running = info["State"].get("Running", False)
        health = (info["State"].get("Health") or {}).get("Status")
        self.healthy = self.running and health in (None, "healthy")
        self.managed = labels.get(LABEL) == "true"
        self.generation = int(labels.get("reskate.generation") or 0)   # newer copy of the same version

    def rank(self, latest=""):
        """Newest first; a version above ReSkate's latest was pulled again and ranks below the rest."""
        pulled = bool(latest) and numbers(self.version) > numbers(latest)
        return not pulled, numbers(self.version), self.generation


class Rollover:
    def __init__(self, config):
        self.cfg = config
        self.dry = bool(config.get("dry_run", True))
        self.grace = float(config.get("drain_grace_minutes", 10)) * 60
        self.repo = config.get("image", "dudedankdave/reskate-server")
        self.protect = set(config.get("protect", []))
        self.empty_since = {}          # container id -> first time it was seen empty
        self.seen = {}                 # (server, release) -> when it was first seen as due
        self.waiting = set()           # messages already logged, so a waiting state is said once
        self.mods_checked = {}         # container id -> (time, pending mod updates)
        self.hub_seen = {}             # release -> when it was first seen without an image on Docker Hub
        self.revisions = {}            # (repo, version) -> (when looked up, newest image revision)

    def once(self, key, message):
        if key not in self.waiting:
            self.waiting.add(key)
            self.notify(message)

    def notify(self, message):
        say(message)
        hook = os.environ.get("DISCORD_WEBHOOK", "")
        if hook and not self.dry:
            try:
                req = urllib.request.Request(hook, json.dumps({"content": message[:1900], "username": "ReSkate rollover"}).encode(),
                                             {"Content-Type": "application/json", "User-Agent": "reskate-rollover"})
                urllib.request.urlopen(req, timeout=20).read()
            except Exception as e:
                say(f"discord: {e}")

    def containers(self):
        """Every container whose name starts with container_prefix, inspected."""
        prefix = "/" + self.cfg.get("container_prefix", "reskate-server-")
        return [docker("GET", f"/containers/{c['Id']}/json") for c in docker("GET", "/containers/json?all=1")
                if any(n.startswith(prefix) for n in c.get("Names") or [])]

    def tick(self):
        latest = latest_release()
        everything = self.containers()
        for number, server in self.cfg["servers"].items():
            ports = {str(s["port"]): name for name, s in server["slots"].items()}
            instances = [Instance(i, ports[env_of(i).get("PORT", "")]) for i in everything
                       if env_of(i).get("PORT", "") in ports]
            try:
                self.server(number, server, latest, instances)
                self.cleanup(number, server, instances)
            except Exception as e:
                say(f"server {number}: {e!r}")
        try:
            self.sweep_tools()
        except Exception as e:
            say(f"cleanup of old tool containers: {e!r}")

    def remove(self, info, why):
        """Delete a stopped container (never its volumes, never an image)."""
        self.notify(f"{why}: removing stopped {info['Name'].lstrip('/')}" + (" (dry run, nothing done)" if self.dry else ""))
        if not self.dry:
            docker("DELETE", f"/containers/{info['Id']}")

    def cleanup(self, number, server, instances):
        """Remove stopped containers of a server that have been stopped for `cleanup_hours` (default 72, 0 = off),
        except the newest `cleanup_keep` (default 1: the one to go back to). Only while the server runs one
        healthy container, so nothing is touched in the middle of a rollover; volumes stay."""
        hours = float(setting(self.cfg, server, "cleanup_hours", 72))
        keep = int(setting(self.cfg, server, "cleanup_keep", 1))
        live = [s for s in instances if s.running]
        if hours <= 0 or len(live) != 1 or not live[0].healthy:
            return
        old = sorted((s for s in instances if not s.running and s.name not in self.protect),
                     key=lambda s: finished_at(s.info), reverse=True)
        for s in old[keep:]:
            stopped = finished_at(s.info)
            if stopped and time.time() - stopped >= hours * 3600:
                self.remove(s.info, f"Server {number}: stopped for {(time.time() - stopped) / 3600:.0f} h")

    def sweep_tools(self):
        """The same for the tools' own leftovers (reskate-bot-prev, reskate-bridge-prev-teams, reskate-rollover-old ...)."""
        hours = float(setting(self.cfg, {}, "cleanup_hours", 72))
        if hours <= 0:
            return
        for c in docker("GET", "/containers/json?all=1"):
            if c.get("State") != "exited" or not any(LEFTOVER.match(n) for n in c.get("Names") or []):
                continue
            info = docker("GET", f"/containers/{c['Id']}/json")
            if info["Name"].lstrip("/") in self.protect:
                continue
            stopped = finished_at(info)
            if stopped and time.time() - stopped >= hours * 3600:
                self.remove(info, f"Tools: stopped for {(time.time() - stopped) / 3600:.0f} h")

    def server(self, number, server, latest, instances):
        live = [s for s in instances if s.running]
        if not live:
            return
        newest = max(live, key=lambda s: s.rank(latest))
        # drain: older instances stop once the newest is healthy and they have been empty long enough
        for s in live:
            if s is newest or s.rank(latest) >= newest.rank(latest):
                continue
            if s.name in self.protect:
                self.once(("protect", s.name), f"Server {number}: {s.name} ({s.version}) is protected, leaving it running.")
                continue
            if not newest.healthy:
                continue
            if self.empty_for(s) >= self.grace:
                self.stop(s, f"Server {number}: {s.name} ({s.version}) empty for {self.grace / 60:.0f} min, "
                             f"{newest.name} ({newest.version}) takes over")
        if numbers(newest.version) < numbers(latest):
            what = f"ReSkate {latest} is out"
            if newest.name in self.protect:
                return self.once(("protect-new", newest.name, latest),
                                 f"Server {number}: {what}, {newest.name} is protected, nothing started.")
            return self.roll(number, server, newest, live, instances, latest, what, latest)
        if numbers(newest.version) > numbers(latest):   # ReSkate pulled it again: back onto its latest
            what = f"ReSkate {newest.version} was pulled, {latest} is the latest again"
            if newest.name in self.protect:
                return self.once(("protect-back", newest.name, latest),
                                 f"Server {number}: {what}, {newest.name} is protected, nothing started.")
            if not hub_has(self.repo, latest):
                return self.once(("back-no-image", latest),
                                 f"Server {number}: {what}, but {self.repo}:{latest} is not on Docker Hub; "
                                 "going back is up to you.")
            return self.roll(number, server, newest, live, instances, latest, what, f"back:{latest}", back=True)
        if len(live) > 1 or not newest.healthy or newest.name in self.protect:
            return                                     # mid-rollover: one change at a time
        if self.lacks_mounts(server, newest):
            return self.repair(number, server, newest, instances)
        if self.settings_changed(number, server, newest):
            return
        if self.restart_requested(number, server, newest):
            return
        if self.image_update(number, server, newest, instances):
            return
        updates = self.pending_mods(number, server, newest)
        if updates:
            what = "map/mod update " + ", ".join(f"{o}-{n} {have} -> {new}" for o, n, new, have in updates)
            key = "mods:" + ",".join(f"{o}-{n}-{new}" for o, n, new, _ in updates)
            self.roll(number, server, newest, live, instances, newest.version, what, key, mods=True)

    def roll(self, number, server, newest, live, instances, version, what, key, mods=False, back=False):
        """Bring the server onto `version` (or onto new mods, or back from a pulled release onto
        `version`'s image): in place when empty, else a copy."""
        if not mods and not self.image_ready(version):
            return
        count = players(newest.info)
        due = time.time() - self.seen.setdefault((number, key), time.time())
        if mods:      # map/mod updates have their own pair, MODUPDATE_MIN_PLAYERS / MODUPDATE_WAIT
            least = int(setting(self.cfg, server, "modupdate_min_players", 1))
            wait = float(setting(self.cfg, server, "modupdate_wait", 60)) * 60
        else:
            least = int(setting(self.cfg, server, "copy_min_players", 1))
            wait = float(setting(self.cfg, server, "copy_wait_minutes", 60)) * 60
        if 0 < count < least and due < wait:           # a few players: wait for them to leave first
            return self.once(("few", number, key),
                             f"Server {number}: {what}, {count} player(s) on {newest.name} "
                             f"(fewer than {least}); updating it once empty, or starting a copy after "
                             f"{wait / 60:.0f} min.")
        if count == 0:
            if mods:                                   # mods install on start: a restart is enough
                return self.restart(newest, f"Server {number}: {what}, nobody is on {newest.name}, restarting it")
            slot = newest.slot                         # nobody on: replace it in place
        else:
            slot = next(n for n in server["slots"] if n != newest.slot)
            busy = [s for s in live if s.slot == slot]
            if busy:
                return self.once(("busy", number, key),
                                 f"Server {number}: {what}, but slot {slot.upper()} still runs "
                                 f"{busy[0].name}; waiting until it is stopped.")
        if back:      # its own name: the stopped copy of `version` from before is usually still there
            return self.start_copy(number, server, newest, slot, version, count, instances, what,
                                   suffix=time.strftime("-p%m%d%H%M", time.gmtime()), tag=version, back=True)
        self.start_copy(number, server, newest, slot, version, count, instances, what,
                        suffix=time.strftime("-m%m%d%H%M", time.gmtime()) if mods else "")

    def mods_volume(self, server):
        """Name of the shared map/mod volume to mount at /mods, "" when it is switched off."""
        volume = str(setting(self.cfg, server, "mods_volume", "reskate-mods"))
        return "" if volume.lower() in ("off", "none", "false") else volume

    def shared_volume(self, server):
        """Name of a volume to mount at /shared (leaderboard database, roles), "" when none is set."""
        volume = str(setting(self.cfg, server, "shared_volume", ""))
        return "" if volume.lower() in ("", "off", "none", "false") else volume

    def missing_mounts(self, server, inst):
        """What the config wants on this container but it lacks: the destinations /mods and /shared,
        and "a console" when it runs without stdin (the bridge sends console commands through it)."""
        if not setting(self.cfg, server, "repair_mods", True):
            return []
        have = [m.get("Destination") for m in inst.info.get("Mounts") or []]
        lacking = [dest for dest, volume in (("/mods", self.mods_volume(server)), ("/shared", self.shared_volume(server)))
                   if volume and dest not in have]
        if not (inst.info.get("Config") or {}).get("OpenStdin", True):
            lacking.append("a console")
        return lacking

    def lacks_mounts(self, server, inst):
        """True when the shared map volume (/mods) or the shared volume (/shared) should be on this
        container but isn't, or it has no console. A server with every standard mount and a console
        never needs a recreate for one."""
        return bool(self.missing_mounts(server, inst))

    def few_enough(self, server, inst):
        """True when at most restart_max_players (default 3) are on the server, a restart is fine.
        A server whose players can't be counted counts as busy."""
        try:
            count = players(inst.info)
        except Exception as e:
            say(f"{inst.name}: could not count players ({e}), treating it as busy")
            return False
        return count <= int(setting(self.cfg, server, "restart_max_players", 3))

    def repair(self, number, server, inst, instances):
        """Recreate an up-to-date server that lacks a standard mount, once at most
        restart_max_players are on it (a repair is not urgent, so it waits for that)."""
        lacking = ", ".join(self.missing_mounts(server, inst))
        if not self.few_enough(server, inst):
            return self.once(("repair-wait", inst.name), f"Server {number}: {inst.name} runs without {lacking}, "
                                                         "recreating it once few enough players are on it.")
        self.start_copy(number, server, inst, inst.slot, inst.version, players(inst.info), instances,
                        f"{lacking} is not mounted", suffix=time.strftime("-r%m%d%H%M", time.gmtime()))

    def image_ready(self, version):
        """True once <image>:<version> is on Docker Hub. A release is announced before its image
        is built, so wait up to image_wait_minutes (default 60) for it; after that go on without
        it (the current image plus the release installed into the volume)."""
        first = self.hub_seen.setdefault(version, time.time())
        wait = float(setting(self.cfg, {}, "image_wait_minutes", 60)) * 60
        try:
            if hub_has(self.repo, version):
                return True
        except Exception as e:
            say(f"Docker Hub check for {version} failed: {e}")
        if time.time() - first >= wait:
            self.once(("image-late", version), f"ReSkate {version}: no {self.repo}:{version} on Docker Hub after "
                                               f"{wait / 60:.0f} min, going on with the current image.")
            return True
        self.once(("image-wait", version), f"ReSkate {version} is out, waiting up to {wait / 60:.0f} min for "
                                           f"{self.repo}:{version} on Docker Hub before updating anything.")
        return False

    def pending_mods(self, number, server, inst):
        """Newer Thunderstore versions of the server's unpinned MODS entries, asked inside the
        container with the image's own mods.py; [] when off, not due or not answerable."""
        if not setting(self.cfg, server, "mod_updates", True) or not inst.env.get("MODS", "").strip():
            return []
        if inst.env.get("MODS_UPDATE", "true").strip().lower() in ("0", "false", "no", "off"):
            return []                                  # a restart would not install them anyway
        every = float(setting(self.cfg, server, "mod_check_minutes", 30)) * 60
        when, cached = self.mods_checked.get(inst.info["Id"], (0, []))
        if time.time() - when < every:
            return cached
        script = ("import json,os,sys; sys.path.insert(0,'/app'); import mods\n"
                  "print('PENDING ' + json.dumps(mods.pending_updates(os.environ.get('MODS',''))))")
        try:
            out = exec_in(inst.info, ["python3", "-c", script])
            line = [l for l in out.splitlines() if l.startswith("PENDING ")][-1]
            cached = [tuple(u) for u in json.loads(line[8:])]
        except Exception as e:
            self.once(("mods-check", inst.name), f"Server {number}: can't check {inst.name} for mod updates ({e}).")
            cached = []
        self.mods_checked[inst.info["Id"]] = (time.time(), cached)
        return cached

    def settings_changed(self, number, server, inst):
        """Restart a server whose settings files changed since it started, once it is empty. True
        while that is pending (one change at a time)."""
        folder = str(setting(self.cfg, server, "settings_dir", "/shared/settings"))
        if settings is None or not os.path.isdir(folder):
            return False
        # With the settings module typing changes into the console, only what it cannot do counts.
        kind = "restart-digest" if self.settings_live(number) else "digest"
        started = [m.group(2) for m in map(DIGEST.match, logs(inst.info)) if m and m.group(1) == kind]
        if not started:
            return False                           # an image from before the settings files
        values, _, _ = settings.load(inst.env, folder, report=False)
        want = settings.restart_digest(values) if kind == "restart-digest" else settings.digest(values)
        if started[-1] == want:
            return False
        if not self.few_enough(server, inst):
            self.once(("settings-wait", inst.name, want),
                      f"Server {number}: its settings changed, restarting {inst.name} once few enough players are on it.")
            return True
        self.restart(inst, f"Server {number}: its settings changed, few players on {inst.name}, restarting it")
        return True

    def restart_requested(self, number, server, inst):
        """Restart once when /shared/settings/.restart/server-<n> is newer than the server's start
        (a restart without a settings change, e.g. asked for in the dashboard). True while pending."""
        folder = str(setting(self.cfg, server, "settings_dir", "/shared/settings"))
        try:
            asked = os.stat(os.path.join(folder, ".restart", f"server-{number}")).st_mtime
        except OSError:
            return False
        if asked <= started_at(inst.info):
            return False
        if not self.few_enough(server, inst):
            self.once(("restart-wait", inst.name, asked),
                      f"Server {number}: a restart was requested, restarting {inst.name} once few enough players are on it.")
            return True
        self.restart(inst, f"Server {number}: a restart was requested, restarting {inst.name}")
        return True

    def settings_live(self, number):
        """True when /shared/modules.json hands this server's settings to the settings module."""
        try:
            import handover
            with open(handover.FILE, encoding="utf-8") as f:
                part = handover.parse(json.load(f), str(number)).get("settings")
        except (ImportError, OSError, ValueError):
            return False
        return str(part or "").strip().lower() == "external"

    def image_update(self, number, server, inst, instances):
        """Replace a server in its slot with a newer image revision of its ReSkate version, once
        nobody is on it. True while that is pending."""
        if not setting(self.cfg, server, "image_updates", False):
            return False
        if not inst.info["Config"]["Image"].split(":")[0] == self.repo or inst.info["Config"]["Image"].endswith("-slim"):
            return False                               # only our default image; slim keeps its own tags
        key = (self.repo, inst.version)
        at, newest = self.revisions.get(key, (0, 0))
        if time.time() - at > 1800:
            try:
                newest = hub_revision(self.repo, inst.version)
            except Exception as e:
                say(f"docker hub: {e}")
            self.revisions[key] = (time.time(), newest)
        have = revision_of(inst.env)
        if newest <= have:
            return False
        what = f"image {inst.version}-r{newest} is out (runs r{have or '?'})"
        if self.empty_for(inst) <= 0:
            self.once(("image-wait", inst.name, newest), f"Server {number}: {what}, updating {inst.name} once nobody is on it.")
            return True
        self.start_copy(number, server, inst, inst.slot, inst.version, 0, instances, what,
                        suffix=f"-r{newest}", tag=f"{inst.version}-r{newest}")
        return True

    def restart(self, s, why):
        self.notify(why + (" (dry run, not restarted)" if self.dry else ""))
        if not self.dry:
            docker("POST", f"/containers/{s.info['Id']}/restart?t=60", timeout=180)
            self.mods_checked.pop(s.info["Id"], None)

    def empty_for(self, s):
        try:
            count = players(s.info)
        except Exception as e:
            say(f"{s.name}: could not count players ({e}), treating it as busy")
            count = None
        if count != 0:
            self.empty_since.pop(s.info["Id"], None)
            return 0
        return time.time() - self.empty_since.setdefault(s.info["Id"], time.time())

    def stop(self, s, why):
        self.notify(why + (" (dry run, not stopped)" if self.dry else ""))
        if not self.dry:
            docker("POST", f"/containers/{s.info['Id']}/stop?t=60", timeout=120)
            self.empty_since.pop(s.info["Id"], None)

    def token(self, server_number, slot, instances):
        """Steam token of a slot: STEAM_TOKEN_<server><slot> on this container, else from any
        container (also a stopped one) that ran on that slot."""
        token = os.environ.get(f"STEAM_TOKEN_{server_number}{slot.upper()}", "")
        if not token:
            for s in sorted(instances, key=lambda s: not s.running):
                if s.slot == slot and s.env.get("STEAM_TOKEN"):
                    return s.env["STEAM_TOKEN"]
        return token

    def start_copy(self, number, server, old, slot, version, count, instances, what, suffix="", tag=None, back=False):
        s = server["slots"][slot]
        token = self.token(number, slot, instances)
        if not token:
            return self.once(("token", number, slot), f"Server {number}: no Steam token for slot {slot.upper()}, "
                                                      f"set STEAM_TOKEN_{number}{slot.upper()}.")
        name = f"{self.cfg.get('container_prefix', 'reskate-server-')}{number}-{version}{suffix}"
        if any(x.name == name for x in instances):
            return self.once(("exists", name), f"Server {number}: {name} exists but is not running; start it by hand.")
        # a mod copy keeps the old image; a release copy takes the Hub tag when there is one
        if tag is None:
            tag = None if suffix else (version if hub_has(self.repo, version) else None)
        image = f"{self.repo}:{tag}" if tag else old.info["Config"]["Image"]
        install = not tag and not suffix and numbers(old.env.get("RESKATE_IMAGE_VERSION")) < numbers(version)
        in_place = slot == old.slot
        how = (f"{count} player(s) on {old.name}, replacing it in slot {slot.upper()}" if in_place and count else
               f"nobody is on {old.name}, replacing it in slot {slot.upper()}" if in_place else
               f"{count} player(s) on {old.name} ({old.version}), starting a copy in slot {slot.upper()}")
        self.notify(f"Server {number}: {what}, {how}: {name} on ports {s['port']}/{s['query_port']}, "
                    f"image {image}" + (" + release installed into its volume" if install else "")
                    + (" (dry run, nothing done)" if self.dry else ""))
        if self.dry:
            return
        if tag:
            self.pull(self.repo, tag)
        elif install:
            self.preinstall(image, s["volume"], version)
        if in_place:
            docker("POST", f"/containers/{old.info['Id']}/stop?t=60", timeout=120)
        for x in instances:                      # stopped copies of ours on that slot are superseded
            if x.slot == slot and x.managed and not x.running and x.name not in self.protect:
                docker("DELETE", f"/containers/{x.info['Id']}")
        env = dict(old.env)
        env.update(PORT=str(s["port"]), QUERY_PORT=str(s["query_port"]), STEAM_TOKEN=token,
                   SERVER_NAME=server_name(self.cfg, number, server, version), RESKATE_SERVER=str(number))
        for k in ("AUTO_UPDATE", "UPDATE_MODE", "UPDATE_POLICY", "RESKATE_IMAGE_VERSION", "RESKATE_IMAGE_REVISION", "PATH",
                  "LD_LIBRARY_PATH", "HOME", "SteamAppId"):
            if suffix and not back and k in ("UPDATE_MODE", "UPDATE_POLICY") and k in env:
                continue                   # same release again: keep how the old one was told to update
            # (going back runs the image's own server: an auto copy would keep the pulled one in /data/server)
            env.pop(k, None)               # image defaults come back from the image
        if install:                        # run the installed release; later ones are ours to roll out
            env.update(UPDATE_MODE="auto", UPDATE_POLICY="ask")
        folder = str(setting(self.cfg, server, "settings_dir", "/shared/settings"))
        if settings is not None and os.path.isdir(folder):   # what the settings files set stays out of the env
            have = settings.load(env, folder, report=False)[0]
            keys = [k for k in have if k in env and k not in settings.IDENTITY and k != "SERVER_NAME"]
            without = settings.load({k: v for k, v in env.items() if k not in keys}, folder, report=False)[0]
            for k in keys:
                if without.get(k) == have[k]:       # not one a KEY+= builds on
                    env.pop(k)
        host = old.info["HostConfig"]
        # shared map/mod store at /mods on every container we create (a server without it can crash-loop);
        # "off" leaves it out
        mods_volume = self.mods_volume(server)
        shared_volume = self.shared_volume(server)
        body = {
            "Image": image,
            "Env": [f"{k}={v}" for k, v in env.items()],
            # always a console: the bridge sends commands through stdin, a copy must not inherit a server without one
            "Tty": True, "OpenStdin": True, "StdinOnce": False,
            "Labels": {LABEL: "true", "reskate.server": str(number), "reskate.slot": slot, "reskate.version": version,
                       "reskate.generation": str(int(time.time()))},
            "HostConfig": {
                "NetworkMode": host.get("NetworkMode", "host"),
                "RestartPolicy": host.get("RestartPolicy") or {"Name": "unless-stopped"},
                "Binds": [f"{s['volume']}:/data"] + ([f"{mods_volume}:/mods"] if mods_volume else [])
                         + ([f"{shared_volume}:/shared"] if shared_volume else []),
                "LogConfig": host.get("LogConfig") or {},
            },
        }
        made = docker("POST", f"/containers/create?name={urllib.parse.quote(name)}", body)
        docker("POST", f"/containers/{made['Id']}/start")
        self.notify(f"Server {number}: {name} started.")

    def pull(self, repo, tag):
        out = docker("POST", f"/images/create?fromImage={urllib.parse.quote(repo)}&tag={urllib.parse.quote(tag)}",
                     raw=True, timeout=900).decode("utf-8", "replace")
        for line in out.splitlines():
            if '"error"' in line:
                raise RuntimeError(f"pulling {repo}:{tag}: {line[:300]}")

    def preinstall(self, image, volume, version):
        """Install the release into the slot's /data/server with the image's own updater, in a
        throwaway container, so the copy starts on it."""
        script = ("import os,sys; sys.path.insert(0,'/app'); import updater\n"
                  "updater.prepare(os.environ.get('RESKATE_IMAGE_VERSION',''), print)\n"
                  "info = updater.latest()\n"
                  f"assert info['version'] == {version!r}, info['version']\n"
                  "if updater.numbers(updater.version_of(updater.DIR)) < updater.numbers(info['version']):\n"
                  "    updater.swap_in(updater.stage(info))\n"
                  "print('installed', updater.version_of(updater.DIR))\n")
        made = docker("POST", "/containers/create", {
            "Image": image, "Entrypoint": ["python3", "-c", script], "Cmd": [],
            "Labels": {LABEL: "install"}, "HostConfig": {"Binds": [f"{volume}:/data"], "NetworkMode": "host"},
        })
        try:
            docker("POST", f"/containers/{made['Id']}/start")
            code = docker("POST", f"/containers/{made['Id']}/wait", timeout=900)["StatusCode"]
            out = docker("GET", f"/containers/{made['Id']}/logs?stdout=1&stderr=1", raw=True).decode("utf-8", "replace")
            if code != 0:
                raise RuntimeError(f"installing {version} into {volume} failed ({code}): {out[-500:]}")
            say(f"installed ReSkate {version} into {volume}")
        finally:
            docker("DELETE", f"/containers/{made['Id']}?force=1")


def exec_in(info, cmd, timeout=120):
    """Run cmd inside a running container and return its output."""
    made = docker("POST", f"/containers/{info['Id']}/exec",
                  {"Cmd": cmd, "AttachStdout": True, "AttachStderr": True, "Tty": False})
    out = docker("POST", f"/exec/{made['Id']}/start", {"Detach": False, "Tty": False}, raw=True, timeout=timeout)
    frames, i = [], 0                                  # multiplexed: 8-byte frame headers
    while i + 8 <= len(out) and out[i] in (0, 1, 2):
        size = int.from_bytes(out[i + 4:i + 8], "big")
        frames.append(out[i + 8:i + 8 + size])
        i += 8 + size
    text = b"".join(frames).decode("utf-8", "replace")
    if docker("GET", f"/exec/{made['Id']}/json").get("ExitCode") not in (0, None):
        raise RuntimeError(text.strip()[-300:] or "exec failed")
    return text


def setting(cfg, server, key, default):
    """A server's own value for key, else the KEY env var (so the container can override the
    file), else the top-level one, else default."""
    if key in server:
        return server[key]
    return os.environ.get(key.upper()) or cfg.get(key, default)


def server_name(cfg, number, server, version):
    fmt = cfg.get("name_format", "[24-7] - [v{v}] - COCOJAMBOS YARD {n} - {role}")
    return fmt.format(v=version.replace(".", ""), n=number, role=server.get("role", ""))


def main():
    with open(CONFIG, encoding="utf-8") as f:
        config = json.load(f)
    if os.environ.get("DRY_RUN"):
        config["dry_run"] = os.environ["DRY_RUN"].strip().lower() not in ("0", "false", "no", "off")
    r = Rollover(config)
    every = float(config.get("check_minutes", 5)) * 60
    say(f"watching {len(config['servers'])} servers, every {every / 60:.0f} min" + (", DRY RUN" if r.dry else ""))
    while True:
        try:
            r.tick()
        except Exception as e:
            say(f"check failed: {e!r}")
        if "--once" in sys.argv:
            break
        time.sleep(every)


if __name__ == "__main__":
    main()
