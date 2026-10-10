"""Brings a second server up when a ReSkate release comes out while players are on the old one.

Each yard has two slots, A and B, each with its own ports, Steam token and /data volume. One of
them runs the yard. When a newer ReSkate release is out:

* nobody on the running server: it is replaced in its own slot (same token, so the same Steam ID).
* players on it: a copy on the new version starts in the other slot. The old server keeps
  running until it has been empty for DRAIN_GRACE minutes, then it is stopped (not removed,
  so it can be started again by hand).

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
from collections import Counter
from datetime import datetime, timezone

CONFIG = os.environ.get("CONFIG", "/config/yards.json")
SOCKET = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
DOCKER_API = os.environ.get("DOCKER_API", "")   # instead of the socket, e.g. Portainer's .../api/endpoints/3/docker
LAUNCHER = "https://github.com/Dingo-Shenanigans/ReSkate/releases/latest/download/launcher.json"
LABEL = "reskate.rollover"            # set on every container this tool creates
LINE = re.compile(r"^\[\d\d:\d\d:\d\d\] (.*)$")
JOINED = re.compile(r"^.+ joined \(\d+(?:, admin)?\), (\d+)/\d+ players, loaded in \d+ s$")
LEFT = re.compile(r"^.+ left \(.*\)( \[.*\])?$")
NETWORK = re.compile(r"^\[network\] (\d+) players, ")
UP = re.compile(r"^.+ is up on .+ for \d+ players\.$")


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


def logs(info):
    """The container's output since it last started, as text lines."""
    started = info["State"].get("StartedAt", "")[:19]
    since = timegm(time.strptime(started, "%Y-%m-%dT%H:%M:%S")) - 5 if started else 0
    out = docker("GET", f"/containers/{info['Id']}/logs?stdout=1&stderr=1&since={since}", raw=True, timeout=120)
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


def hub_has(repo, tag):
    try:
        fetch(f"https://hub.docker.com/v2/repositories/{repo}/tags/{urllib.parse.quote(tag)}", 20)
        return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise


# ---- yards -------------------------------------------------------------------------------
class Server:
    """One container of a yard."""

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


class Rollover:
    def __init__(self, config):
        self.cfg = config
        self.dry = bool(config.get("dry_run", True))
        self.grace = float(config.get("drain_grace_minutes", 10)) * 60
        self.repo = config.get("image", "dudedankdave/reskate-server")
        self.protect = set(config.get("protect", []))
        self.empty_since = {}          # container id -> first time it was seen empty
        self.waiting = set()           # messages already logged, so a waiting state is said once

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
        for number, yard in self.cfg["yards"].items():
            ports = {str(s["port"]): name for name, s in yard["slots"].items()}
            servers = [Server(i, ports[env_of(i).get("PORT", "")]) for i in everything
                       if env_of(i).get("PORT", "") in ports]
            try:
                self.yard(number, yard, latest, servers)
            except Exception as e:
                say(f"yard {number}: {e!r}")

    def yard(self, number, yard, latest, servers):
        live = [s for s in servers if s.running]
        if not live:
            return
        newest = max(live, key=lambda s: numbers(s.version))
        # drain: older servers stop once the newest is healthy and they have been empty long enough
        for s in live:
            if s is newest or numbers(s.version) >= numbers(newest.version):
                continue
            if s.name in self.protect:
                self.once(("protect", s.name), f"Yard {number}: {s.name} ({s.version}) is protected, leaving it running.")
                continue
            if not newest.healthy:
                continue
            if self.empty_for(s) >= self.grace:
                self.stop(s, f"Yard {number}: {s.name} ({s.version}) empty for {self.grace / 60:.0f} min, "
                             f"{newest.name} ({newest.version}) takes over")
        if numbers(newest.version) >= numbers(latest):
            return
        if newest.name in self.protect:
            return self.once(("protect-new", newest.name, latest),
                             f"Yard {number}: ReSkate {latest} is out, {newest.name} is protected, nothing started.")
        count = players(newest.info)
        if count == 0:
            slot = newest.slot                         # nobody on: replace it in place
        else:
            slot = next(n for n in yard["slots"] if n != newest.slot)
            busy = [s for s in live if s.slot == slot]
            if busy:
                return self.once(("busy", number, latest),
                                 f"Yard {number}: ReSkate {latest} is out, but slot {slot.upper()} still runs "
                                 f"{busy[0].name}; waiting until it is stopped.")
        self.start_copy(number, yard, newest, slot, latest, count, servers)

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

    def token(self, yard_number, slot, servers):
        """Steam token of a slot: STEAM_TOKEN_<yard><slot> on this container, else from any
        container (also a stopped one) that ran on that slot."""
        token = os.environ.get(f"STEAM_TOKEN_{yard_number}{slot.upper()}", "")
        if not token:
            for s in sorted(servers, key=lambda s: not s.running):
                if s.slot == slot and s.env.get("STEAM_TOKEN"):
                    return s.env["STEAM_TOKEN"]
        return token

    def start_copy(self, number, yard, old, slot, version, count, servers):
        s = yard["slots"][slot]
        token = self.token(number, slot, servers)
        if not token:
            return self.once(("token", number, slot), f"Yard {number}: no Steam token for slot {slot.upper()}, "
                                                      f"set STEAM_TOKEN_{number}{slot.upper()}.")
        name = f"{self.cfg.get('container_prefix', 'reskate-server-')}{number}-{version}"
        if any(x.name == name for x in servers):
            return self.once(("exists", name), f"Yard {number}: {name} exists but is not running; start it by hand.")
        tag = version if hub_has(self.repo, version) else None
        image = f"{self.repo}:{tag}" if tag else old.info["Config"]["Image"]
        in_place = slot == old.slot
        how = (f"nobody is on {old.name}, replacing it in slot {slot.upper()}" if in_place else
               f"{count} player(s) on {old.name} ({old.version}), starting a copy in slot {slot.upper()}")
        self.notify(f"Yard {number}: ReSkate {version} is out, {how}: {name} on ports {s['port']}/{s['query_port']}, "
                    f"image {image}" + ("" if tag else " + release installed into its volume")
                    + (" (dry run, nothing done)" if self.dry else ""))
        if self.dry:
            return
        if tag:
            self.pull(self.repo, tag)
        else:
            self.preinstall(image, s["volume"], version)
        if in_place:
            docker("POST", f"/containers/{old.info['Id']}/stop?t=60", timeout=120)
        for x in servers:                      # stopped copies of ours on that slot are superseded
            if x.slot == slot and x.managed and not x.running and x.name not in self.protect:
                docker("DELETE", f"/containers/{x.info['Id']}")
        env = dict(old.env)
        env.update(PORT=str(s["port"]), QUERY_PORT=str(s["query_port"]), STEAM_TOKEN=token,
                   SERVER_NAME=yard_name(self.cfg, number, yard, version))
        for k in ("AUTO_UPDATE", "UPDATE_MODE", "UPDATE_POLICY", "RESKATE_IMAGE_VERSION", "PATH",
                  "LD_LIBRARY_PATH", "HOME", "SteamAppId"):
            env.pop(k, None)               # image defaults come back from the image
        if not tag:                        # run the installed release; later ones are ours to roll out
            env.update(UPDATE_MODE="auto", UPDATE_POLICY="ask")
        host = old.info["HostConfig"]
        cfg = old.info["Config"]
        body = {
            "Image": image,
            "Env": [f"{k}={v}" for k, v in env.items()],
            "Tty": cfg.get("Tty", True), "OpenStdin": cfg.get("OpenStdin", True), "StdinOnce": False,
            "Labels": {LABEL: "true", "reskate.yard": str(number), "reskate.slot": slot, "reskate.version": version},
            "HostConfig": {
                "NetworkMode": host.get("NetworkMode", "host"),
                "RestartPolicy": host.get("RestartPolicy") or {"Name": "unless-stopped"},
                "Binds": [f"{s['volume']}:/data"],
                "LogConfig": host.get("LogConfig") or {},
            },
        }
        made = docker("POST", f"/containers/create?name={urllib.parse.quote(name)}", body)
        docker("POST", f"/containers/{made['Id']}/start")
        self.notify(f"Yard {number}: {name} started.")

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


def yard_name(cfg, number, yard, version):
    fmt = cfg.get("name_format", "[24-7] - [v{v}] - COCOJAMBOS YARD {n} - {role}")
    return fmt.format(v=version.replace(".", ""), n=number, role=yard.get("role", ""))


def main():
    with open(CONFIG, encoding="utf-8") as f:
        config = json.load(f)
    if os.environ.get("DRY_RUN"):
        config["dry_run"] = os.environ["DRY_RUN"].strip().lower() not in ("0", "false", "no", "off")
    r = Rollover(config)
    every = float(config.get("check_minutes", 5)) * 60
    say(f"watching {len(config['yards'])} yards, every {every / 60:.0f} min" + (", DRY RUN" if r.dry else ""))
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
