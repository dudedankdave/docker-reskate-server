"""Live status cards in Discord: one message with a card per server (online, players, map, join
code), edited in place every minute instead of posting new messages.

It posts the message once through a webhook (STATUS_WEBHOOK) and remembers its id in
STATUS_STATE; after that it only edits it. If the message is deleted, it posts a new one.
Without STATUS_WEBHOOK it prints the message it would post (a dry run).

Which container is which server comes from servers.json, like rollover.py: a container belongs to
server N when it has the label reskate.server=N, its name is <container_prefix>N or
<container_prefix>N-..., or its PORT is one of server N's slot ports. Players, map and join code
are read from the server log (the server's A2S query reports 0 players): the log is read once
since the container started, then only the new lines each minute.

Talks to Docker like rollover.py (socket or DOCKER_API); Python standard library only.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rollover import CONFIG, LINE, docker, env_of, logs, numbers, say, started_at  # noqa: E402

WEBHOOK = os.environ.get("STATUS_WEBHOOK", "").strip()
STATE = os.environ.get("STATUS_STATE", "/state/status.json")
INTERVAL = max(15, int(os.environ.get("STATUS_INTERVAL", "60")))         # seconds between checks
REFRESH = max(1, int(os.environ.get("STATUS_REFRESH_MINUTES", "5"))) * 60  # edit at least this often
JOIN_CODE = os.environ.get("STATUS_JOIN_CODE", "true").strip().lower() not in ("0", "false", "no", "off")

UP = re.compile(r"^.+ is up on (.+) for (\d+) players\.$")
JOINED = re.compile(r"^.+ joined \(\d+(?:, admin)?\), (\d+)/(\d+) players, loaded in \d+ s$")
LEFT = re.compile(r"^.+ left \(.*\)( \[.*\])?$")
NETWORK = re.compile(r"^\[network\] (\d+) players, ")
MAP = re.compile(r"^(?:Everyone has loaded (.+)|\[rotation\] Changing the map to (.+)|Changing map to (.+?))\.?$")
CODE = re.compile(r"^Join code: (\S+)$")
STAMP = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?Z (.*)$")

GREEN, YELLOW, RED = 0x2ECC71, 0xF1C40F, 0xE74C3C


class Server:
    """What the log of one container says, read a bit more each check."""

    def __init__(self, info):
        self.id, self.started = info["Id"], info["State"].get("StartedAt", "")
        self.last = (0, 0)                     # (seconds, nanoseconds) of the last line read
        self.players, self.max, self.map, self.code = 0, None, None, None
        self.ok = False                        # the log was read at least once

    def read(self, info):
        since = f"{self.last[0]}.{self.last[1]:09d}" if self.ok else None
        for line in logs(info, since=since, timestamps=True):
            m = STAMP.match(line)
            if not m:
                continue
            at = (int(datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()),
                  int((m.group(2) or "0").ljust(9, "0")[:9]))
            if self.ok and at <= self.last:
                continue                       # `since` includes the line it starts at
            self.last = max(self.last, at)
            self.line(m.group(3))
        self.ok = True

    def line(self, line):
        m = LINE.match(line)
        text = m.group(1) if m else line
        if up := UP.match(text):
            self.players, self.map, self.max = 0, up.group(1), int(up.group(2))
        elif j := JOINED.match(text):
            self.players, self.max = int(j.group(1)), int(j.group(2))
        elif n := NETWORK.match(text):
            self.players = int(n.group(1))
        elif LEFT.match(text):
            self.players = max(self.players - 1, 0)
        elif mp := MAP.match(text):
            self.map = next(g for g in mp.groups() if g)
        elif c := CODE.match(text):
            self.code = c.group(1)


def version_of(info):
    labels = info["Config"].get("Labels") or {}
    env = env_of(info)
    m = re.search(r"-(\d+(?:\.\d+)+)$", info["Name"])
    return labels.get("reskate.version") or env.get("RESKATE_IMAGE_VERSION") or (m.group(1) if m else "")


class Board:
    def __init__(self, config):
        self.cfg = config
        self.prefix = config.get("container_prefix", "reskate-server-")
        self.seen = {}                         # container id -> Server
        self.state = self.load()

    # ---- which container is which server ---------------------------------------------------
    def number_of(self, info):
        labels = info["Config"].get("Labels") or {}
        if labels.get("reskate.server") in self.cfg["servers"]:
            return labels["reskate.server"]
        name = info["Name"].lstrip("/")
        if name.startswith(self.prefix):
            n = re.split(r"-", name[len(self.prefix):], maxsplit=1)[0]
            if n in self.cfg["servers"]:
                return n
        port = env_of(info).get("PORT", "")
        for n, server in self.cfg["servers"].items():
            if any(str(s["port"]) == port for s in server["slots"].values()):
                return n
        return None

    def gather(self):
        """server number -> its containers (inspected), running ones first, newest version first."""
        found = {n: [] for n in self.cfg["servers"]}
        for c in docker("GET", "/containers/json?all=1"):
            if not any(name.startswith("/" + self.prefix) for name in c.get("Names") or []):
                continue
            info = docker("GET", f"/containers/{c['Id']}/json")
            n = self.number_of(info)
            if n:
                found[n].append(info)
        for infos in found.values():
            infos.sort(key=lambda i: (not i["State"].get("Running"), not numbers(version_of(i)),
                                      tuple(-x for x in numbers(version_of(i))),
                                      -started_at(i)))
        return found

    def follow(self, info):
        s = self.seen.get(info["Id"])
        if s is None or s.started != info["State"].get("StartedAt", ""):
            s = self.seen[info["Id"]] = Server(info)
        try:
            s.read(info)
        except Exception as e:
            say(f"{info['Name'].lstrip('/')}: could not read the log ({e})")
        return s

    # ---- the cards ---------------------------------------------------------------------------
    def card(self, number, server, infos):
        role = server.get("role") or f"Server {number}"
        running = [i for i in infos if i["State"].get("Running")]
        if not running:
            last = max((i["State"].get("FinishedAt", "")[:19] for i in infos), default="")
            since = ""
            if last and not last.startswith("0001"):
                t = int(datetime.strptime(last, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp())
                since = f" since <t:{t}:R>"
            return {"title": f"🔴 {role}", "color": RED, "description": f"**Offline**{since}",
                    "footer": {"text": f"Server {number}"}}, 0, False
        info = running[0]
        s = self.follow(info)
        health = (info["State"].get("Health") or {}).get("Status")
        up = health in (None, "healthy") and s.map is not None
        env = env_of(info)
        name = env.get("SERVER_NAME", "")
        players = s.players if s.ok else None
        most = s.max or env.get("MAX_PLAYERS") or "?"
        fields = [
            {"name": "Players", "value": f"**{players if players is not None else '?'}** / {most}", "inline": True},
            {"name": "Map", "value": s.map or env.get("MAP") or "?", "inline": True},
            {"name": "Version", "value": version_of(info) or "?", "inline": True},
        ]
        if JOIN_CODE and s.code and up:
            fields.append({"name": "Join code", "value": f"`{s.code}`", "inline": False})
        for other in running[1:]:              # an older version still draining its players
            o = self.follow(other)
            fields.append({"name": f"Still on {version_of(other) or 'the old version'}",
                           "value": f"{o.players if o.ok else '?'} player(s) until they switch", "inline": False})
        started = started_at(info)
        footer = f"Server {number}" + (" · up since" if started else "")
        card = {"title": f"{'🟢' if up else '🟡'} {role}", "color": GREEN if up else YELLOW,
                "description": (f"`{name}`" if name else "") + ("" if up else "\n**Starting…**"),
                "fields": fields, "footer": {"text": footer}}
        if started:
            card["timestamp"] = datetime.fromtimestamp(started, timezone.utc).isoformat()
        total = (players or 0) + sum(self.seen[o["Id"]].players for o in running[1:] if o["Id"] in self.seen)
        return card, total, up

    def message(self):
        cards, total, online = [], 0, 0
        found = self.gather()
        for number, server in self.cfg["servers"].items():
            card, players, up = self.card(number, server, found[number])
            cards.append(card)
            total += players
            online += up
        for gone in set(self.seen) - {i["Id"] for infos in found.values() for i in infos}:
            del self.seen[gone]
        head = (f"**{total} player{'s' if total != 1 else ''} online** · {online}/{len(cards)} servers up")
        return {"content": head, "embeds": cards[:10], "allowed_mentions": {"parse": []}}

    # ---- Discord ---------------------------------------------------------------------------
    def load(self):
        try:
            with open(STATE, encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, ValueError):
            state = {}
        if os.environ.get("STATUS_MESSAGE_ID"):
            state["message"] = os.environ["STATUS_MESSAGE_ID"]
        return state

    def save(self):
        try:
            os.makedirs(os.path.dirname(STATE) or ".", exist_ok=True)
            with open(STATE + ".tmp", "w", encoding="utf-8") as f:
                json.dump(self.state, f)
            os.replace(STATE + ".tmp", STATE)
        except OSError as e:
            say(f"could not save {STATE} ({e}); a restart would post a new message")

    def publish(self, payload):
        stamp = f"\nUpdated <t:{int(time.time())}:R>"
        body = dict(payload, content=payload["content"] + stamp)
        msg = self.state.get("message")
        if msg:
            code, _ = discord("PATCH", f"{WEBHOOK}/messages/{msg}", body)
            if code != 404:
                return 200 <= code < 300
            say(f"status message {msg} is gone, posting a new one")
        code, reply = discord("POST", WEBHOOK + ("&" if "?" in WEBHOOK else "?") + "wait=true", body)
        if 200 <= code < 300 and reply.get("id"):
            self.state["message"] = reply["id"]
            self.save()
            say(f"posted status message {reply['id']}")
            return True
        return False

    def run(self, once=False):
        say(f"status cards for {len(self.cfg['servers'])} servers, every {INTERVAL} s"
            + ("" if WEBHOOK else ", DRY RUN (no STATUS_WEBHOOK)"))
        last, last_at = None, 0
        while True:
            try:
                payload = self.message()
                if not WEBHOOK:
                    print(json.dumps(payload, indent=2, ensure_ascii=False), flush=True)
                elif payload != last or time.time() - last_at >= REFRESH:
                    if self.publish(payload):
                        last, last_at = payload, time.time()
            except Exception as e:
                say(f"status check failed: {e!r}")
            if once:
                break
            time.sleep(INTERVAL)


def discord(method, url, payload):
    """(HTTP status, parsed JSON reply), honouring Discord's rate limits."""
    data = json.dumps(payload).encode("utf-8")
    for _ in range(4):
        req = urllib.request.Request(url, data, method=method,
                                     headers={"Content-Type": "application/json", "User-Agent": "reskate-status"})
        try:
            with urllib.request.urlopen(req, timeout=20) as res:
                body = res.read()
                return res.status, json.loads(body) if body.strip() else {}
        except urllib.error.HTTPError as e:
            body = e.read()
            if e.code == 429:
                try:
                    wait = float(json.loads(body).get("retry_after", 2))
                except (ValueError, AttributeError):
                    wait = 2
                time.sleep(min(max(wait, 1), 30))
                continue
            say(f"discord {method}: HTTP {e.code} {body[:200].decode('utf-8', 'replace')}")
            return e.code, {}
        except OSError as e:
            say(f"discord {method}: {e}")
            return 0, {}
    say("discord: still rate limited after retries")
    return 429, {}


def main():
    with open(CONFIG, encoding="utf-8") as f:
        config = json.load(f)
    Board(config).run(once="--once" in sys.argv)


if __name__ == "__main__":
    main()
