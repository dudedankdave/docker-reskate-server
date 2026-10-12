"""The bridge (RESKATE_MODE=bridge): one small HTTP API between the game servers and everything that
wants to talk to them (the Discord bot, scripts), so no server needs to know about the bot.

* Server -> outside: it follows the log of every running server container and keeps the last lines
  as events (id, server, time, scopes, text). GET /v1/events?after=<id>&wait=25 waits for new ones.
* Outside -> server: POST /v1/servers/<n>/do {"action": "announce", "args": {"text": "..."}, "by": "name"}
  types the matching line into the console of every running container of that server (or only of
  slot "A"/"B" when "slot" is given) (the Docker
  attach API on the host's socket, like bot/console.py). Only the actions below exist; `quit`, `stop`,
  `restart`, `update` and `reload` are never sent, restarts are the rollover's job.
* GET /v1/actions lists the actions and their inputs (so a dialog can be built from it),
  GET /v1/servers the servers with players and map, GET /v1/health is for checks.

Every request needs `Authorization: Bearer $BRIDGE_TOKEN`. Settings: BRIDGE_TOKEN (required),
BRIDGE_BIND (default 127.0.0.1), BRIDGE_PORT (default 8780), BRIDGE_BUFFER (events kept, default 5000).
Reads servers.json like the rollover; standard library only."""
import calendar
import json
import os
import re
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import rollover  # noqa: E402
import status  # noqa: E402
from rollover import docker, logs, say  # noqa: E402

TOKEN = os.environ.get("BRIDGE_TOKEN", "").strip()
BIND = os.environ.get("BRIDGE_BIND", "127.0.0.1")
PORT = int(os.environ.get("BRIDGE_PORT", "8780"))
BUFFER = max(100, int(os.environ.get("BRIDGE_BUFFER", "5000")))
POLL = 3     # seconds between log reads

BLOCKED = {"quit", "exit", "stop", "restart", "shutdown", "update", "reload"}

# name -> label, console line template ({arg} placeholders), inputs. `admin` ones are only for
# callers that vouch for an admin (the bridge cannot check who is behind `by`).
ACTIONS = {
    "say": {"label": "Say in chat", "line": "say {text}", "args": [("text", "Text", 200)]},
    "announce": {"label": "Announcement to everyone", "line": "announce {text}", "args": [("text", "Text", 200)]},
    "msg": {"label": "Private message to a player", "line": "msg {player} {text}",
            "args": [("player", "Player", 64), ("text", "Text", 200)]},
    "kick": {"label": "Kick a player", "line": "kick {player}", "args": [("player", "Player", 64)]},
    "poll": {"label": "Start a poll", "line": "poll {question} | {answers}",
             "args": [("question", "Question", 120), ("answers", "Answers, separated by |", 280)]},
    "poll_end": {"label": "End the poll", "line": "poll end", "args": []},
    "console": {"label": "Any console command", "line": "{command}", "args": [("command", "Command", 300)]},
    "players": {"label": "Players now", "info": True, "args": []},
    "log": {"label": "Last log lines", "info": True, "args": [("count", "How many lines (default 15)", 3)]},
}

events = []              # {"id", "server", "time", "scopes", "text"}
next_id = 1
cond = threading.Condition()
board = None
seen = {}                # container id -> last time stamp read

CHAT = re.compile(r"^\[chat\] ")
JOIN = re.compile(r"^(.+) joined \(\d+(?:, admin)?\), \d+/\d+ players")
LEAVE = re.compile(r"^(.+?) left \(.*\)(?: \[.*\])?$")
THROW = re.compile(r"^\[throwdown\] .+ has finished: ")
ADMIN = re.compile(r"^\[(?:kick|ban|anticheat|command|words|spam)\]|\b(?:kicked|banned|warning|error)\b", re.I)


def scopes_of(text):
    """Which feeds a console line belongs to (same split as the bot's feeds)."""
    m = rollover.LINE.match(text)
    body = m.group(1) if m else text
    if CHAT.match(body):
        return ["chat"]
    out = ["log"]
    if JOIN.match(body) or LEAVE.match(body) or THROW.match(body) or status.MAP.match(body):
        out.append("public")
    if ADMIN.search(body) or JOIN.match(body) or LEAVE.match(body):
        out.append("admin")
    return out


def push(server, stamp, text, scopes=None):
    global next_id
    with cond:
        events.append({"id": next_id, "server": server, "time": stamp, "scopes": scopes or scopes_of(text), "text": text})
        next_id += 1
        del events[:-BUFFER]
        cond.notify_all()


def slot_of(number, info):
    """Slot letter (A/B ...) of a container: the slot whose port is the container's PORT, else None."""
    port = rollover.env_of(info).get("PORT", "")
    for letter, slot in board.cfg["servers"][number]["slots"].items():
        if str(slot["port"]) == port:
            return letter
    return None


def running(number, slot=None):
    """The running containers of a server, only those in `slot` (A/B) when given."""
    return [i for i in board.gather().get(number, []) if i["State"].get("Running")
            and (not slot or slot_of(number, i) == slot)]


def follow():
    """Read what every running server container logged since the last look."""
    while True:
        try:
            for number, infos in board.gather().items():
                for info in infos:
                    if not info["State"].get("Running"):
                        continue
                    first = info["Id"] not in seen
                    last = seen.get(info["Id"], "")
                    since = int(time.time()) - POLL * 4 if first else max(
                        calendar.timegm(time.strptime(last[:19], "%Y-%m-%dT%H:%M:%S")) - 1, 0)
                    # The first read only marks the position: nothing from before the bridge started is replayed.
                    for raw in logs(info, since=since, timestamps=True):
                        m = status.STAMP.match(raw)
                        if not m:
                            continue
                        stamp = m.group(1) + "." + (m.group(2) or "0").ljust(9, "0")
                        if stamp <= last:
                            continue
                        last = stamp
                        if not first:
                            push(number, m.group(1) + "Z", m.group(3).rstrip())
                    seen[info["Id"]] = last or time.strftime("%Y-%m-%dT%H:%M:%S.000000000", time.gmtime())
        except Exception as e:
            say(f"bridge: could not read the logs: {e!r}")
        time.sleep(POLL)


def type_into(info, line, timeout=10):
    """Type `line` into the container's console (Docker attach, stdin only)."""
    if rollover.DOCKER_API:
        raise RuntimeError("console input needs the Docker socket, not DOCKER_API")
    if not info["Config"].get("Tty") or not info["Config"].get("OpenStdin"):
        raise RuntimeError("the container has no TTY/stdin open, so there is no console to type into")
    path = f"/v1.41/containers/{info['Id']}/attach?stream=1&stdin=1&stdout=0&stderr=0"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(rollover.SOCKET)
        sock.sendall((f"POST {path} HTTP/1.1\r\nHost: docker\r\nConnection: Upgrade\r\nUpgrade: tcp\r\n"
                      "Content-Length: 0\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(4096)
            if not chunk:
                break
            head += chunk
        first = head.split(b"\r\n", 1)[0].decode("latin-1")
        if " 101 " not in first and " 200 " not in first:
            raise RuntimeError(f"docker attach: {first or 'no answer'}")
        sock.sendall(line.encode("utf-8") + b"\n")
    finally:
        sock.close()


def clean(value, limit):
    value = str(value or "").strip()
    if not value or "\n" in value or "\r" in value:
        raise ValueError("every input is one non-empty line")
    return value[:limit]


def do(number, action, args, by, slot=None):
    """Run one action on a server; returns the text to show. Raises ValueError for bad requests."""
    spec = ACTIONS.get(action)
    if not spec:
        raise ValueError(f"unknown action {action!r}")
    if number not in board.cfg["servers"]:
        raise ValueError("unknown server")
    args = args or {}
    slot = str(slot).upper() if slot else None
    if slot and slot not in board.cfg["servers"][number]["slots"]:
        raise ValueError(f"unknown slot {slot!r}")
    infos = running(number, slot)
    if not infos:
        raise ValueError(f"that server is not running in slot {slot}" if slot else "that server is not running")
    if spec.get("info"):
        if action == "players":
            s = board.follow(infos[0])
            return f"{s.players}/{s.max or '?'} players on {s.map or '?'}"
        count = max(1, min(int(args.get("count") or 15), 50))
        lines = [m.group(3) for raw in logs(infos[0], timestamps=True) if (m := status.STAMP.match(raw))]
        return "\n".join(lines[-count:]) or "(no log lines)"
    values = {}
    for key, _label, limit in spec["args"]:
        values[key] = clean(args.get(key), limit)
    line = spec["line"].format(**values)
    word = line.split(None, 1)[0].lower().lstrip("/")
    if word in BLOCKED:
        raise ValueError(f"`{word}` stops or restarts the server; restarts are only done on an empty server, by the rollover")
    sent = 0
    for info in infos:
        try:
            type_into(info, line)
            sent += 1
        except (OSError, RuntimeError) as e:
            say(f"bridge: could not type into {info['Name']}: {e}")
    if not sent:
        raise RuntimeError("could not type into the console")
    note = f"{by or 'someone'} used the bridge{' (slot ' + slot + ')' if slot else ''}: {line[:300]}"
    say(f"bridge: server {number}: {note}")
    push(number, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), note, ["admin", "audit"])
    where = f" slot {slot}" if slot else ""
    return f"Sent to server {number}{where}: {line[:300]}" + (f" ({sent} containers)" if sent > 1 else "")


def server_list():
    out = []
    for number, infos in board.gather().items():
        cfg = board.cfg["servers"][number]
        up = [i for i in infos if i["State"].get("Running")]
        row = {"server": number, "role": cfg.get("role", ""), "running": bool(up), "players": None, "max": None,
               "map": None, "slots": {}}
        for letter in cfg["slots"]:
            row["slots"][letter] = {"running": False, "players": None, "max": None, "map": None, "version": None}
        for info in up:
            s = board.follow(info)
            slot = slot_of(number, info)
            if slot:
                row["slots"][slot].update(running=True, players=s.players, max=s.max, map=s.map,
                                          version=status.version_of(info))
        if up:
            s = board.follow(up[0])
            row.update(players=s.players, max=s.max, map=s.map)
        out.append(row)
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, code, body):
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def authorized(self):
        if self.headers.get("Authorization", "") == f"Bearer {TOKEN}":
            return True
        self.reply(401, {"error": "bad or missing token"})
        return False

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == "/v1/health":
            return self.reply(200, {"ok": True, "events": next_id - 1})
        if not self.authorized():
            return
        try:
            if url.path == "/v1/actions":
                return self.reply(200, {k: {"label": v["label"], "info": bool(v.get("info")),
                                            "args": [{"name": a, "label": lab, "max": m} for a, lab, m in v["args"]]}
                                        for k, v in ACTIONS.items()})
            if url.path == "/v1/servers":
                return self.reply(200, server_list())
            if url.path == "/v1/events":
                after, wait = int(q.get("after", 0)), min(int(q.get("wait", 0)), 60)
                end = time.time() + wait
                with cond:
                    while True:
                        found = [e for e in events if e["id"] > after
                                 and (not q.get("server") or e["server"] == q["server"])
                                 and (not q.get("scope") or q["scope"] in e["scopes"])]
                        if found or time.time() >= end:
                            break
                        cond.wait(end - time.time())
                return self.reply(200, {"events": found[:500], "last": next_id - 1})
        except (ValueError, RuntimeError) as e:
            return self.reply(400, {"error": str(e)})
        self.reply(404, {"error": "not found"})

    def do_POST(self):
        if not self.authorized():
            return
        m = re.fullmatch(r"/v1/servers/([^/]+)/do", urlparse(self.path).path)
        if not m:
            return self.reply(404, {"error": "not found"})
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 16384)) or b"{}")
            text = do(m.group(1), body.get("action"), body.get("args"), str(body.get("by") or "")[:64],
                      body.get("slot"))
        except (ValueError, TypeError) as e:
            return self.reply(400, {"ok": False, "error": str(e)})
        except RuntimeError as e:
            return self.reply(502, {"ok": False, "error": str(e)})
        self.reply(200, {"ok": True, "text": text})


def main():
    global board
    if not TOKEN:
        sys.exit("bridge: BRIDGE_TOKEN is not set; refusing to start without a token")
    with open(rollover.CONFIG, encoding="utf-8") as f:
        board = status.Board(json.load(f))
    threading.Thread(target=follow, daemon=True).start()
    say(f"bridge: listening on {BIND}:{PORT}")
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
