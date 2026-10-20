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
  S.K.A.T.E. (no places in the log) is placed by the fewest missed tricks, then the most landed (equal results share a place), and then scored like those. Unranked
  players keep their place but get nothing.
* Boards (LEADERBOARD_BOARDS, default all): every award is counted on All Time (kind "general"), on the current Year ("2026"), Month ("October 2026") and Week (ISO week, UTC; "Week 41") and on every Event that is open. Events
  are started and ended by admins in the game (/lb event start <name>, /lb event end).
* Every board also has one per game mode (Jam, Spot Battle, S.K.A.T.E.): /top skate, /top week jam.
* Props: /props <player> gives a prop to a player who is online (one per player per 24 hours, at most
  LEADERBOARD_PROPS_PER_DAY a day, 0 = off). Props count on a Props board of each period (/top props,
  /top week props), not on the points boards, and are announced in chat (LEADERBOARD_PROPS_ANNOUNCE).
* The points live in a SQLite database (leaderboard.db), with every award kept in a ledger.
  A leaderboard.json from the earlier versions is imported into All Time once.
* Chat commands (LEADERBOARD_COMMANDS, default on; added to the server's "commands"): /top [board],
  /rank [board], /boards, and /lb for admins (the server itself refuses it for everyone else).
  The server logs a "[command] Name /top week" line for each allowed command; this module answers
  it with a DM, so the reply is only seen by the sender.
* Every LEADERBOARD_INTERVAL minutes (default 60) the top LEADERBOARD_SIZE (default 5) go to
  chat while players are on (LEADERBOARD_INTERVALS: a time per board kind, e.g. general=60,week=30,event=10,
  0 = never), and the top 10 to Discord (LEADERBOARD_DISCORD_INTERVALS the same way) (LEADERBOARD_WEBHOOK, else the public
  feed) when a board changed since the last post. LEADERBOARD_DISCORD chooses which boards it
  lists (general, week, event and the games jam, spot, skate; default all); players ask for any
  board in chat.

LEADERBOARD_SCOPE picks whose points are counted: "shared" (default) keeps one database for every
server that mounts the same folder at /shared (/shared/leaderboard.db); without that mount it
falls back to "server", this server's own database in /data/leaderboard.db. LEADERBOARD_FILE
overrides the file in either scope.

LEADERBOARD_LEAGUE keeps two kinds of servers apart. "ranked" (default) is for a server that forces the
ranked requirements: only ranked players score (see above), in leaderboard.db. "unranked" is for every
other server: everybody scores, in a database of its own (leaderboard-unranked.db next to it), so
results from the two never end up on the same boards. Every answer and post names its league.
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
TRIED = re.compile(r"(?:^|, )(.+?) (\d+) landed / (\d+) missed( \(quit\))?(?=, |$)")
TUNING = re.compile(r"^Players skate with (the game's|their) own physics tuning\.$")
# "[command] Name /top week": logged by the server for an allowed custom command (the line for any
# typed slash command is "[command] Name: /text" and is ignored: the server may have refused it).
COMMAND = re.compile(r"^\[command\] (.+)$")

RULE = "-" * 30       # the server's own reply to a chat command: the line that opens every answer

CHECK_AFTER = 15      # seconds after joining before the mod report is taken as complete
CHAT_MAX = 200        # bytes in one chat line (multiplayer_chat_max_bytes)
ASK_EVERY = 3         # seconds between two answers to the same player
KINDS = ("general", "year", "month", "week", "event")
VERBS = ("top", "rank", "boards", "props", "lb", "team")
# The game modes with a board of their own next to every board: "All Time:jam", "Week 41:skate".
MODES = {"jam": "Jam", "spot": "Spot Battle", "skate": "S.K.A.T.E.", "props": "Props"}
MODE_OF = {"Jam": "jam", "throwdown": "jam", "Spot Battle": "spot", "S.K.A.T.E.": "skate"}
MODE_WORDS = {"jam": "jam", "jams": "jam", "skate": "skate", "s.k.a.t.e": "skate", "s.k.a.t.e.": "skate", "game of skate": "skate",
              "spot": "spot", "spot battle": "spot", "spotbattle": "spot", "battle": "spot", "spot battles": "spot",
              "props": "props", "prop": "props"}

RANKED = "You are RANKED: your throwdown results count for the leaderboard."
UNRANKED_MODS = ("You are NOT ranked: your mods change scoring or physics ({mods}). "
                 "Restart Skate without them to earn leaderboard points.")
UNRANKED_SPEED = "You are NOT ranked until you rejoin: your game ran faster than normal."
UNRANKED_TUNING = ("Nobody is ranked right now: this server lets players skate with their own physics tuning. "
                   "Points count again once it is back on the game's own.")
UNRANKED_SERVER = "This is an UNRANKED server: your results count on its own Unranked boards, apart from the Ranked ones."
UNAVAILABLE = "The leaderboard is not available right now."
CONFIG = "/data/ReSkateServer.json"

# The chat commands the image adds to the server's "commands" (entrypoint.py): the server replies with the
# top rule, the title, the rows and the bottom rule follow as DMs from this module.
COMMANDS = [
    {"name": "top", "reply": RULE},
    {"name": "rank", "reply": RULE},
    {"name": "boards", "reply": RULE},
    {"name": "props", "reply": RULE},
    {"name": "lb", "admin": True, "reply": RULE},
]
TEAM_COMMAND = {"name": "team", "reply": RULE}   # added with TEAMS=true; the leaderboard module answers it through the bridge


# Replies of the same commands in earlier builds: a command with one of these is ours, to be replaced.
OLD_REPLIES = {"Leaderboard (/top week, /top event, /boards):", "Leaderboard (/top week, /top skate, /top week jam, /boards):",
               "Your rank (/rank week, /rank event):", "Leaderboards:", "Leaderboard admin (/lb help):"}


def env(name, default="", environ=None):
    return ((os.environ if environ is None else environ).get(name) or "").strip() or default


def truthy(value):
    return value.strip().lower() in ("1", "true", "yes", "on")


def free_tuning(environ=None):
    """True when the config lets players skate with their own physics tuning (enforce_tuning off).
    Outside the server (environ given: the leaderboard module) from its ENFORCE_TUNING setting."""
    if environ is not None:
        return env("ENFORCE_TUNING", "true", environ).lower() in ("0", "false", "no", "off")
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return (json.load(f).get("anti_cheat") or {}).get("enforce_tuning", True) is False
    except (OSError, ValueError, AttributeError):
        return False


def fit(text, limit=CHAT_MAX):
    data = re.sub(r"[\x00-\x1f\x7f]+", " ", text).encode("utf-8")[:limit]
    return data.decode("utf-8", "ignore")


def kind_of(board_id):
    """general / year / month / week / event, also for a game board ("week-2026-41:jam")."""
    return board_id.split(":")[0].split("-")[0]


def intervals(name, default, environ=None):
    """{kind: seconds} from "general=60,week=30,event=0" (minutes, 0 = never); kinds not named use default."""
    out = {k: default for k in KINDS}
    for item in env(name, "", environ).split(","):
        kind, _, minutes = item.strip().partition("=")
        if kind.strip().lower() in KINDS and minutes.strip().isdigit():
            out[kind.strip().lower()] = 60 * int(minutes)
    return out


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:32]


MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")


def week_of(now):
    year, week, _ = datetime.fromtimestamp(now, timezone.utc).isocalendar()
    return f"week-{year}-{week:02d}", f"Week {week}", week


def period_of(kind, now):
    """(id, name) of the current year / month / week board (UTC)."""
    t = datetime.fromtimestamp(now, timezone.utc)
    if kind == "year":
        return f"year-{t.year}", str(t.year)
    if kind == "month":
        return f"month-{t.year}-{t.month:02d}", f"{MONTHS[t.month - 1]} {t.year}"
    return week_of(now)[:2]


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
CREATE TABLE IF NOT EXISTS props_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, giver TEXT NOT NULL, receiver TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS props_by_giver ON props_log (giver, at);
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
            self.board(cur, "general", "All Time", "general")
            cur.execute("UPDATE boards SET name = 'All Time' WHERE id = 'general' AND name != 'All Time'")   # was "General"
            cur.execute("UPDATE boards SET name = 'All Time' || substr(name, 8) WHERE id LIKE 'general:%' AND name LIKE 'General - %'")
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
        """leaderboard.json of the versions before the database: its players become All Time."""
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
        print(f"[leaderboard] imported {len(players)} players from {legacy} into All Time", flush=True)
        return True

    @staticmethod
    def board(cur, board_id, name, kind):
        cur.execute("INSERT OR IGNORE INTO boards (id, name, kind, created_at) VALUES (?, ?, ?, ?)",
                    (board_id, name, kind, time.time()))

    def active(self, cur, kinds, now):
        """The boards that count new points: All Time, this week, the open events."""
        ids = []
        if "general" in kinds:
            self.board(cur, "general", "All Time", "general")      # (missing after the points were wiped)
            ids.append("general")
        for kind in ("year", "month", "week"):
            if kind in kinds:
                period_id, period_name = period_of(kind, now)
                self.board(cur, period_id, period_name, kind)
                ids.append(period_id)
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
        if spec in ("", "general", "all", "all time", "alltime", "all-time", "g"):
            return cur.execute("SELECT * FROM boards WHERE id='general'").fetchone()
        for kind, words in (("year", ("year", "y", "yearly")), ("month", ("month", "m", "monthly")), ("week", ("week", "w", "weekly"))):
            if spec in words:
                return cur.execute("SELECT * FROM boards WHERE id=?", (period_of(kind, now)[0],)).fetchone()
        if spec in ("event", "e"):
            return cur.execute("SELECT * FROM boards WHERE kind='event' ORDER BY closed_at IS NOT NULL, created_at DESC").fetchone()
        if re.fullmatch(r"\d{4}", spec):
            row = cur.execute("SELECT * FROM boards WHERE id=?", (f"year-{spec}",)).fetchone()
            if row:
                return row
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
    def __init__(self, send, post, environ=None):
        """send(console line) types into the server; post(text) goes to Discord (or None). environ: the
        server's settings (default this process's env; the leaderboard module passes each server's)."""
        self.send, self.post, self.environ = send, post, environ
        self.teams = None      # the leaderboard module sets a provider: tags() -> {SteamID64: tag}, act(who, verb, args) -> result

        def env(name, default=""):
            return globals()["env"](name, default, environ)
        self.active = lambda: True
        self.pool = env("LEADERBOARD_SCOPE", "shared").lower() != "server"
        self.unranked_league = env("LEADERBOARD_LEAGUE", "ranked").lower() == "unranked"
        self.league = "Unranked" if self.unranked_league else "Ranked"
        name = "leaderboard-unranked.db" if self.unranked_league else "leaderboard.db"
        path = env("LEADERBOARD_FILE", ("/shared/" if self.pool else "/data/") + name)
        if self.pool and not os.path.isdir(os.path.dirname(path) or "."):
            print(f"[leaderboard] {os.path.dirname(path)} is not mounted, so the leaderboard is this server's own "
                  "(LEADERBOARD_SCOPE=server); mount one folder there on every server to share it", flush=True)
            self.pool, path = False, "/data/" + name
        legacy = os.path.join(os.path.dirname(path) or ".", "leaderboard.json")
        if path.endswith(".json"):                  # an old LEADERBOARD_FILE: the points move to a .db beside it
            legacy, path = path, path[:-5] + ".db"
        self.store = Store(path, env("SERVER_NAME", "server"))
        self.ok = self.store.setup(legacy if os.path.exists(legacy) and not self.unranked_league else None)
        self.kinds = {k for k in (x.strip().lower() for x in env("LEADERBOARD_BOARDS", ",".join(KINDS)).split(",")) if k in KINDS} or {"general"}
        self.label = "all servers" if self.pool else "this server"
        self.interval = 60 * max(1, int(env("LEADERBOARD_INTERVAL", "60")))
        self.size = max(1, min(10, int(env("LEADERBOARD_SIZE", "5"))))
        self.props_per_day = max(0, int(env("LEADERBOARD_PROPS_PER_DAY", "5")))     # 0: /props is off
        self.props_announce = truthy(env("LEADERBOARD_PROPS_ANNOUNCE", "true"))
        chosen = {x.strip().lower() for x in env("LEADERBOARD_DISCORD", ",".join(KINDS + tuple(MODES))).split(",")}
        self.discord_scopes = {k for k in KINDS if k in chosen} or set(KINDS)   # which boards Discord lists
        self.discord_games = {m for m in MODES if m in chosen}                  # and which game boards of them
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
        self.free_tuning = free_tuning(environ)  # own physics tuning allowed: nobody is ranked
        self.chat_every = intervals("LEADERBOARD_INTERVALS", self.interval, environ)
        self.discord_every = intervals("LEADERBOARD_DISCORD_INTERVALS", self.interval, environ)
        self.chat_at = {}      # kind -> time of the last chat post
        self.discord_at = time.time()
        self.started = time.time()
        self.tags_at, self.tag_cache = 0.0, {}

    # ---- log lines ------------------------------------------------------------------
    def feed(self, line):
        m = LINE.match(line)
        text = m.group(2) if m else line
        with self.lock:
            if UP.match(text):
                self.online.clear(), self.waiting.clear(), self.modded.clear()
                self.speeding.clear(), self.told.clear(), self.admins.clear()
                self.free_tuning = free_tuning(self.environ)
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
        if self.unranked_league:        # nothing is required here, so everybody scores (on the Unranked boards)
            return True
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
        if self.unranked_league:
            text = UNRANKED_SERVER
        elif ranked:
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
        """[(name, place or None, quit, S.K.A.T.E. sort key)] from a finished line."""
        if mode == "S.K.A.T.E.":
            return [(m.group(1), None, bool(m.group(4)), (int(m.group(3)), -int(m.group(2)))) for m in TRIED.finditer(results)]
        return [(m.group(2), int(m.group(1)), bool(m.group(4)), None) for m in PLACED.finditer(results)]

    def score(self, mode, results):
        rows = self.results(mode, results)
        finished = [r for r in rows if not r[2]]
        if len(finished) < 2 or not self.points:
            return
        if mode == "S.K.A.T.E.":      # no places in the log: fewest missed, then most landed; equal results share a place
            finished = [(name, 1 + sum(o[3] < key for o in finished), quit_, key) for name, _p, quit_, key in finished]
        awards = []
        for name, place, _quit, _missed in finished:
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
                title, lines = "Leaderboard admin", self.admin(name, arg, now)
            else:
                title, lines = getattr(self, "cmd_" + verb)(steam_id, arg, now)
        except Exception as e:                          # a bad command must not stop the log reader
            print(f"[leaderboard] /{verb} {arg!r}: {e!r}", flush=True)
            title, lines = "Leaderboard", ["Something went wrong with that command."]
        if verb != "team":
            title = f"{title} ({self.league})"
        for text in [title] + lines:                    # after the server's own rule: the board, then one row per line
            self.send(fit(f"msg {steam_id} {text}", CHAT_MAX + 30))

    @staticmethod
    def pack(head, entries):
        """Chat lines of at most CHAT_MAX bytes: head, then the entries side by side."""
        lines, line = [], head
        for entry in entries:
            if not line:
                line = entry
            elif len((line + "  " + entry).encode("utf-8")) > CHAT_MAX:
                lines.append(line)
                line = entry
            else:
                line += "  " + entry
        return lines + [line] if line else lines

    def cmd_top(self, steam_id, arg, now):
        def get(cur):
            board = Store.find(cur, arg, now)
            return board, board and Store.top(cur, board["id"], self.size)
        res = self.store.run(get, write=False)
        if res is None:
            return "Leaderboard", [UNAVAILABLE]
        board, rows = res
        if not board:
            return "Leaderboard", [f"No board called '{arg}'. Try /boards."]
        if not rows:
            return f"Top - {board['name']}", ["Nobody has points yet."]
        return f"Top {len(rows)} - {board['name']}", [f"{i}. {self.shown(r)}  {r['points']}" for i, r in enumerate(rows, 1)]

    def cmd_rank(self, steam_id, arg, now):
        def get(cur):
            board = Store.find(cur, arg, now)
            return board, board and Store.rank(cur, board["id"], steam_id)
        res = self.store.run(get, write=False)
        if res is None:
            return "Leaderboard", [UNAVAILABLE]
        board, found = res
        if not board:
            return "Leaderboard", [f"No board called '{arg}'. Try /boards."]
        title = f"Your rank - {board['name']}"
        if not found:
            return title, ["You have no points yet. Finish a ranked throwdown with two or more players."]
        rank, total, row = found
        return title, [f"You are #{rank} of {total} with {row['points']} points ({row['wins']} won / {row['played']} played)."]

    def cmd_boards(self, steam_id, arg, now):
        def get(cur):
            self.store.active(cur, self.kinds, now)
            return cur.execute(
                "SELECT b.name, b.closed_at, (SELECT COUNT(*) FROM scores s WHERE s.board = b.id AND s.points > 0) AS n "
                "FROM boards b WHERE b.kind != 'mode' ORDER BY b.kind='general' DESC, b.closed_at IS NOT NULL, b.created_at DESC LIMIT 12").fetchall()
        rows = self.store.run(get)
        if rows is None:
            return "Leaderboard", [UNAVAILABLE]
        return "Boards", [f"{r['name']}{' (ended)' if r['closed_at'] else ''}  -  {r['n']} players" for r in rows]

    # ---- teams ------------------------------------------------------------------------------------
    def tag_map(self):
        """{SteamID64: team tag}, from the teams provider (the bridge), looked up at most every 30 seconds."""
        if self.teams is None:
            return {}
        if time.time() - self.tags_at > 30:
            self.tags_at = time.time()
            try:
                self.tag_cache = self.teams.tags()
            except Exception as e:           # no bridge, no teams: boards show names only
                print(f"[leaderboard] teams: {e!r}", flush=True)
        return self.tag_cache

    def shown(self, row):
        """A board row's name with its team tag: [TAG] Name."""
        tag = self.tag_map().get(row["steam_id"]) if "steam_id" in row.keys() else None
        return f"[{tag}] {row['name']}" if tag else row["name"]

    def cmd_team(self, steam_id, arg, now):
        """/team ...: apply for a team, join, leave, configure; the teams provider (the bridge) holds them."""
        if self.teams is None:
            return "Team", ["Teams are not available on this server."]
        words = arg.split()
        sub, rest = (words[0].lower() if words else "help"), " ".join(words[1:])
        name = next((n for n, i in self.online.items() if i == steam_id), "")
        if sub == "who":
            tags = self.tag_map()
            teamed = sorted((tags[i], n) for n, i in self.online.items() if i in tags)
            lines = self.pack("Teams online:", [f"[{t}] {n}" for t, n in teamed])
            return "Team", lines or ["Nobody from a team is on right now."]
        args = {}
        if sub == "apply":
            tag, _, team = rest.partition(" ")
            args = {"tag": tag, "name": team.strip()}
        elif sub in ("info", "join"):
            args = {"team": rest}
        elif sub in ("invite", "kick", "owner", "leader"):
            found = self.find_online(rest)
            args = {"player": found[1] if found else rest}
        elif sub == "set":
            key, _, value = rest.partition(" ")
            args = {"key": key, "value": value.strip()}
        elif sub == "disband":
            args = {"confirm": rest}
        result = self.teams.act({"steam_id": steam_id, "name": name}, sub, args)
        return "Team", [part for text in result.get("lines") or ["Done."] for part in self.wrap(text)]

    @staticmethod
    def wrap(text, limit=CHAT_MAX - 40):
        """Chat lines of at most `limit` bytes, split between words."""
        lines, line = [], ""
        for word in text.split(" "):
            if line and len((line + " " + word).encode("utf-8")) > limit:
                lines.append(line)
                line = word
            else:
                line = f"{line} {word}" if line else word
        return lines + [line] if line else lines

    def find_online(self, text):
        """(name, SteamID64) of the online player called text: the exact name, else the only name starting with it."""
        text = text.strip().lower()
        exact = [(n, i) for n, i in self.online.items() if n.lower() == text]
        starts = [(n, i) for n, i in self.online.items() if n.lower().startswith(text)]
        found = exact or starts
        return found[0] if len(found) == 1 and text else None

    def cmd_props(self, steam_id, arg, now):
        """Give props to another player online: one per player per 24 hours, at most LEADERBOARD_PROPS_PER_DAY
        a day. They count on the Props boards (/top props), not on the points boards."""
        if not self.props_per_day:
            return "Props", ["Props are off on this server."]
        giver = next((n for n, i in self.online.items() if i == steam_id), steam_id)
        if not arg:
            def mine(cur):
                board = Store.find(cur, "props", now)
                return Store.rank(cur, board["id"], steam_id) if board else None
            found = self.store.run(mine, write=False)
            have = f"You have {found[2]['points']} props (#{found[0]} of {found[1]})." if found else "You have no props yet."
            return "Props", [have, "Give them with /props <player>. See /top props."]
        target = self.find_online(arg)
        if not target:
            return "Props", [f"Nobody online is called '{arg}' (or several names start like that)."]
        name, target_id = target
        if target_id == steam_id:
            return "Props", ["You can't give props to yourself."]

        def give(cur):
            since = now - 86400
            given = cur.execute("SELECT COUNT(*) FROM props_log WHERE giver=? AND at>?", (steam_id, since)).fetchone()[0]
            if given >= self.props_per_day:
                return "limit", given
            if cur.execute("SELECT 1 FROM props_log WHERE giver=? AND receiver=? AND at>?", (steam_id, target_id, since)).fetchone():
                return "again", given
            boards = []
            for base in self.store.active(cur, self.kinds, now):
                row = cur.execute("SELECT name FROM boards WHERE id=?", (base,)).fetchone()
                self.store.board(cur, f"{base}:props", f"{row['name']} - Props", "mode")
                boards.append(f"{base}:props")
            self.store.add(cur, boards, [(target_id, name, 1, 0, 0)], f"props:{giver}", now, self.store.server)
            cur.execute("INSERT INTO props_log (at, giver, receiver) VALUES (?, ?, ?)", (now, steam_id, target_id))
            total = cur.execute("SELECT points FROM scores WHERE board=? AND steam_id=?", (boards[0], target_id)).fetchone()
            return "ok", given + 1, total["points"] if total else 1
        res = self.store.run(give)
        if res is None:
            return "Props", [UNAVAILABLE]
        state, used, *total = res
        if state == "limit":
            return "Props", [f"You have given {used} props in the last 24 hours, the most for now."]
        if state == "again":
            return "Props", [f"You gave {name} props in the last 24 hours already."]
        self.send(fit(f"msg {target_id} {giver} gave you props! You have {total[0]} now (/top props).", CHAT_MAX + 30))
        if self.props_announce:
            self.send("say " + fit(f"{giver} gave props to {name}!"))
        return "Props", [f"You gave props to {name}. {self.props_per_day - used} left in the next 24 hours."]

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
        for kind in self.kinds:
            every = self.chat_every[kind]
            if every and now - self.chat_at.get(kind, self.started) >= every:
                self.chat_at[kind] = now
                if players_on:
                    self.post_chat(kind)
        if self.post and now - self.discord_at >= 60:
            self.discord_at = now
            self.post_discord(now)

    def current(self, cur, now, discord=False):
        """[(board row, top rows)] for the boards shown now: All Time, this week, the open events. For
        Discord (LEADERBOARD_DISCORD) only the chosen ones, each followed by its chosen game boards."""
        out = []
        for board_id in self.store.active(cur, self.kinds, now):
            kind = board_id.split("-")[0] if board_id != "general" else "general"
            if discord and kind not in self.discord_scopes:
                continue
            ids = [board_id]
            if discord and self.discord_games:
                ids += [r["id"] for r in cur.execute("SELECT id FROM boards WHERE kind='mode' AND id LIKE ? ORDER BY id", (board_id + ":%",))
                        if r["id"].rsplit(":", 1)[1] in self.discord_games]
            for shown_id in ids:
                row = cur.execute("SELECT * FROM boards WHERE id=?", (shown_id,)).fetchone()
                if row:
                    out.append((row, Store.top(cur, shown_id, 10)))
        return out

    def post_chat(self, kind):
        shown = self.store.run(lambda cur: self.current(cur, time.time())) or []
        first = True
        for board, rows in shown:
            rows = rows[:self.size] if board["kind"] == kind else []
            if not rows:
                continue
            entries = [f"{n}. {self.shown(p)} {p['points']}" for n, p in enumerate(rows, 1)]
            if first:
                self.send("announce " + fit(f"{board['name']}: " + " | ".join(entries[:3])))
                first = False
            head = f"Leaderboard ({self.label}, {self.league.lower()} throwdowns):" if board["kind"] == "general" else f"{board['name']}:"
            for line in self.pack(head, entries):
                self.send("say " + fit(line))

    def table(self, rows, props=False):
        names = [self.shown(p) for p in rows]
        width = max(len(n) for n in names)
        return "\n".join((f"{i:>2}. {n:<{width}}  {p['points']:>5} props" if props else
                          f"{i:>2}. {n:<{width}}  {p['points']:>5} pts  {p['wins']} won / {p['played']} played")
                         for i, (n, p) in enumerate(zip(names, rows), 1)).replace("```", "'''")

    def post_discord(self, now):
        def due(cur):
            shown = self.current(cur, now, True)
            late = set()                          # the kinds whose interval is over and that have news
            for kind in {kind_of(b["id"]) for b, _ in shown}:
                every = self.discord_every[kind]
                at = cur.execute("SELECT value FROM meta WHERE key=?", ("posted_at:" + kind,)).fetchone()
                news = any(b["version"] != b["posted_version"] for b, _ in shown if kind_of(b["id"]) == kind)
                if every and news and (not at or now - float(at["value"]) >= every):
                    late.add(kind)                # (another server's post counts: the file is shared)
            for kind in late:
                cur.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", ("posted_at:" + kind, str(now)))
            for b, _ in shown:
                if kind_of(b["id"]) in late:
                    cur.execute("UPDATE boards SET posted_version = version WHERE id = ?", (b["id"],))
            return [(b, rows) for b, rows in shown if kind_of(b["id"]) in late]
        parts = [f"**{board['name']}** ({self.label}, {'props from players' if board['id'].endswith(':props') else self.league.lower() + ' throwdowns'})\n```\n"
                 f"{self.table(rows[:10 if board['kind'] == 'general' else 5], board['id'].endswith(':props'))}\n```"
                 for board, rows in self.store.run(due) or [] if rows]
        message = ""
        for part in parts:                  # Discord takes 2000 characters per message
            if message and len(message) + len(part) + 1 > 1900:
                self.post(message)
                message = ""
            message = message + "\n" + part if message else part
        if message:
            self.post(message)

    def loop(self):
        while True:
            try:
                if self.active():           # off while the leaderboard module outside runs it
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
