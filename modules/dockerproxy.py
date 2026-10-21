"""A filtering Docker socket for the dashboard (RESKATE_MODE=dockerproxy).

A Docker socket is root on the host, read-only mount or not: `:ro` does not stop API calls. The dashboard
gets this socket instead of the real one. It passes on only what the dashboard does:

  GET  /_ping, /version, /containers/json, /containers/<c>/json, /containers/<c>/logs (no follow)
  GET  /containers/<c>/archive          game server containers only
  PUT  /containers/<c>/archive          game servers, path /data or /data/Mods, a tar of plain files and
                                        symlinks with bare names (no folders, no "..")
  POST /containers/<c>/restart          game servers only

Everything else (create, exec, images, volumes, networks, start/stop/kill, ...) is refused with 403. A game
server is a container whose name starts with DOCKERPROXY_SERVERS (default reskate-server-). Other containers'
env values whose key looks secret (TOKEN, PASSWORD, WEBHOOK, SECRET, KEY) are replaced by "***" in
/containers/<c>/json, so the dashboard cannot read the bot's Discord token; game servers keep theirs (the
dashboard shows a server's join password on request).

Settings: DOCKERPROXY_SOCKET (default /run/dockerproxy/docker.sock), DOCKERPROXY_UPSTREAM (default
/var/run/docker.sock), DOCKERPROXY_UID / DOCKERPROXY_GID (owner of the socket, default 1000), DOCKERPROXY_SERVERS.
Runs as root (it needs the real socket); the dashboard can then run as uid 1000 without it."""
import http.client
import io
import json
import os
import re
import socket
import socketserver
import tarfile
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

SOCKET = os.environ.get("DOCKERPROXY_SOCKET", "/run/dockerproxy/docker.sock")
UPSTREAM = os.environ.get("DOCKERPROXY_UPSTREAM", "/var/run/docker.sock")
UID = int(os.environ.get("DOCKERPROXY_UID", "1000"))
GID = int(os.environ.get("DOCKERPROXY_GID", "1000"))
SERVERS = os.environ.get("DOCKERPROXY_SERVERS", "reskate-server-")
SECRET = re.compile(r"PASSWORD|TOKEN|SECRET|WEBHOOK|_KEY$|^KEY$", re.I)
VERSION = re.compile(r"^/v\d+\.\d+(?=/)")
NAME = r"[A-Za-z0-9][A-Za-z0-9_.-]*"
PUT_PATHS = {"/data", "/data/Mods"}
PUT_MAX = 8 * 1024 * 1024


def say(message):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())} dockerproxy: {message}", flush=True)


class Unix(http.client.HTTPConnection):
    def __init__(self, path, timeout=90):
        super().__init__("docker", timeout=timeout)
        self.unix = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.unix)


def upstream(method, path, body=None, headers=None):
    """(status, headers, body) of one call on the real socket."""
    conn = Unix(UPSTREAM)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        r = conn.getresponse()
        return r.status, r.getheaders(), r.read()
    finally:
        conn.close()


def container_name(ref):
    """The container's name without the slash, or None when it does not exist."""
    status, _, body = upstream("GET", f"/containers/{ref}/json")
    if status != 200:
        return None
    return json.loads(body).get("Name", "").lstrip("/")


def is_server(ref):
    name = container_name(ref)
    return bool(name) and name.startswith(SERVERS)


def plain_tar(data):
    """Why a tar for PUT archive is refused, or None: only regular files and symlinks with bare names."""
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as t:
            members = t.getmembers()
    except (tarfile.TarError, EOFError) as e:
        return f"not a tar: {e}"
    if not members:
        return "empty tar"
    for m in members:
        if not (m.isfile() or m.issym()):
            return f"{m.name}: only files and symlinks"
        if "/" in m.name or m.name in ("", ".", "..") or "\\" in m.name:
            return f"{m.name}: only bare names"
    return None


def redact(body):
    """Container json with secret-looking env values of a non-server container replaced by ***."""
    info = json.loads(body)
    if info.get("Name", "").lstrip("/").startswith(SERVERS):
        return body
    config = info.get("Config") or {}
    env = config.get("Env") or []
    info["Config"] = config
    config["Env"] = [f"{e.split('=', 1)[0]}=***" if "=" in e and SECRET.search(e.split("=", 1)[0]) else e
                             for e in env]
    return json.dumps(info).encode()


def decide(method, path, query):
    """('pass' | 'redact' | 'server' | 'put', container) when allowed, else raises PermissionError."""
    if method == "GET":
        if path in ("/_ping", "/version", "/containers/json"):
            return "pass", None
        if m := re.fullmatch(rf"/containers/({NAME})/json", path):
            return "redact", m.group(1)
        if m := re.fullmatch(rf"/containers/({NAME})/logs", path):
            if query.get("follow", ["0"])[0] not in ("0", "false", ""):
                raise PermissionError("follow is not allowed")
            return "pass", m.group(1)
        if m := re.fullmatch(rf"/containers/({NAME})/archive", path):
            return "server", m.group(1)
    if method == "PUT" and (m := re.fullmatch(rf"/containers/({NAME})/archive", path)):
        if query.get("path", [""])[0] not in PUT_PATHS:
            raise PermissionError(f"archive path must be one of {sorted(PUT_PATHS)}")
        return "put", m.group(1)
    if method == "POST" and (m := re.fullmatch(rf"/containers/({NAME})/restart", path)):
        return "server", m.group(1)
    raise PermissionError(f"{method} {path} is not allowed")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def reply(self, status, body, headers=()):
        self.send_response(status)
        sent = set()
        for k, v in headers:
            if k.lower() in ("content-length", "transfer-encoding", "connection"):
                continue
            self.send_header(k, v)
            sent.add(k.lower())
        if "content-type" not in sent:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def refuse(self, why):
        say(f"refused {self.command} {self.path[:200]}: {why}")
        self.reply(403, json.dumps({"message": f"dockerproxy: {why}"}).encode())

    def handle_any(self):
        url = urlparse(self.path)
        path, query = VERSION.sub("", url.path), parse_qs(url.query)
        length = int(self.headers.get("Content-Length") or 0)
        if length > PUT_MAX:
            return self.refuse("body too large")
        body = self.rfile.read(length) if length else None
        try:
            kind, ref = decide(self.command, path, query)
            if kind in ("server", "put") and not is_server(ref):
                raise PermissionError(f"{ref} is not a game server container")
            if kind == "put":
                why = plain_tar(body or b"")
                if why:
                    raise PermissionError(why)
        except PermissionError as e:
            return self.refuse(str(e))
        except (OSError, ValueError, http.client.HTTPException) as e:
            return self.reply(502, json.dumps({"message": f"dockerproxy: upstream: {e}"}).encode())
        headers = {k: v for k, v in self.headers.items() if k.lower() in ("content-type",)}
        try:
            status, out_headers, data = upstream(self.command, self.path, body, headers)
        except (OSError, http.client.HTTPException) as e:
            return self.reply(502, json.dumps({"message": f"dockerproxy: upstream: {e}"}).encode())
        if kind == "redact" and status == 200:
            data = redact(data)
        if self.command != "GET":
            say(f"{self.command} {path} ({ref}) -> {status}")
        self.reply(status, data, out_headers)

    do_GET = do_PUT = do_POST = do_DELETE = do_HEAD = handle_any


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def get_request(self):
        request, _ = super().get_request()
        return request, ("unix", 0)


def main():
    os.makedirs(os.path.dirname(SOCKET), exist_ok=True)
    try:
        os.unlink(SOCKET)
    except FileNotFoundError:
        pass
    server = Server(SOCKET, Handler)
    os.chown(SOCKET, UID, GID)
    os.chmod(SOCKET, 0o660)
    say(f"listening on {SOCKET}, passing allowed calls to {UPSTREAM}")
    server.serve_forever()


if __name__ == "__main__":
    main()
