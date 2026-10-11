"""Ranked play and the leaderboards (LEADERBOARD=true). Runs inside notifier.py's console loop,
built only on what the vanilla server logs:

* Ranked: a player whose game reports no mods that change scoring or physics, and who has not
  been caught with a sped-up game this session. Each player is told by DM shortly after joining
  (the mod report arrives after the join line), and again when that changes. While the server
  lets players skate with their own physics tuning (enforce_tuning off, or `tuning-enforce off`
  in the console), nobody is ranked: the server cannot tell who edited theirs.
* Points: only from finished throwdowns with at least two players who did not quit (the
  server's "[throwdown] ... has finished: ..." line, so activity_log must be on). Jam and Spot
  Battle by place (LEADERBOARD_POINTS, default 10,6,4,2: the last value for every later place),
  S.K.A.T.E. (no winner in the log) the last value for each player who finished. Unranked
  players keep their place but get nothing.
* Boards (LEADERBOARD_BOARDS, default general,week,event): every award is counted on General (all
  time), on the current Week (ISO week, UTC; "Week 41") and on every Event that is open. Events
  are started and ended by admins in the game (/lb event start <name>, /lb event end).
* Every board also has one per game mode (Jam, Spot Battle, S.K.A.T.E.): /top skate, /top week jam.
* The points live in a SQLite database (leaderboard.db), with every award kept in a ledger.
  A leaderboard.json from the earlier versions is imported into General once.
* Chat commands (LEADERBOARD_COMMANDS, default on; added to the server's "commands"): /top [board],
  /rank [board], /boards, and /lb for admins (the server itself refuses it for everyone else).
  The server logs a "[command] Name /top week" line for each allowed command; this module answers
  it with a DM, so the reply is only seen by the sender.
* Every LEADERBOARD_INTERVAL minutes (default 60) the top LEADERBOARD_SIZE (default 5) go to
  chat while players are on, and the top 10 to Discord (LEADERBOARD_WEBHOOK, else the public
  feed) when a board changed since the last post.

LEADERBOARD_SCOPE picks whose points are counted: "shared" (default) keeps one database for every
server that mounts the same folder at /shared (/shared/leaderboard.db); without that mount it
falls back to "server", this server's own database in /data/leaderboard.db. LEADERBOARD_FILE
overrides the file in either scope.
"""
import json
import os
import re
import threading
import time
from contextlib import closing
from datetime import datetime, timezone

try:
    import sqlite3
except ImportError:  # python3-minimal without libpython3-stdlib
    sqlite3 = None

LINE = re.compile(r"^\[(\d\d:\d\d:\d\d)\] (.*)$")
JOINED = re.compile(r"^(.+) joined \((\d+)(, admin)?\), \d+/\d+ players")
# "Name left (reason) [connection details]": the server appends the details in brackets.
LEFT = re.compile(r"^(.+?) left \(.*\)(?: \[.*\])?$")
UP = re.compile(r"^.+ is up on .+ for \d+ players\.$")
MODDED = re.compile(r"^\[anticheat\] (.+)'s mods change scoring or physics: (.*) \(scoring [^)]*\)\.$")
CLEARED = re.compile(r"^\[anticheat\] (.+) may take part in throwdowns again ")
SPEEDING = re.compile(r"^\[anticheat\] (.+)'s game is running at [\d.]+x speed")
FINISHED = re.compile(r"^\[throwdown\] .+'s (Jam|Spot Battle|S\.K\.A\.T\.E\.|throwdown) has finished: (.+)$")
PLACED = re.compile(r"(\d+)\. (.+?) (-?\d{1,3}(?:,\d{3})*)( \(quit\))?(?=, \d+\. |$)")
TRIED = re.compile(r"(?:^|, )(.+?) \d+ landed / \d+ missed( \(quit\))?(?=, |$)")
TUNING = re.compile(r"^Players skate with (the game's|their) own physics tuning\.$")
# "[command] Name /top week": logged by the server for an allowed custom command (the line for any
# typed slash command is "[command] Name: /text" and is ignored: the server may have refused it).
COMMAND = re.compile(r"^\[command\] (.+)$")

CHECK_AFTER = 15      # seconds after joining before the mod report is taken as complete
CHAT_MAX = 200        # bytes in one chat line (multiplayer_chat_max_bytes)
ASK_EVERY = 3         # seconds between two answers to the same player
KINDS = ("general", "week", "event")
VERBS = ("top", "rank", "boards", "lb")
# The game modes with a board of their own next to every board: "General:jam", "Week 41:skate".
MODES = {"jam": "Jam", "spot": "Spot Battle", "skate": "S.K.A.T.E."}
MODE_OF = {"Jam": "jam", "throwdown": "jam", "Spot Battle": "spot", "S.K.A.T.E.": "skate"}
MODE_WORDS = {"jam": "jam", "jams": "jam", "skate": "skate", "s.k.a.t.e": "skate", "s.k.a.t.e.": "skate", "game of skate": "skate",
              "spot": "spot", "spot battle": "spot", "spotbattle": "spot", "battle": "spot", "spot battles": "spot"}

RANKED = "You are RANKED: your throwdown results count for the leaderboard."
UNRANKED_MODS = ("You are NOT ranked: your mods change scoring or physics ({mods}). "
                 "Restart Skate without them to earn leaderboard points.")
UNRANKED_SPEED = "You are NOT ranked until you rejoin: your game ran faster than normal."
UNRANKED_TUNING = ("Nobody is ranked right now: this server lets players skate with their own physics tuning. "
                   "Points count again once it is back on the game's own.")
CONFIG = "/data/ReSkateServer.json"

# The chat commands the image adds to the server's "commands" (entrypoint.py): the reply is the
# header, the rows follow as a DM from this module.
COMMANDS = [
    {"name": "top", "reply": "Leaderboard (/top week, /top skate, /top week jam, /boards):"},
    {"name": "rank", "reply": "Your rank (/rank week, /rank event):"},
    {"name": "boards", "reply": "Leaderboards:"},
    {"name": "lb", "admin": True, "reply": "Leaderboard admin (/lb help):"},
]


def env(name, default=""):
    return (os.environ.get(name) or "").strip() or default


def truthy(value):
    return value.strip().lower() in ("1", "true", "yes", "on")


def free_tuning():
    """True when the config lets players skate with their own physics tuning (enforce_tuning off)."""
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return (json.load(f).get("anti_cheat") or {}).get("enforce_tuning", True) is False
    except (OSError, ValueError, AttributeError):
        return False


def fit(text, limit=CHAT_MAX):
    data = re.sub(r"[\x00-\x1f\x7f]+", " ", text).encode("utf-8")[:limit]
    return data.decode("utf-8", "ignore")


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:32]


def week_of(now):
    year, week, _ = datetime.fromtimestamp(now, timezone.utc).isocalendar()
    return f"week-{year}-{week:02d}", f"Week {week}", week


SCHEMA = """
CREATE TABLE IF NOT EXISTS boards (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
  created_at REAL NOT NULL, closed_at REAL,
  version INTEGER NOT NULL DEFAULT 0, posted_version INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS players (steam_id TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS scores (
  board TEXT NOT NULL REFERENCES boards(id), steam_id TEXT NOT NULL,
  points INTEGER NOT NULL DEFAULT 0, wins INTEGER NOT NULL DEFAULT 0, played INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (board, steam_id));
CREATE INDEX IF NOT EXISTS scores_by_points ON scores (board, points DESC);
CREATE TABLE IF NOT EXISTS ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, board TEXT NOT NULL, steam_id TEXT NOT NULL,
  points INTEGER NOT NULL, reason TEXT NOT NULL, server TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class Store:
    """The SQLite file: players, boards, scores and the ledger of every award. Each call opens its
    own short connection (several servers may share the file) and runs in one transaction."""

    def __init__(self, path, server):
        self.path, self.server = path, server

    def run(self, fn, write=True):
        """fn(cursor) -> result, committed afterwards. None (and a log line) if the file can't be used."""
        try:
            with closing(sqlite3.connect(self.path, timeout=15, isolation_level=None)) as db:
                db.row_factory = sqlite3.Row
                cur = db.cursor()
                cur.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                try:
                    result = fn(cur)
                except BaseException:
                    cur.execute("ROLLBACK")
                    raise
                cur.execute("COMMIT")
                return result
        except (sqlite3.Error, OSError) as e:
            print(f"[leaderboard] cannot use {self.path}: {e}", flush=True)
            return None

    def setup(self, legacy):
        try:
            with closing(sqlite3.connect(self.path, timeout=15)) as db:
                db.execute("PRAGMA journal_mode=WAL")   # readers never block the writer
        except (sqlite3.Error, OSError) as e:
            print(f"[leaderboard] cannot use {self.path}: {e}", flush=True)
            return False

        def init(cur):
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    cur.execute(statement)
            self.board(cur, "general", "General", "general")
            if cur.execute("SELECT 1 FROM meta WHERE key='legacy_imported'").fetchone():
                return False
            cur.execute("INSERT INTO meta VALUES ('legacy_imported', ?)", (str(time.time()),))
            return bool(legacy) and self.import_legacy(cur, legacy)
        imported = self.run(init)
        if imported is None:
            return False
        if imported:
            try:
                os.replace(legacy, legacy + ".migrated")
            except OSError:
                pass
        return True

    def import_legacy(self, cur, legacy):
        """leaderboard.json of the versions before the database: its players become General."""
        try:
            with open(legacy, encoding="utf-8") as f:
                players = json.load(f).get("players") or {}
        except (OSError, ValueError, AttributeError, TypeError):
            return False
        for key, p in players.items():
            if not isinstance(p, dict) or not isinstance(p.get("points"), int):
                continue
            cur.execute("INSERT OR REPLACE INTO players VALUES (?, ?)", (str(key), str(p.get("name") or key)))
            cur.execute("INSERT OR IGNORE INTO scores VALUES ('general', ?, ?, ?, ?)",
                        (str(key), p["points"], int(p.get("wins") or 0), int(p.get("played") or 0)))
            cur.execute("INSERT INTO ledger (at, board, steam_id, points, reason, server) VALUES (?, 'general', ?, ?, 'imported', ?)",
                        (time.time(), str(key), p["points"], self.server))
        cur.execute("UPDATE boards SET version = version + 1 WHERE id = 'general'")
        print(f"[leaderboard] imported {len(players)} players from {legacy} into General", flush=True)
        return True

    @staticmethod
    def board(cur, board_id, name, kind):
        cur.execute("INSERT OR IGNORE INTO boards (id, name, kind, created_at) VALUES (?, ?, ?, ?)",
                    (board_id, name, kind, time.time()))

    def active(self, cur, kinds, now):
        """The boards that count new points: General, this week, the open events."""
        ids = []
        if "general" in kinds:
            ids.append("general")
        if "week" in kinds:
            week_id, week_name, _ = week_of(now)
            self.board(cur, week_id, week_name, "week")
            ids.append(week_id)
        if "event" in kinds:
            ids += [r["id"] for r in cur.execute(
                "SELECT id FROM boards WHERE kind='event' AND closed_at IS NULL ORDER BY created_at")]
        return ids

    @staticmethod
    def add(cur, boards, awards, reason, now, server):
        """awards: [(steam_id, name, points, won, played)] on every board in boards."""
        for steam_id, name, points, won, played in awards:
            cur.execute("INSERT INTO players VALUES (?, ?) ON CONFLICT(steam_id) DO UPDATE SET name=excluded.name",
                        (steam_id, name))
            for board in boards:
                cur.execute("INSERT INTO scores VALUES (?, ?, ?, ?, ?) ON CONFLICT(board, steam_id) DO UPDATE SET "
                            "points = points + excluded.points, wins = wins + excluded.wins, played = played + excluded.played",
                            (board, steam_id, points, won, played))
                cur.execute("INSERT INTO ledger (at, board, steam_id, points, reason, server) VALUES (?, ?, ?, ?, ?, ?)",
                            (now, board, steam_id, points, reason, server))
        for board in boards:
            cur.execute("UPDATE boards SET version = version + 1 WHERE id = ?", (board,))

    @staticmethod
    def top(cur, board, n):
        return cur.execute(
            "SELECT p.name, s.steam_id, s.points, s.wins, s.played FROM scores s JOIN players p USING (steam_id) "
            "WHERE s.board = ? AND s.points > 0 ORDER BY s.points DESC, s.wins DESC, p.name COLLATE NOCASE LIMIT ?",
            (board, n)).fetchall()

    @staticmethod
    def rank(cur, board, steam_id):
        row = cur.execute("SELECT points, wins, played FROM scores WHERE board=? AND steam_id=?", (board, steam_id)).fetchone()
        if not row or row["points"] <= 0:
            return None
        ahead = cur.execute("SELECT COUNT(*) FROM scores WHERE board=? AND points > ?", (board, row["points"])).fetchone()[0]
        total = cur.execute("SELECT COUNT(*) FROM scores WHERE board=? AND points > 0", (board,)).fetchone()[0]
        return ahead + 1, total, row

    @staticmethod
    def find(cur, spec, now):
        """A board by what a player types: general, week, event, a week number, an id or (the start of) a name,
        optionally followed by a game mode (jam, skate, spot): "week skate" is that mode's board of the week.
        A mode board nobody scored on yet comes back as a plain dict."""
        spec = spec.strip().lower()
        for word in sorted(MODE_WORDS, key=len, reverse=True):
            if spec == word or spec.endswith(" " + word):
                base = Store.find_base(cur, spec[:-len(word)], now)
                if not base:
                    return None
                mode = MODE_WORDS[word]
                row = cur.execute("SELECT * FROM boards WHERE id=?", (f"{base['id']}:{mode}",)).fetchone()
                return row or {"id": f"{base['id']}:{mode}", "name": f"{base['name']} - {MODES[mode]}", "kind": "mode",
                               "closed_at": base["closed_at"], "version": 0, "posted_version": 0}
        return Store.find_base(cur, spec, now)

    @staticmethod
    def find_base(cur, spec, now):
        spec = spec.strip().lower()
        if spec in ("", "general", "all", "alltime", "all-time", "g"):
            return cur.execute("SELECT * FROM boards WHERE id='general'").fetchone()
        if spec in ("week", "w", "weekly"):
            return cur.execute("SELECT * FROM boards WHERE id=?", (week_of(now)[0],)).fetchone()
        if spec in ("event", "e"):
            return cur.execute("SELECT * FROM boards WHERE kind='event' ORDER BY closed_at IS NOT NULL, created_at DESC").fetchone()
        number = re.fullmatch(r"(?:week[ -]?|w)?(\d{1,2})", spec)
        if number:
            year = datetime.fromtimestamp(now, timezone.utc).isocalendar()[0]
            row = cur.execute("SELECT * FROM boards WHERE id=?", (f"week-{year}-{int(number.group(1)):02d}",)).fetchone()
            if row:
                return row
            row = cur.execute("SELECT * FROM boards WHERE kind='week' AND name=? ORDER BY created_at DESC",
                              (f"Week {int(number.group(1))}",)).fetchone()
            if row:
                return row
        for sql in ("lower(id) = ? OR lower(name) = ?", "lower(id) LIKE ? || '%' OR lower(name) LIKE ? || '%'"):
            row = cur.execute(f"SELECT * FROM boards WHERE kind != 'mode' AND ({sql}) ORDER BY closed_at IS NOT NULL, created_at DESC",
                              (spec, spec)).fetchone()
            if row:
                return row
        return None


class Leaderboard:
    def __init__(self, send, post):
        """send(console line) types into the server; post(text) goes to Discord (or None)."""
        self.send, self.post = send, post
        self.pool = env("LEADERBOARD_SCOPE", "shared").lower() != "server"
        path = env("LEADERBOARD_FILE", "/shared/leaderboard.db" if self.pool else "/data/leaderboard.db")
        if self.pool and not os.path.isdir(os.path.dirname(path) or "."):
            print(f"[leaderboard] {os.path.dirname(path)} is not mounted, so the leaderboard is this server's own "
                  "(LEADERBOARD_SCOPE=server); mount one folder there on every server to share it", flush=True)
            self.pool, path = False, "/data/leaderboard.db"
        legacy = os.path.join(os.path.dirname(path) or ".", "leaderboard.json")
        if path.endswith(".json"):                  # an old LEADERBOARD_FILE: the points move to a .db beside it
            legacy, path = path, path[:-5] + ".db"
        self.store = Store(path, env("SERVER_NAME", "server"))
        self.ok = self.store.setup(legacy if os.path.exists(legacy) else None)
        self.kinds = {k for k in (x.strip().lower() for x in env("LEADERBOARD_BOARDS", ",".join(KINDS)).split(",")) if k in KINDS} or {"general"}
        self.label = "all servers" if self.pool else "this server"
        self.interval = 60 * max(1, int(env("LEADERBOARD_INTERVAL", "60")))
        self.size = max(1, min(10, int(env("LEADERBOARD_SIZE", "5"))))
        self.points = [int(x) for x in env("LEADERBOARD_POINTS", "10,6,4,2").split(",") if x.strip()]
        self.ranked_text = env("RANKED_MESSAGE", RANKED)
        self.unranked_text = env("UNRANKED_MESSAGE", UNRANKED_MODS)
        self.unranked_tuning_text = env("UNRANKED_TUNING_MESSAGE", UNRANKED_TUNING)
        self.unranked_speed_text = env("UNRANKED_SPEED_MESSAGE", UNRANKED_SPEED)
        self.lock = threading.Lock()
        self.online = {}       # name -> SteamID64
        self.admins = set()    # SteamID64s that joined as admin
        self.waiting = {}      # name -> time the ranked DM is due
        self.modded = {}       # name -> the mods the server named
        self.speeding = set()  # caught this session: unranked until they rejoin
        self.told = {}         # name -> what they were told last (True = ranked)
        self.asked = {}        # SteamID64 -> time of their last command answer
        self.free_tuning = free_tuning()  # own physics tuning allowed: nobody is ranked
        self.chat_at = self.discord_at = time.time()

    # ---- log lines ------------------------------------------------------------------
    def feed(self, line):
        m = LINE.match(line)
        text = m.group(2) if m else line
        with self.lock:
            if UP.match(text):
                self.online.clear(), self.waiting.clear(), self.modded.clear()
                self.speeding.clear(), self.told.clear(), self.admins.clear()
                self.free_tuning = free_tuning()
            elif j := JOINED.match(text):
                name = j.group(1)
                self.online[name] = j.group(2)
                (self.admins.add if j.group(3) else self.admins.discard)(j.group(2))
                self.waiting[name] = time.time() + CHECK_AFTER
                self.modded.pop(name, None), self.speeding.discard(name), self.told.pop(name, None)
            elif left := LEFT.match(text):
                name = left.group(1)
                self.admins.discard(self.online.pop(name, ""))
                self.waiting.pop(name, None)
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
            elif tuning := TUNING.match(text):
                self.free_tuning = tuning.group(1) == "their"
                for name in list(self.online):
                    self.tell(name)
            elif command := COMMAND.match(text):
                self.command(command.group(1))

    def ranked(self, name):
        return name not in self.modded and name not in self.speeding and not self.free_tuning

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
        elif self.free_tuning:
            text = self.unranked_tuning_text
        else:
            text = self.unranked_speed_text
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
            awards.append((self.online.get(name) or f"name:{name}", name, self.points[index], int(place == 1), 1))
        if not awards:
            return
        now = time.time()

        def apply(cur):
            boards = self.store.active(cur, self.kinds, now)
            game = MODE_OF.get(mode)
            if game:                                     # each board also has one per game mode
                for board in list(boards):
                    row = cur.execute("SELECT name FROM boards WHERE id=?", (board,)).fetchone()
                    self.store.board(cur, f"{board}:{game}", f"{row['name']} - {MODES[game]}", "mode")
                    boards.append(f"{board}:{game}")
            self.store.add(cur, boards, awards, mode, now, self.store.server)
            return boards
        boards = self.store.run(apply)
        if boards is not None:
            print("[leaderboard] " + ", ".join(f"{a[1]} +{a[2]}" for a in awards) + f" ({', '.join(boards)})", flush=True)

    # ---- chat commands --------------------------------------------------------------
    def command(self, rest):
        """rest: "Name /top week" after "[command] ". The name is whoever is online and fits. If the
        line could also be another player's typed text in the colon form ("Other: /lb ..."), someone
        named to look like it must not get a say, so the line is dropped."""
        if any(rest.startswith(other + ": /") for other in self.online):
            return
        names = [n for n in self.online if rest.startswith(n + " /")]
        if not names:
            return
        name = max(names, key=len)
        verb, _, arg = rest[len(name) + 2:].partition(" ")
        verb, arg = verb.lower(), arg.strip()
        if verb not in VERBS:
            return
        steam_id = self.online[name]
        now = time.time()
        if now - self.asked.get(steam_id, 0) < ASK_EVERY:
            return
        self.asked[steam_id] = now
        try:
            if verb == "lb":
                if steam_id not in self.admins:
                    return                              # the server logs only allowed commands; belt and braces
                lines = self.admin(name, arg, now)
            else:
                lines = getattr(self, "cmd_" + verb)(steam_id, arg, now)
        except Exception as e:                          # a bad command must not stop the log reader
            print(f"[leaderboard] /{verb} {arg!r}: {e!r}", flush=True)
            lines = ["Something went wrong with that command."]
        for text in lines:
            self.send(fit(f"msg {steam_id} {text}", CHAT_MAX + 30))

    @staticmethod
    def pack(head, entries):
        """Chat lines of at most CHAT_MAX bytes: head, then the entries side by side."""
        lines, line = [], head
        for entry in entries:
            if len((line + "  " + entry).encode("utf-8")) > CHAT_MAX:
                lines.append(line)
                line = entry
            else:
                line += "  " + entry
        return lines + [line]

    def cmd_top(self, steam_id, arg, now):
        def get(cur):
            board = Store.find(cur, arg, now)
            return board, board and Store.top(cur, board["id"], self.size)
        res = self.store.run(get, write=False)
        if res is None:
            return ["The leaderboard is not available right now."]
        board, rows = res
        if not board:
            return [f"No board called '{arg}'. Try /boards."]
        if not rows:
            return [f"{board['name']}: nobody has points yet."]
        return self.pack(f"{board['name']}:", [f"{i}. {r['name']} {r['points']}" for i, r in enumerate(rows, 1)])

    def cmd_rank(self, steam_id, arg, now):
        def get(cur):
            board = Store.find(cur, arg, now)
            return board, board and Store.rank(cur, board["id"], steam_id)
        res = self.store.run(get, write=False)
        if res is None:
            return ["The leaderboard is not available right now."]
        board, found = res
        if not board:
            return [f"No board called '{arg}'. Try /boards."]
        if not found:
            return [f"{board['name']}: you have no points yet. Finish a ranked throwdown with two or more players."]
        rank, total, row = found
        return [f"{board['name']}: you are #{rank} of {total} with {row['points']} points "
                f"({row['wins']} won / {row['played']} played)."]

    def cmd_boards(self, steam_id, arg, now):
        def get(cur):
            self.store.active(cur, self.kinds, now)
            return cur.execute(
                "SELECT b.name, b.closed_at, (SELECT COUNT(*) FROM scores s WHERE s.board = b.id AND s.points > 0) AS n "
                "FROM boards b WHERE b.kind != 'mode' ORDER BY b.kind='general' DESC, b.closed_at IS NOT NULL, b.created_at DESC LIMIT 12").fetchall()
        rows = self.store.run(get)
        if rows is None:
            return ["The leaderboard is not available right now."]
        entries = [f"{r['name']}{' (ended)' if r['closed_at'] else ''}: {r['n']}" for r in rows]
        return self.pack("Boards (players):", entries) + ["Add jam, skate or spot for one game: /top week skate."]

    def admin(self, by, arg, now):
        word, _, rest = arg.partition(" ")
        word, rest = word.lower(), rest.strip()
        if word == "award":
            return self.admin_award(by, rest, now)
        if word == "event":
            return self.admin_event(rest, now)
        return ["/lb award <points> <player> [board:<board>] | /lb event start <name> | /lb event end [name]"]

    def admin_award(self, by, rest, now):
        m = re.fullmatch(r"(-?\d{1,6})\s+(.+?)(?:\s+board:(\S+))?", rest)
        if not m:
            return ["Usage: /lb award <points> <player or SteamID64> [board:<board>]"]
        points, who, spec = int(m.group(1)), m.group(2).strip(), m.group(3)
        steam_id = who if re.fullmatch(r"\d{17}", who) else next((i for n, i in self.online.items() if n.lower() == who.lower()), None)

        def apply(cur):
            sid, name = steam_id, who
            if sid is None:
                found = cur.execute("SELECT steam_id, name FROM players WHERE name = ? COLLATE NOCASE", (who,)).fetchall()
                if len(found) != 1:
                    return ("Player not found: use the name of someone online or a SteamID64." if not found
                            else "Several players have that name: use the SteamID64.")
                sid, name = found[0]["steam_id"], found[0]["name"]
            else:
                known = cur.execute("SELECT name FROM players WHERE steam_id = ?", (sid,)).fetchone()
                name = next((n for n, i in self.online.items() if i == sid), known["name"] if known else sid)
            if spec:
                board = Store.find(cur, spec, now)
                if not board or board["closed_at"] or board["kind"] == "mode":
                    return f"No open board called '{spec}' (game mode boards only get throwdown points)."
                boards = [board["id"]]
            else:
                boards = self.store.active(cur, self.kinds, now)
            self.store.add(cur, boards, [(sid, name, points, 0, 0)], f"admin:{by}", now, self.store.server)
            return f"Gave {points:+d} points to {name} on {len(boards)} board(s)."
        reply = self.store.run(apply)
        if reply:
            print(f"[leaderboard] {by}: award {points:+d} to {who}: {reply}", flush=True)
        return [reply or "The leaderboard could not be changed."]

    def admin_event(self, rest, now):
        word, _, event = rest.partition(" ")
        word, event = word.lower(), event.strip()
        if "event" not in self.kinds:
            return ["Events are off (LEADERBOARD_BOARDS)."]
        if word == "start":
            name = re.sub(r"\s+", " ", event)[:40]
            if not slug(name):
                return ["Usage: /lb event start <name>"]

            def start(cur):
                board_id = "event-" + slug(name)
                row = cur.execute("SELECT closed_at FROM boards WHERE id=?", (board_id,)).fetchone()
                if row and not row["closed_at"]:
                    return f"Event '{name}' is already running.", False
                if row:                                 # same name again: a board of its own
                    board_id += "-" + datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%d")
                    if cur.execute("SELECT 1 FROM boards WHERE id=?", (board_id,)).fetchone():
                        return f"An event '{name}' already ran today; pick another name.", False
                self.store.board(cur, board_id, name, "event")
                return f"Event '{name}' started: every ranked result now counts there as well. End it with /lb event end.", True
            res = self.store.run(start)
            if res and res[1]:
                self.send("say " + fit(f"Leaderboard event '{name}' has started! Ranked throwdown results count for it too: /top event"))
            return [res[0] if res else "The leaderboard could not be changed."]
        if word == "end":
            def end(cur):
                rows = cur.execute("SELECT id, name FROM boards WHERE kind='event' AND closed_at IS NULL"
                                   + (" AND (lower(name) = lower(?) OR id = ?)" if event else ""),
                                   (event, "event-" + slug(event)) if event else ()).fetchall()
                if not rows:
                    return None, "No such event is running." if event else "No event is running."
                if len(rows) > 1:
                    return None, "Several events are running: /lb event end <name>"
                cur.execute("UPDATE boards SET closed_at = ?, version = version + 1 WHERE id = ?", (now, rows[0]["id"]))
                return dict(rows[0]), f"Event '{rows[0]['name']}' ended."
            board, reply = self.store.run(end) or (None, "The leaderboard could not be changed.")
            if board:
                self.final(board)
            return [reply]
        return ["Usage: /lb event start <name> | /lb event end [name]"]

    def final(self, board):
        """The result of a finished event goes to chat and Discord."""
        rows = self.store.run(lambda cur: Store.top(cur, board["id"], 10), write=False) or []
        if not rows:
            return
        entries = [f"{i}. {r['name']} {r['points']}" for i, r in enumerate(rows[:3], 1)]
        for line in self.pack(f"Event '{board['name']}' is over:", entries):
            self.send("say " + fit(line))
        if self.post:
            self.post(f"**Event finished: {board['name']}**\n```\n{self.table(rows)}\n```")

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

    def current(self, cur, now):
        """[(board row, top rows)] for the boards shown now: General, this week, the open events."""
        out = []
        for board_id in self.store.active(cur, self.kinds, now):
            out.append((cur.execute("SELECT * FROM boards WHERE id=?", (board_id,)).fetchone(), Store.top(cur, board_id, 10)))
        return out

    def post_chat(self):
        shown = self.store.run(lambda cur: self.current(cur, time.time())) or []
        first = True
        for board, rows in shown:
            rows = rows[:self.size]
            if not rows:
                continue
            entries = [f"{n}. {p['name']} {p['points']}" for n, p in enumerate(rows, 1)]
            if first:
                self.send("announce " + fit(f"{board['name']}: " + " | ".join(entries[:3])))
                first = False
            head = f"Leaderboard ({self.label}, ranked throwdowns):" if board["kind"] == "general" else f"{board['name']}:"
            for line in self.pack(head, entries):
                self.send("say " + fit(line))

    @staticmethod
    def table(rows):
        width = max(len(p["name"]) for p in rows)
        return "\n".join(f"{i:>2}. {p['name']:<{width}}  {p['points']:>5} pts  {p['wins']} won / {p['played']} played"
                         for i, p in enumerate(rows, 1)).replace("```", "'''")

    def post_discord(self, now):
        def due(cur):
            at = cur.execute("SELECT value FROM meta WHERE key='posted_at'").fetchone()
            if at and now - float(at["value"]) < self.interval:
                return None                           # another server posted lately
            shown = self.current(cur, now)
            if not any(b["version"] != b["posted_version"] for b, _ in shown):
                return None                           # nothing new since the last post
            cur.execute("INSERT OR REPLACE INTO meta VALUES ('posted_at', ?)", (str(now),))
            cur.execute("UPDATE boards SET posted_version = version")
            return shown
        parts = [f"**{board['name']}** ({self.label}, ranked throwdowns)\n```\n{self.table(rows[:10 if board['kind'] == 'general' else 5])}\n```"
                 for board, rows in self.store.run(due) or [] if rows]
        if parts:
            self.post("\n".join(parts))

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
    if sqlite3 is None:
        print("[leaderboard] this image's Python has no sqlite3, so the leaderboard is off", flush=True)
        return None
    board = Leaderboard(send, post)
    if not board.ok:
        return None
    threading.Thread(target=board.loop, daemon=True).start()
    return board
