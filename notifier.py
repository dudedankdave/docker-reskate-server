"""Discord feeds and the ReSkate update policy. Runs as threads inside supervisor.py's process;
nothing here may disturb the server: every failure is swallowed and written to
/data/DiscordWebhook.log instead.

Four webhooks, each optional:
  DISCORD_WEBHOOK_ESSENTIALS  what an admin has to know: server up + join code, config problems,
                              warnings/errors, crashes, releases, update approvals and results,
                              mod updates. The only feed that pings (DISCORD_MENTION_IDS).
  DISCORD_WEBHOOK_LOG         the whole console as code blocks.
  DISCORD_WEBHOOK_PUBLIC      for players: joins, leaves, throwdowns, server up, update countdowns.
  DISCORD_WEBHOOK_CHAT        in-game chat only.
Older names: DISCORD_WEBHOOK_ADMIN / DISCORD_WEBHOOK = essentials + log (log off with
DISCORD_CONSOLE=false), DISCORD_WEBHOOK_USER = public.

Updates (UPDATE_MODE=auto, see updater.py), once a newer release is found, by UPDATE_POLICY:
  instant    install right away (players are told and kicked)
  timed      announce in game, wait UPDATE_COUNTDOWN minutes, install
  scheduled  wait for UPDATE_SCHEDULE (UTC, "04:00" or "sat,sun 04:00"), then as timed
  ask        a Discord bot (DISCORD_BOT_TOKEN) posts in DISCORD_APPROVAL_CHANNEL and adds
             ✅/❌; the first reaction by one of DISCORD_MENTION_IDS decides, then as timed
The countdown is skipped while nobody is on. `update` in the console installs at once in any
policy. UPDATE_MODE=pinned only tells the essentials feed that a newer image is due.
"""
import json
import os
import re
import subprocess
import threading
import time
import urllib.parse

import updater

LOG = "/data/ReSkateServer.log"
STATE = "/data/.update-state.json"
PINNED_STATE = "/data/.discord-update-notified"
MODS_STATE = "/data/.discord-mods-notified"
NOTES = "/data/DiscordWebhook.log"
REPO = updater.REPO
HUB = "dudedankdave/reskate-server"
API = "https://discord.com/api/v10"
YES, NO = "\u2705", "\u274c"
MODS_CHECK_EVERY = 3600
FLUSH_AFTER = 3          # seconds a console line may wait to be batched
MAX_PENDING = 300        # lines kept while Discord is unreachable
DATE = re.compile(r"^\[\d{4}-\d{2}-\d{2} (\d{2}:\d{2}:\d{2})\]")
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
# Console commands whose answer is posted to the log feed as its own titled block.
INFO_COMMANDS = {"help", "status", "players", "reserved", "net", "bans", "maps", "map-pool", "rotation",
                 "votes", "parties", "score-check", "score-allow", "announcements", "objects", "admin"}
COMMAND_WINDOW = 3       # seconds of log after a command that count as its answer
SCHEDULE = re.compile(r"^(?:([a-z]{3}(?:,[a-z]{3})*) )?(\d{1,2}):(\d{2})$")


def note(message):
    try:
        if os.path.exists(NOTES) and os.path.getsize(NOTES) > 50_000:
            os.truncate(NOTES, 0)
        with open(NOTES, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except OSError:
        pass


def truthy(value, default):
    value = (value or "").strip().lower()
    return default if not value else value in ("1", "true", "yes", "on")


def curl(args, payload=None, timeout=20):
    """Returns (http_code, body); (0, error text) when curl itself failed."""
    cmd = ["curl", "-sS", "-m", str(timeout), "-w", "\n%{http_code}", *args]
    try:
        run = subprocess.run(cmd, input=payload, capture_output=True, timeout=timeout + 5)
    except (OSError, subprocess.SubprocessError) as e:
        return 0, str(e)
    out = run.stdout.decode("utf-8", "replace")
    body, _, code = out.rpartition("\n")
    if run.returncode != 0:
        return 0, run.stderr.decode("utf-8", "replace").strip()
    return int(code) if code.isdigit() else 0, body


def request(url, payload=None, method=None, headers=()):
    """JSON request with Discord's rate limits honoured. Returns the parsed reply, or None."""
    args = [h for pair in (("-H", h) for h in headers) for h in pair]
    data = None
    if payload is not None:
        args += ["-H", "Content-Type: application/json", "--data-binary", "@-"]
        data = json.dumps(payload).encode("utf-8")
    if method:
        args += ["-X", method] + (["-H", "Content-Length: 0"] if data is None else [])
    for _ in range(4):
        code, body = curl(args + [url], data)
        if 200 <= code < 300:
            try:
                return json.loads(body) if body.strip() else {}
            except ValueError:
                return {}
        if code == 429:
            try:
                wait = float(json.loads(body).get("retry_after", 2))
            except (ValueError, AttributeError):
                wait = 2
            time.sleep(min(max(wait, 1), 30))
            continue
        note(f"{method or ('POST' if data else 'GET')} {url.split('?')[0][:60]} failed: HTTP {code} {body[:200]}")
        return None
    note("still rate limited after retries")
    return None


def username():
    name = os.environ.get("DISCORD_USERNAME") or os.environ.get("SERVER_NAME") or ""
    # Discord rejects webhook names containing these words (server names often hold a discord.gg link)
    name = re.sub(r"(?i)\S*(discord|clyde)\S*", "", name)
    name = re.sub(r"\s+", " ", name)
    name = re.sub(r"(\s*\|\s*)+$", "", name).strip(" |-")
    return (name or "ReSkate server")[:80]


def clean(line):
    line = DATE.sub(r"[\1]", line.rstrip("\r"))
    return line.replace("```", "'''")[:500]


JOINED = re.compile(r"^(.+) joined \(\d+(?:, admin)?\), (\d+/\d+) players, loaded in \d+ s$")
LEFT = re.compile(r"^(.+) left \(.*\)$")
UP = re.compile(r"^.+ is up on .+ for \d+ players\.$")
TAGGED = re.compile(r"^\[(?:chat|admin|join|objects)\] ")
LINE = re.compile(r"^\[(\d\d:\d\d:\d\d)\] (.*)$")
ESSENTIAL = re.compile(r"(?i)\bcode\b|Config problem|\bWARNING\b|\bERROR\b|\bfailed\b")


def public_line(line):
    """The player-facing version of a cleaned console line, or None when it is admin-only:
    joins and leaves (no Steam IDs, no leave reasons), throwdowns and the server-up line."""
    m = LINE.match(line)
    if not m:
        return None
    stamp, text = m.groups()
    if TAGGED.match(text):
        return None
    if text.startswith("[throwdown] ") or UP.match(text):
        return line
    j = JOINED.match(text)
    if j:
        return f"[{stamp}] {j.group(1)} joined, {j.group(2)} players"
    left = LEFT.match(text)
    if left:
        return f"[{stamp}] {left.group(1)} left"
    return None


def chat_line(line):
    """An in-game chat line without its [chat] tag, or None for anything else."""
    m = LINE.match(line)
    if not m or not m.group(2).startswith("[chat] "):
        return None
    return f"[{m.group(1)}] {m.group(2)[7:]}"


def essential_line(line):
    """Server up, the join code, config problems, warnings and errors; never chat or players."""
    m = LINE.match(line)
    text = m.group(2) if m else line
    if TAGGED.match(text) or JOINED.match(text) or LEFT.match(text) or text.startswith("[throwdown] "):
        return None
    return line if UP.match(text) or ESSENTIAL.search(text) else None


class Feed:
    """Batches console lines for one webhook; `convert` filters/rewrites them (None = every line)."""

    def __init__(self, hook, convert=None):
        self.hook, self.convert = hook, convert
        self.pending, self.first = [], 0.0

    def add(self, lines):
        if self.convert:
            lines = [x for x in map(self.convert, lines) if x]
        if lines and not self.pending:
            self.first = time.time()
        self.pending += lines
        if len(self.pending) > MAX_PENDING:
            self.pending = [f"... {len(self.pending) - MAX_PENDING} lines skipped"] + self.pending[-MAX_PENDING:]

    def flush_if_due(self):
        if self.pending and (time.time() - self.first >= FLUSH_AFTER or sum(map(len, self.pending)) > 1700):
            batch, self.pending = self.pending, []
            chunk, size = [], 0
            for line in batch + [None]:
                if line is not None and size + len(line) + 1 <= 1850:
                    chunk.append(line)
                    size += len(line) + 1
                    continue
                if chunk:
                    self.hook.send("```\n" + "\n".join(chunk) + "\n```")
                chunk, size = ([line], len(line) + 1) if line is not None else ([], 0)


class Hook:
    def __init__(self, url, name, mentions=()):
        self.url, self.name, self.mentions = url, name, list(mentions)

    def send(self, text, ping=False):
        if not self.url:
            return True
        users = self.mentions if ping else []
        if users:
            text = " ".join(f"<@{i}>" for i in users) + " " + text
        return request(self.url, {"username": self.name, "content": text[:2000],
                                  "allowed_mentions": {"parse": [], "users": users}}) is not None


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_json(path, data):
    try:
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(path + ".tmp", path)
    except OSError as e:
        note(f"cannot write {path}: {e}")


def parse_schedule(value):
    """'04:00' or 'sat,sun 04:00' (UTC) -> (set of weekday numbers or None, hour, minute)."""
    m = SCHEDULE.match((value or "").strip().lower())
    if not m or int(m.group(2)) > 23 or int(m.group(3)) > 59:
        raise ValueError(value)
    days = None
    if m.group(1):
        days = {DAYS.index(d) for d in m.group(1).split(",") if d in DAYS}
        if len(days) != len(m.group(1).split(",")):
            raise ValueError(value)
    return days, int(m.group(2)), int(m.group(3))


class Notifier:
    def __init__(self, sup, mode, image_version):
        self.sup, self.mode, self.image_version = sup, mode, image_version
        legacy = (os.environ.get("DISCORD_WEBHOOK_ADMIN") or os.environ.get("DISCORD_WEBHOOK") or "").strip()
        name = username()
        self.mentions = [i.strip() for i in os.environ.get("DISCORD_MENTION_IDS", "").split(",") if i.strip()]
        self.essentials = Hook(os.environ.get("DISCORD_WEBHOOK_ESSENTIALS", "").strip() or legacy, name, self.mentions)
        log = os.environ.get("DISCORD_WEBHOOK_LOG", "").strip()
        if not log and legacy and truthy(os.environ.get("DISCORD_CONSOLE"), True):
            log = legacy
        self.log = Hook(log, name)
        self.public = Hook((os.environ.get("DISCORD_WEBHOOK_PUBLIC") or os.environ.get("DISCORD_WEBHOOK_USER") or "").strip(), name)
        self.chat = Hook(os.environ.get("DISCORD_WEBHOOK_CHAT", "").strip(), name)
        self.policy = (os.environ.get("UPDATE_POLICY") or "timed").strip().lower()
        self.countdown = int(os.environ.get("UPDATE_COUNTDOWN") or 10)
        self.schedule = parse_schedule(os.environ["UPDATE_SCHEDULE"]) if self.policy == "scheduled" else None
        self.check_every = 60 * int(os.environ.get("UPDATE_CHECK_MINUTES") or (30 if mode == "auto" else 180))
        self.bot = (os.environ.get("DISCORD_BOT_TOKEN") or "").strip()
        self.channel = (os.environ.get("DISCORD_APPROVAL_CHANNEL") or "").strip()
        self.players = set()
        self.busy = None             # release version being handled
        self.lock = threading.Lock()
        self.installing = threading.Lock()
        sup.notify = self.event
        if mode == "auto":
            sup.on_update = lambda: self.check(now=True)

    # ---- console ------------------------------------------------------------------
    def track(self, line):
        m = LINE.match(line)
        text = m.group(2) if m else line
        if UP.match(text):
            self.players.clear()
        elif j := JOINED.match(text):
            self.players.add(j.group(1))
        elif left := LEFT.match(text):
            self.players.discard(left.group(1))

    def console_loop(self):
        feeds = [Feed(h, c) for h, c in ((self.essentials, essential_line), (self.log, None),
                                         (self.public, public_line), (self.chat, chat_line)) if h.url]
        try:
            st = os.stat(LOG)
            state = {"ino": st.st_ino, "pos": st.st_size}   # only what the server writes from now on
        except FileNotFoundError:
            state = {"ino": None, "pos": 0}
        answer, answered = [], None
        while True:
            try:
                new = read_new(state)
                for line in new:
                    self.track(line)
                cmd, at = self.sup.last_command
                word = cmd.split(" ", 1)[0].lower()
                if word in INFO_COMMANDS and time.time() - at < COMMAND_WINDOW:
                    answer += new
                    answered = cmd
                    new = []
                elif answer:
                    self.post_answer(answered, answer)
                    answer = []
            except Exception as e:  # never let the sidecar die
                note(f"console loop: {e!r}")
                new = []
            for feed in feeds:
                try:
                    feed.add(new)
                    feed.flush_if_due()
                except Exception as e:
                    note(f"console feed: {e!r}")
            time.sleep(1)

    def post_answer(self, cmd, lines):
        """A console command's answer as one titled block in the log feed (essentials if no log feed)."""
        hook = self.log if self.log.url else self.essentials
        title = f"**Console:** `{cmd.replace('`', chr(39))[:100]}`"
        chunk = []
        for line in lines + [None]:
            if line is not None and sum(len(x) + 1 for x in chunk) + len(line) < 1800:
                chunk.append(line)
                continue
            if chunk:
                hook.send(f"{title}\n```ini\n" + "\n".join(chunk) + "\n```")
                title = f"**Console:** `{cmd[:100]}` (continued)"
            chunk = [line] if line is not None else []

    # ---- supervisor events --------------------------------------------------------
    def event(self, kind, **d):
        if kind in ("installed", "failed") and self.installing.locked():
            self.installing.release()
        try:
            if kind == "installed":
                self.essentials.send(f"**UPDATED** to ReSkate **{d['version']}**, the server is starting again.")
                self.public.send(f"The server is updated to ReSkate **{d['version']}** and starting again.")
                state = read_json(STATE)
                state["installed"] = d["version"]
                write_json(STATE, state)
            elif kind == "failed":
                self.essentials.send(f"**UPDATE FAILED**: ReSkate {d['version']}: {d['error']}", ping=True)
            elif kind == "rolled_back":
                self.essentials.send(f"**ROLLED BACK**: ReSkate {d['version']} stopped (exit {d['code']}) right after "
                                     f"the update, back on **{d['now']}**. It will not be installed again by itself; "
                                     "type `update` in the console to retry.", ping=True)
                state = read_json(STATE)
                state.update(version=d["version"], declined=True)
                write_json(STATE, state)
            elif kind == "exited":
                self.essentials.send(f"**SERVER STOPPED** (exit code {d['code']}). Docker restarts it if the "
                                     "restart policy allows.", ping=d["code"] != 0)
        except Exception as e:
            note(f"event {kind}: {e!r}")

    # ---- updates ------------------------------------------------------------------
    def update_loop(self):
        time.sleep(60)
        while True:
            try:
                self.check()
            except Exception as e:
                note(f"update check: {e!r}")
            time.sleep(self.check_every)

    def running(self):
        return updater.version_of(self.sup.folder) or self.image_version

    def check(self, now=False):
        try:
            info = updater.latest()
        except Exception as e:
            note(f"release check failed: {e}")
            if now:
                print(f"[update] release check failed: {e}", flush=True)
            return
        version, have = info["version"], self.running()
        if updater.numbers(version) <= updater.numbers(have):
            if now:
                print(f"[update] already on the latest ReSkate release ({have})", flush=True)
            return
        if self.mode != "auto":
            return self.pinned_notice(version, have)
        if now:
            threading.Thread(target=self.apply, args=(info, 0), daemon=True).start()
            return
        state = read_json(STATE)
        if state.get("version") == version and state.get("declined"):
            return
        with self.lock:
            if self.busy == version:
                return
            self.busy = version
        threading.Thread(target=self.handle, args=(info, have), daemon=True).start()

    def handle(self, info, have):
        version = info["version"]
        try:
            state = read_json(STATE)
            if state.get("version") != version:
                state = {"version": version}
            how = {
                "instant": "Installing it now.",
                "timed": f"Installing it in {self.countdown} min (sooner if nobody is on).",
                "scheduled": f"Installing it at the next update slot ({os.environ.get('UPDATE_SCHEDULE')} UTC).",
                "ask": "Waiting for approval.",
            }[self.policy]
            if not state.get("announced"):
                if self.essentials.send(f"**UPDATE AVAILABLE**: ReSkate **{version}** is out, this server runs "
                                        f"**{have}**. {how} `update` in the console installs it now.\n"
                                        f"https://github.com/{REPO}/releases/tag/v{version}", ping=self.policy == "ask"):
                    state["announced"] = True
                    write_json(STATE, state)
            if self.policy == "scheduled":
                self.wait_for_slot(version)
            elif self.policy == "ask" and not self.approved(info, have, state):
                return
            countdown = 0 if self.policy == "instant" else self.countdown
            if self.outdated(version):
                self.apply(info, countdown)
        except Exception as e:
            note(f"update {version}: {e!r}")
        finally:
            with self.lock:
                if self.busy == version:
                    self.busy = None

    def outdated(self, version):
        """False once something else (console `update`) already installed this version."""
        return updater.numbers(self.running()) < updater.numbers(version)

    def wait_for_slot(self, version):
        days, hour, minute = self.schedule
        while self.outdated(version):
            t = time.gmtime()
            if (days is None or t.tm_wday in days) and (t.tm_hour, t.tm_min) == (hour, minute):
                return
            time.sleep(20)

    def apply(self, info, countdown):
        version = info["version"]
        if not self.outdated(version) or not self.installing.acquire(blocking=False):
            return                   # released by event() once the install is done or failed
        print(f"[update] downloading ReSkate {version}", flush=True)
        try:
            staged = updater.stage(info)
        except Exception as e:
            print(f"[update] ReSkate {version} not installed: {e}", flush=True)
            self.event("failed", version=version, error=str(e))
            return
        marks = sorted({m for m in (countdown, 5, 1) if 0 < m <= countdown}, reverse=True)
        deadline = time.time() + countdown * 60
        for i, minutes in enumerate(marks):
            if not self.players:
                break
            self.sup.send(f"announce Server restarts in {minutes} min to update ReSkate to {version}")
            self.public.send(f"Restarting in **{minutes} min** to update ReSkate to **{version}**.")
            if i == 0:
                self.essentials.send(f"Countdown started: installing ReSkate **{version}** in {minutes} min.")
            until = deadline - (marks[i + 1] if i + 1 < len(marks) else 0) * 60
            while time.time() < until and self.players:
                time.sleep(5)
        if self.players:
            self.sup.send(f"announce Restarting now for ReSkate {version}, rejoin in a minute!")
            time.sleep(5)
        self.public.send(f"Restarting now to update ReSkate to **{version}**.")
        self.essentials.send(f"Installing ReSkate **{version}** (was {self.running()}).")
        self.sup.install(staged, version)

    def approved(self, info, have, state):
        """Ask policy: True once an approver reacted ✅, False on ❌ (that version is skipped)."""
        version = info["version"]
        if not (self.bot and self.channel):
            note("UPDATE_POLICY=ask without DISCORD_BOT_TOKEN/DISCORD_APPROVAL_CHANNEL: waiting for `update` in the console")
            while self.outdated(version):
                time.sleep(60)
            return False
        auth = [f"Authorization: Bot {self.bot}", "User-Agent: DiscordBot (reskate-server-image, 1)"]
        base = f"{API}/channels/{self.channel}/messages"
        msg = state.get("ask_message")
        if not msg:
            who = " ".join(f"<@{i}>" for i in self.mentions)
            reply = request(base, {"content": f"{who} **APPROVE UPDATE?** {username()}: ReSkate **{version}** "
                                              f"(running {have}). React {YES} to install (with a {self.countdown} min "
                                              f"countdown for players) or {NO} to skip this version.".strip(),
                                   "allowed_mentions": {"parse": [], "users": self.mentions}}, headers=auth)
            if not reply or "id" not in reply:
                self.essentials.send("**APPROVAL FAILED**: the bot could not post in DISCORD_APPROVAL_CHANNEL "
                                     "(see /data/DiscordWebhook.log). `update` in the console installs it.", ping=True)
                while self.outdated(version):
                    time.sleep(60)
                return False
            msg = reply["id"]
            state["ask_message"] = msg
            write_json(STATE, state)
            for emoji in (YES, NO):
                request(f"{base}/{msg}/reactions/{urllib.parse.quote(emoji)}/@me", method="PUT", headers=auth)
                time.sleep(1)
        while self.outdated(version):
            for emoji, verdict in ((YES, True), (NO, False)):
                users = request(f"{base}/{msg}/reactions/{urllib.parse.quote(emoji)}?limit=100", headers=auth) or []
                for u in users if isinstance(users, list) else []:
                    if u.get("bot") or (self.mentions and u.get("id") not in self.mentions):
                        continue
                    word = "Approved" if verdict else "Skipped"
                    request(f"{base}/{msg}", {"content": f"**{word}** by <@{u['id']}>: ReSkate **{version}**.",
                                              "allowed_mentions": {"parse": []}}, method="PATCH", headers=auth)
                    if not verdict:
                        state["declined"] = True
                        write_json(STATE, state)
                    return verdict
            time.sleep(20)
        return False

    def pinned_notice(self, latest, running):
        published = curl(["-o", "/dev/null", f"https://hub.docker.com/v2/repositories/{HUB}/tags/{latest}"])[0] == 200
        state = read_json(PINNED_STATE)
        if state.get("version") == latest and (state.get("published") or not published):
            return                                          # already announced, nothing new to say
        if published:
            how = ("Update with `docker compose pull && docker compose up -d`, or set `UPDATE_MODE=auto`. "
                   "The restart kicks everyone and changes the join code.")
        else:
            how = f"`{HUB}:{latest}` is not on Docker Hub yet; `UPDATE_MODE=auto` would install it without a new image."
        if state.get("version") == latest:                  # earlier message said "not published yet"
            text = f"**Image available**: `{HUB}:{latest}` is on Docker Hub now. {how}"
        else:
            text = (f"**UPDATE AVAILABLE**: ReSkate **{latest}** is out, this server is pinned to **{running}**. "
                    f"Players on the new version can't join until the server is updated.\n{how}\n"
                    f"https://github.com/{REPO}/releases/tag/v{latest}")
        if self.essentials.send(text, ping=True):
            write_json(PINNED_STATE, {"version": latest, "published": published})

    # ---- mods ---------------------------------------------------------------------
    def post_mod_events(self):
        """Announce the installs/updates mods.py did during this start, then forget them."""
        import mods
        try:
            with open(mods.EVENTS, encoding="utf-8") as f:
                events = [json.loads(line) for line in f if line.strip()]
            os.remove(mods.EVENTS)
        except (OSError, ValueError):
            return
        for e in events:
            label = f"{e['owner']}-{e['name']}"
            maps = f" Maps: {', '.join(e['maps'])}." if e.get("maps") else ""
            if e.get("from"):
                self.essentials.send(f"**MOD UPDATED**: {label} {e['from']} to **{e['to']}**.{maps}")
            else:
                self.essentials.send(f"**MOD INSTALLED**: {label} **{e['to']}**.{maps}")

    def check_mods(self):
        """Announce (once per version) newer Thunderstore versions of the unpinned MODS entries."""
        import mods
        update = truthy(os.environ.get("MODS_UPDATE"), True)
        seen = read_json(MODS_STATE)
        for owner, pkg, latest, have in mods.pending_updates(os.environ.get("MODS", "")):
            key = f"{owner}-{pkg}"
            if seen.get(key) == latest:
                continue
            how = ("Restart the server to install it. The restart kicks everyone and changes the join code."
                   if update else "`MODS_UPDATE` is false, so it will not be installed automatically.")
            if self.essentials.send(f"**MOD UPDATE AVAILABLE**: {key} **{latest}** is out, this server has **{have}**. "
                                    f"{how}\nhttps://thunderstore.io/c/reskate/p/{owner}/{pkg}/", ping=True):
                seen[key] = latest
                write_json(MODS_STATE, seen)

    def mods_loop(self):
        time.sleep(5)
        try:
            self.post_mod_events()
        except Exception as e:
            note(f"mod events: {e!r}")
        time.sleep(85)
        while True:
            try:
                self.check_mods()
            except Exception as e:
                note(f"mod update check: {e!r}")
            time.sleep(MODS_CHECK_EVERY)

    def start(self):
        def guarded(fn):
            def run():
                try:
                    fn()
                except Exception as e:
                    note(f"{fn.__name__}: {e!r}")
            threading.Thread(target=run, daemon=True).start()
        guarded(self.console_loop)
        if self.essentials.url and os.environ.get("MODS", "").strip():
            guarded(self.mods_loop)
        if self.mode == "auto" or (self.essentials.url and re.fullmatch(r"\d+(\.\d+)+", self.image_version)):
            guarded(self.update_loop)


def read_new(state):
    try:
        st = os.stat(LOG)
    except FileNotFoundError:
        return []
    if state["ino"] != st.st_ino or st.st_size < state["pos"]:
        state["ino"], state["pos"] = st.st_ino, 0      # log was replaced or truncated
    if st.st_size == state["pos"]:
        return []
    with open(LOG, "rb") as f:
        f.seek(state["pos"])
        data = f.read(st.st_size - state["pos"])
    end = data.rfind(b"\n")
    if end < 0:
        return []                                       # wait for the rest of the line
    state["pos"] += end + 1
    return [clean(line) for line in data[:end].decode("utf-8", "replace").splitlines() if line.strip()]
