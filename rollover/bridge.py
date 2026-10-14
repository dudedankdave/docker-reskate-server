"""The bridge (RESKATE_MODE=bridge): one small HTTP API between the game servers and everything that
wants to talk to them (the Discord bot, scripts), so no server needs to know about the bot.

* Server -> outside: it follows the log of every running server container and keeps the last lines
  as events (id, server, time, scopes, text). GET /v1/events?after=<id>&wait=25 waits for new ones.
  The image's own messages (server up, join code, updates, mods: what its webhooks post) come as
  "[event] <scope> {json}" lines; they become events of that scope with "kind": "message" and
  "ping" (console lines have "kind": "console").
* Outside -> server: POST /v1/servers/<n>/do {"action": "announce", "args": {"text": "..."}, "by": "name"}
  types the matching line into the console of every running container of that server (or only of
  slot "A"/"B" when "slot" is given) (the Docker
  attach API on the host's socket, like bot/console.py). Only the actions below exist; `quit`, `stop`,
  `restart`, `update` and `reload` are never sent, restarts are the rollover's job.
* GET /v1/actions lists the actions and their inputs (so a dialog can be built from it),
  GET /v1/servers the servers with players and map, GET /v1/status the status cards (status.py's message,
  for the bot), GET /v1/health is for checks.
* Teams (app/teams.py, /shared/teams.db, the bridge is its only writer; /shared must be writable for it):
  GET /v1/servers/<n>/players (who is on, with their team tag), GET /v1/teams/tags, GET /v1/teams/applications,
  POST /v1/teams/act {"who": {"steam_id"|"discord", "name"}, "verb", "args"}, POST /v1/teams/decide
  {"id", "accept", "by"}. Messages for players go to them in game (msg), else back to the caller.
* GET /v1/servers/<n>/settings: per running slot, the settings the server started with (its env plus the
  settings files, see app/settings.py), without secrets: what the modules outside need (leaderboard ...).
* A caller whose "by" starts with "module:" (the leaderboard, the announcer ...) acts as often as it needs:
  its actions are audit events only, not posts to the admin scope.

Every request needs `Authorization: Bearer $BRIDGE_TOKEN`. Settings: BRIDGE_TOKEN (required),
BRIDGE_BIND (default 127.0.0.1), BRIDGE_PORT (default 8780), BRIDGE_BUFFER (events kept, default 5000).
Reads servers.json like the rollover; standard library only."""
import calendar
import json
import os
import re
import socket
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import rollover  # noqa: E402
import status  # noqa: E402

try:
    import teams  # noqa: E402  (app/teams.py: teams, applications, Discord links; /shared/teams.db)
except ImportError:
    teams = None
try:
    import settings  # noqa: E402  (app/settings.py: the settings files, with /shared mounted)
except ImportError:
    settings = None
from rollover import logs, say  # noqa: E402

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

online = {}              # (server, slot) -> {steam_id: name}: who is on, from the join and leave lines
teams_db = None          # teams.Teams when the bridge keeps teams (the file's folder is writable)
events = []              # {"id", "server", "slot", "time", "scopes", "text", "kind", "ping"}
next_id = 1
cond = threading.Condition()
board = None
seen = {}                # container id -> last time stamp read

JOINED = re.compile(r"^(.+) joined \((\d+)(?:, admin)?\), \d+/\d+ players")
CHAT = re.compile(r"^\[chat\] ")
EVENT = re.compile(r"^\[event\] ([a-z-]+) (\{.*\})$")
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


def message(text):
    """(scope, text, ping) of an [event] line from the image, or None for a console line."""
    m = EVENT.match(text)
    if not m:
        return None
    try:
        data = json.loads(m.group(2))
    except ValueError:
        return None
    return m.group(1), str(data.get("text") or ""), bool(data.get("ping"))


def track(server, slot, text):
    """Keep who is on each server (and tell the teams about every name seen)."""
    m = rollover.LINE.match(text)
    body = m.group(1) if m else text
    key = (server, slot)
    if j := JOINED.match(body):
        online.setdefault(key, {})[j.group(2)] = j.group(1)
        if teams_db:
            try:
                teams_db.seen(j.group(2), j.group(1))
            except Exception as e:
                say(f"bridge: teams: {e!r}")
    elif status.UP.match(body):
        online.pop(key, None)
    elif left := LEAVE.match(body):
        people = online.get(key, {})
        for steam_id in [i for i, n in people.items() if n == left.group(1)][:1]:
            del people[steam_id]


def players_of(number):
    """[{name, steam_id, slot, team}] on a server now (only its running slots)."""
    tags = teams_db.tags() if teams_db else {}
    out = []
    for info in running(number):
        slot = slot_of(number, info)
        for steam_id, name in sorted(online.get((number, slot), {}).items(), key=lambda x: x[1].lower()):
            out.append({"name": name, "steam_id": steam_id, "slot": slot, "team": tags.get(steam_id, "")})
    return out


def team_act(who, verb, args):
    """Run a team command; its notifications go to the player in game (else back to the caller for Discord)."""
    if not teams_db:
        raise RuntimeError("teams are off: /shared is not writable here")
    return deliver(teams_db.act(who, verb, args))


def deliver(result):
    for note in result.get("notify") or []:
        note["delivered"] = send_to_player(note["steam_id"], note["text"])
        note["discord"] = teams_db.discord_of(note["steam_id"])
    return result


def send_to_player(steam_id, text):
    """Private message to a player who is on a server; False when they are not."""
    sent = False
    for (number, slot), people in list(online.items()):
        if steam_id not in people:
            continue
        for info in running(number, slot):
            try:
                type_into(info, f"msg {steam_id} {text[:190]}")
                sent = True
            except (OSError, RuntimeError, KeyError):
                pass
    return sent


def push(server, stamp, text, scopes=None, kind="console", ping=False, slot=None):
    global next_id
    if kind == "console":
        track(server, slot, text)
    with cond:
        events.append({"id": next_id, "server": server, "slot": slot, "time": stamp, "scopes": scopes or scopes_of(text),
                       "text": text, "kind": kind, "ping": ping})
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
                    slot = slot_of(number, info)
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
                        if first:
                            continue
                        text = m.group(3).rstrip()
                        if msg := message(text):
                            push(number, m.group(1) + "Z", msg[1], [msg[0]], "message", msg[2], slot)
                        else:
                            push(number, m.group(1) + "Z", text, slot=slot)
                    seen[info["Id"]] = last or time.strftime("%Y-%m-%dT%H:%M:%S.000000000", time.gmtime())
        except Exception as e:
            say(f"bridge: could not read the logs: {e!r}")
        time.sleep(POLL)


def type_into(info, line, timeout=10):
    """Type `line` into the container's console (Docker attach, stdin only)."""
    if rollover.DOCKER_API:
        raise RuntimeError("console input needs the Docker socket, not DOCKER_API")
    if not info["Config"].get("OpenStdin"):    # with or without a TTY: attach writes to the server's stdin either way
        raise RuntimeError("the container keeps no stdin open, so there is no console to type into")
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
    if slot:                                     # "A" or "a": as servers.json spells it
        given, slot = slot, next((k for k in board.cfg["servers"][number]["slots"] if k.lower() == str(slot).lower()), None)
        if slot is None:
            raise ValueError(f"unknown slot {given!r}")
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
    module = by.startswith("module:")
    if not module:
        say(f"bridge: server {number}: {note}")
    push(number, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), note, ["audit"] if module else ["admin", "audit"],
         kind="audit", slot=slot)
    where = f" slot {slot}" if slot else ""
    return f"Sent to server {number}{where}: {line[:300]}" + (f" ({sent} containers)" if sent > 1 else "")


SECRET = re.compile(r"PASSWORD|TOKEN|SECRET|WEBHOOK|_KEY$|^KEY$", re.I)
IMAGE_KEYS = {"PATH", "LD_LIBRARY_PATH", "HOME", "SteamAppId", "HOSTNAME", "TERM"}


def server_settings(number):
    """{slot: {key: value}} of the running containers of a server: env + settings files, no secrets."""
    if number not in board.cfg["servers"]:
        raise ValueError("unknown server")
    out = {}
    for info in running(number):
        env = rollover.env_of(info)
        if settings is not None and os.path.isdir(settings.DIR):
            env.update(settings.load(env, report=False)[0])
        out[slot_of(number, info) or info["Name"].lstrip("/")] = {
            k: v for k, v in sorted(env.items()) if not SECRET.search(k) and k not in IMAGE_KEYS}
    return out


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
            if url.path == "/v1/status":
                return self.reply(200, board.message())
            if m := re.fullmatch(r"/v1/servers/([^/]+)/players", url.path):
                if m.group(1) not in board.cfg["servers"]:
                    raise ValueError("unknown server")
                return self.reply(200, players_of(m.group(1)))
            if url.path == "/v1/teams/tags":
                return self.reply(200, teams_db.tags() if teams_db else {})
            if url.path == "/v1/teams/applications":
                if not teams_db:
                    raise RuntimeError("teams are off")
                return self.reply(200, teams_db.applications(q.get("status", "pending")))
            if m := re.fullmatch(r"/v1/servers/([^/]+)/settings", url.path):
                return self.reply(200, server_settings(m.group(1)))
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
        path = urlparse(self.path).path
        m = re.fullmatch(r"/v1/servers/([^/]+)/do", path)
        if not m and not path.startswith("/v1/teams/"):
            return self.reply(404, {"error": "not found"})
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 16384)) or b"{}")
            if path.startswith("/v1/teams/"):
                return self.reply(200, team_post(path, body))
            text = do(m.group(1), body.get("action"), body.get("args"), str(body.get("by") or "")[:64],
                      body.get("slot"))
        except (ValueError, TypeError) as e:
            return self.reply(400, {"ok": False, "error": str(e)})
        except RuntimeError as e:
            return self.reply(502, {"ok": False, "error": str(e)})
        self.reply(200, {"ok": True, "text": text})


def team_post(path, body):
    """POST /v1/teams/act, /v1/teams/decide, /v1/teams/applications/<id>/posted."""
    if not teams_db:
        raise RuntimeError("teams are off: /shared is not writable here")
    try:
        return team_call(path, body)
    except sqlite3.Error as e:
        raise RuntimeError(f"teams.db: {e}") from None


def team_call(path, body):
    if path == "/v1/teams/act":
        who = {k: str(v)[:64] for k, v in (body.get("who") or {}).items()}
        return team_act(who, str(body.get("verb") or ""), body.get("args") or {})
    if path == "/v1/teams/decide":
        result = teams_db.decide(int(body.get("id") or 0), bool(body.get("accept")), str(body.get("by") or "admin"))
        return deliver(result)
    if m := re.fullmatch(r"/v1/teams/applications/(\d+)/posted", path):
        teams_db.posted(int(m.group(1)), str(body.get("message") or ""))
        return {"ok": True}
    raise ValueError("not found")


def main():
    global board, teams_db
    if not TOKEN:
        sys.exit("bridge: BRIDGE_TOKEN is not set; refusing to start without a token")
    with open(rollover.CONFIG, encoding="utf-8") as f:
        board = status.Board(json.load(f))
    if teams is not None:
        try:
            teams_db = teams.Teams().setup()
            say(f"bridge: teams in {teams_db.path}")
        except Exception as e:
            say(f"bridge: no teams ({e!r}); /shared must be writable for the bridge (teams.db)")
    threading.Thread(target=follow, daemon=True).start()
    say(f"bridge: listening on {BIND}:{PORT}")
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
