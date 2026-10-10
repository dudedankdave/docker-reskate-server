"""Ranked play and the leaderboard (LEADERBOARD=true). Runs inside notifier.py's console loop,
built only on what the vanilla server logs:

* Ranked: a player whose game reports no mods that change scoring or physics, and who has not
  been caught with a sped-up game this session. Each player is told by DM shortly after joining
  (the mod report arrives after the join line), and again when that changes.
* Points: only from finished throwdowns with at least two players who did not quit (the
  server's "[throwdown] ... has finished: ..." line, so activity_log must be on). Jam and Spot
  Battle by place (LEADERBOARD_POINTS, default 10,6,4,2: the last value for every later place),
  S.K.A.T.E. (no winner in the log) the last value for each player who finished. Unranked
  players keep their place but get nothing.
* Every LEADERBOARD_INTERVAL minutes (default 60) the top LEADERBOARD_SIZE (default 5) go to
  chat while players are on, and the top 10 to Discord (LEADERBOARD_WEBHOOK, else the public
  feed) when the board changed since the last post.

LEADERBOARD_SCOPE picks whose points are counted: "shared" (default) keeps one board for every
server that mounts the same folder at /shared (/shared/leaderboard.json); without that mount it
falls back to "server", this server's own board in /data/leaderboard.json. The shared file is
locked while it is changed, and one server posts it to Discord per interval. LEADERBOARD_FILE
overrides the file in either scope.
"""
import json
import os
import re
import threading
import time

try:
    import fcntl
except ImportError:  # no locking: fine for a file only one server uses
    fcntl = None

LINE = re.compile(r"^\[(\d\d:\d\d:\d\d)\] (.*)$")
JOINED = re.compile(r"^(.+) joined \((\d+)(?:, admin)?\), \d+/\d+ players")
LEFT = re.compile(r"^(.+) left \(.*\)$")
UP = re.compile(r"^.+ is up on .+ for \d+ players\.$")
MODDED = re.compile(r"^\[anticheat\] (.+)'s mods change scoring or physics: (.*) \(scoring [^)]*\)\.$")
CLEARED = re.compile(r"^\[anticheat\] (.+) may take part in throwdowns again ")
SPEEDING = re.compile(r"^\[anticheat\] (.+)'s game is running at [\d.]+x speed")
FINISHED = re.compile(r"^\[throwdown\] .+'s (Jam|Spot Battle|S\.K\.A\.T\.E\.|throwdown) has finished: (.+)$")
PLACED = re.compile(r"(\d+)\. (.+?) (-?\d{1,3}(?:,\d{3})*)( \(quit\))?(?=, \d+\. |$)")
TRIED = re.compile(r"(?:^|, )(.+?) \d+ landed / \d+ missed( \(quit\))?(?=, |$)")

CHECK_AFTER = 15      # seconds after joining before the mod report is taken as complete
CHAT_MAX = 200        # bytes in one chat line (multiplayer_chat_max_bytes)

RANKED = "You are RANKED: your throwdown results count for the leaderboard."
UNRANKED_MODS = ("You are NOT ranked: your mods change scoring or physics ({mods}). "
                 "Restart Skate without them to earn leaderboard points.")
UNRANKED_SPEED = "You are NOT ranked until you rejoin: your game ran faster than normal."


def env(name, default=""):
    return (os.environ.get(name) or "").strip() or default


def truthy(value):
    return value.strip().lower() in ("1", "true", "yes", "on")


def fit(text, limit=CHAT_MAX):
    data = text.encode("utf-8")[:limit]
    return data.decode("utf-8", "ignore")


class Board:
    """The points file, read and written under an exclusive lock."""

    def __init__(self, path):
        self.path = path

    def change(self, fn):
        """fn(data) -> result, with data saved afterwards. None if the file can't be used."""
        try:
            with open(self.path + ".lock", "a") as lock:
                if fcntl:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    with open(self.path, encoding="utf-8") as f:
                        data = json.load(f)
                except (OSError, ValueError):
                    data = {}
                data.setdefault("players", {})
                result = fn(data)
                with open(self.path + ".tmp", "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=1)
                os.replace(self.path + ".tmp", self.path)
                return result
        except OSError as e:
            print(f"[leaderboard] cannot use {self.path}: {e}", flush=True)
            return None

    def read(self):
        try:
            with open(self.path + ".lock", "a") as lock:
                if fcntl:
                    fcntl.flock(lock, fcntl.LOCK_SH)
                with open(self.path, encoding="utf-8") as f:
                    return json.load(f)
        except (OSError, ValueError):
            return {"players": {}}


def top(data, n):
    rows = [p for p in data.get("players", {}).values() if p.get("points", 0) > 0]
    rows.sort(key=lambda p: (-p["points"], -p.get("wins", 0), p.get("name", "").lower()))
    return rows[:n]


class Leaderboard:
    def __init__(self, send, post):
        """send(console line) types into the server; post(text) goes to Discord (or None)."""
        self.send, self.post = send, post
        self.pool = env("LEADERBOARD_SCOPE", "shared").lower() != "server"
        path = env("LEADERBOARD_FILE", "/shared/leaderboard.json" if self.pool else "/data/leaderboard.json")
        if self.pool and not os.path.isdir(os.path.dirname(path) or "."):
            print(f"[leaderboard] {os.path.dirname(path)} is not mounted, so the leaderboard is this server's own "
                  "(LEADERBOARD_SCOPE=server); mount one folder there on every server to share it", flush=True)
            self.pool, path = False, "/data/leaderboard.json"
        self.board = Board(path)
        self.label = "all servers" if self.pool else "this server"
        self.interval = 60 * max(1, int(env("LEADERBOARD_INTERVAL", "60")))
        self.size = max(1, min(10, int(env("LEADERBOARD_SIZE", "5"))))
        self.points = [int(x) for x in env("LEADERBOARD_POINTS", "10,6,4,2").split(",") if x.strip()]
        self.ranked_text = env("RANKED_MESSAGE", RANKED)
        self.unranked_text = env("UNRANKED_MESSAGE", UNRANKED_MODS)
        self.lock = threading.Lock()
        self.online = {}       # name -> SteamID64
        self.waiting = {}      # name -> time the ranked DM is due
        self.modded = {}       # name -> the mods the server named
        self.speeding = set()  # caught this session: unranked until they rejoin
        self.told = {}         # name -> what they were told last (True = ranked)
        self.chat_at = self.discord_at = time.time()

    # ---- log lines ------------------------------------------------------------------
    def feed(self, line):
        m = LINE.match(line)
        text = m.group(2) if m else line
        with self.lock:
            if UP.match(text):
                self.online.clear(), self.waiting.clear(), self.modded.clear()
                self.speeding.clear(), self.told.clear()
            elif j := JOINED.match(text):
                name = j.group(1)
                self.online[name] = j.group(2)
                self.waiting[name] = time.time() + CHECK_AFTER
                self.modded.pop(name, None), self.speeding.discard(name), self.told.pop(name, None)
            elif left := LEFT.match(text):
                name = left.group(1)
                self.online.pop(name, None), self.waiting.pop(name, None)
                self.modded.pop(name, None), self.speeding.discard(name), self.told.pop(name, None)
            elif mod := MODDED.match(text):
                self.modded[mod.group(1)] = mod.group(2)
                self.tell(mod.group(1))
            elif cleared := CLEARED.match(text):
                self.modded.pop(cleared.group(1), None)
                self.tell(cleared.group(1))
            elif fast := SPEEDING.match(text):
                self.speeding.add(fast.group(1))
                self.tell(fast.group(1))
            elif done := FINISHED.match(text):
                self.score(done.group(1), done.group(2))

    def ranked(self, name):
        return name not in self.modded and name not in self.speeding

    def tell(self, name):
        """DM the player whether they are ranked, unless the join check is still pending or
        nothing changed since the last DM."""
        if name not in self.online or name in self.waiting:
            return
        ranked = self.ranked(name)
        if self.told.get(name) == ranked:
            return
        self.told[name] = ranked
        if ranked:
            text = self.ranked_text
        elif name in self.modded:
            text = self.unranked_text.replace("{mods}", self.modded[name] or "your mods")
        else:
            text = UNRANKED_SPEED
        self.send(fit(f"msg {self.online[name]} {text}", CHAT_MAX + 30))

    # ---- points ---------------------------------------------------------------------
    def results(self, mode, results):
        """[(name, place or None, quit)] from a finished line."""
        if mode == "S.K.A.T.E.":
            return [(m.group(1), None, bool(m.group(2))) for m in TRIED.finditer(results)]
        return [(m.group(2), int(m.group(1)), bool(m.group(4))) for m in PLACED.finditer(results)]

    def score(self, mode, results):
        rows = self.results(mode, results)
        finished = [r for r in rows if not r[2]]
        if len(finished) < 2 or not self.points:
            return
        awards = []
        for name, place, _quit in finished:
            if not self.ranked(name):
                continue
            index = len(self.points) - 1 if place is None else min(place, len(self.points)) - 1
            awards.append((name, self.points[index], place == 1))

        def apply(data):
            players = data["players"]
            for name, points, won in awards:
                key = self.online.get(name) or f"name:{name}"
                p = players.setdefault(key, {"points": 0, "wins": 0, "played": 0})
                p["name"] = name
                p["points"] += points
                p["wins"] += int(won)
                p["played"] += 1
            data["version"] = data.get("version", 0) + 1
        if awards:
            self.board.change(apply)
            print("[leaderboard] " + ", ".join(f"{n} +{p}" for n, p, _ in awards), flush=True)

    # ---- timers ---------------------------------------------------------------------
    def tick(self):
        now = time.time()
        with self.lock:
            for name, due in list(self.waiting.items()):
                if now >= due:
                    del self.waiting[name]
                    self.tell(name)
            players_on = bool(self.online)
        if now - self.chat_at >= self.interval:
            self.chat_at = now
            if players_on:
                self.post_chat()
        if self.post and now - self.discord_at >= 60:
            self.discord_at = now
            self.post_discord(now)

    def post_chat(self):
        rows = top(self.board.read(), self.size)
        if not rows:
            return
        entries = [f"{i}. {p['name']} {p['points']}" for i, p in enumerate(rows, 1)]
        self.send("announce " + fit("Leaderboard: " + " | ".join(entries[:3])))
        line = f"Leaderboard ({self.label}, ranked throwdowns):"
        for entry in entries:
            if len((line + "  " + entry).encode("utf-8")) > CHAT_MAX:
                self.send("say " + line)
                line = entry
            else:
                line += "  " + entry
        self.send("say " + line)

    def post_discord(self, now):
        data = self.board.read()
        if now - data.get("posted_at", 0) < self.interval or data.get("version", 0) == data.get("posted_version"):
            return

        def due(data):
            if now - data.get("posted_at", 0) < self.interval:
                return None
            data["posted_at"] = now
            if data.get("version", 0) == data.get("posted_version"):
                return None                       # nothing new since the last post
            data["posted_version"] = data.get("version", 0)
            return top(data, 10)
        rows = self.board.change(due)
        if not rows:
            return
        width = max(len(p["name"]) for p in rows)
        lines = [f"{i:>2}. {p['name']:<{width}}  {p['points']:>5} pts  "
                 f"{p.get('wins', 0)} won / {p.get('played', 0)} played" for i, p in enumerate(rows, 1)]
        self.post(f"**Leaderboard** ({self.label}, ranked throwdowns)\n```\n" + "\n".join(lines).replace("```", "'''") + "\n```")

    def loop(self):
        while True:
            try:
                self.tick()
            except Exception as e:  # never let the sidecar die
                print(f"[leaderboard] {e!r}", flush=True)
            time.sleep(1)


def create(send, post):
    """The Leaderboard when LEADERBOARD is on, else None."""
    if not truthy(os.environ.get("LEADERBOARD") or ""):
        return None
    board = Leaderboard(send, post)
    threading.Thread(target=board.loop, daemon=True).start()
    return board
