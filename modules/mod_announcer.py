"""The announcer module (RESKATE_MODE=announcer), two jobs:

1. Kick announcer: app/announcer.py from outside the server, for every server whose /shared/modules.json says
   "announcer": "external" and that has ANNOUNCE_KICKS on (default). An admin kick or ban in a server's console
   becomes "Name was kicked." / "Name was banned." in its chat.
2. Scheduled announcements, each with its own interval (the server's own ANNOUNCEMENTS has one interval for all
   lines), from /shared/announcements.json, re-read every tick:

       {"default": [{"text": "Commands: /top /rank /boards", "every": 10},
                    {"text": "Join our Discord: ...", "every": 20, "offset": 5}],
        "servers": {"1": {"extra": [{"text": "Map pool: /vote map <map>", "every": 10}]},
                    "3": [],
                    "6": [{"text": "RANKED rules ...", "every": 5}]}}

   `every` minutes (whole), `offset` minutes into that period (default 0), `say: true` types the line into the
   chat instead of the announcement card. A server's list replaces "default"; {"extra": [...]} adds to it; []
   means none. A line is due when the clock (UTC) passes a multiple of `every` plus `offset`, so a restart of
   the module neither repeats nor skips one. Only slots with players on get it. A server whose own
   ANNOUNCEMENT_INTERVAL_MINUTES and ANNOUNCEMENTS are set is skipped (it would say everything twice): clear
   them in its settings first, the settings module types that into the running server.

3. /coach: the server logs "[command] Name /coach" for an allowed custom command (a `coach` entry in COMMANDS
   whose reply is only a rule line). For every server with "coach" lines in /shared/announcements.json this
   module answers with one private message per line, each starting "Coach Frank <random verb>:" (tells you,
   shouts at you, teaches you ...), the text behind `pad` spaces (the chat wraps there; leading spaces are trimmed,
   so the prefix goes first) and a closing rule:

       {"coach": {"verbs": ["tells you", "shouts at you"], "pad": 100,
                  "lines": [{"text": "/top shows the best players"}, {"text": "/rank your rank"}],
                  "servers": {"1": {"extra": [{"text": "Map vote: /vote map <map>"}]}, "6": [...]}}}

   `lines` is the default for every server, a server's list replaces it, {"extra": [...]} adds to it (as above).
   A line is at most 200 bytes in all; longer text is cut.

Needs BRIDGE_URL and BRIDGE_TOKEN, and /shared (read-only) for modules.json and announcements.json."""
import json
import os
import random
import re
import sys
import threading
import time

sys.path[:0] = [os.path.dirname(os.path.abspath(__file__)), os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib")]
import announcer  # noqa: E402
import bridgeclient  # noqa: E402
import common  # noqa: E402

BY = "module:announcer"
SCHEDULE = os.environ.get("ANNOUNCEMENTS_FILE") or "/shared/announcements.json"
TICK = 15
JOINED = re.compile(r"^(.+) joined \((\d+)(?:, admin)?\), \d+/\d+ players")
LEFT = re.compile(r"^(.+?) left \(")
UP = re.compile(r"^.+ is up on .+ for \d+ players\.$")
COMMAND = re.compile(r"^\[command\] (.+) /coach(?: .*)?$")
VERBS = ["tells you", "shouts at you", "teaches you", "yells at you", "whispers to you", "reminds you",
         "warns you", "explains to you", "barks at you", "lectures you"]
RULE = "-" * 30
CHAT_MAX = 200          # bytes in one chat line
ASK_EVERY = 5           # seconds between two /coach answers for one player
LINE = re.compile(r"^\[\d\d:\d\d:\d\d\] (.*)$")


def announcement(line):
    """The chat line for one console line as the bridge has it ("[HH:MM:SS] text"), or None."""
    m = LINE.match(line.rstrip("\r\n"))
    if not m:
        return None
    for pattern, what in ((announcer.KICKED, "kicked"), (announcer.BANNED, "banned")):
        k = pattern.match(m.group(1))
        if k:
            return f"{k.group(1).strip()} was {what}."
    return None


def wanted(number, slot, cache={}):
    """Handed over and ANNOUNCE_KICKS on; looked up at most once a minute per server."""
    at, value = cache.get(number, (0, None))
    if value is None or time.time() - at > 60:
        value = common.handed_over(number, "announcer") and {
            s for s, env in common.server_settings(number).items() if common.truthy(env.get("ANNOUNCE_KICKS"), True)}
        cache[number] = (time.time(), value)
    return bool(value) and (slot in value or slot is None)


def pick(raw, number):
    """Lines of `raw` (a section with "lines" and "servers", as the scheduled announcements have) for one server."""
    own = (raw.get("servers") or {}).get(str(number))
    default = raw.get("lines") or []
    if isinstance(own, list):
        lines = own
    elif isinstance(own, dict):
        lines = list(default) + list(own.get("extra") or [])
    else:
        lines = default
    return [str(x.get("text") if isinstance(x, dict) else x).strip() for x in lines
            if (x.get("text") if isinstance(x, dict) else x)]


def coach_messages(raw, number, steam_id, rng=random):
    """The `msg` console lines /coach answers with, or [] when this server has no coach lines."""
    texts = pick(raw, number)
    if not texts:
        return []
    verbs = [v for v in (raw.get("verbs") or VERBS) if isinstance(v, str) and v.strip()] or VERBS
    try:
        pad = max(0, min(150, int(raw.get("pad", 100))))
    except (TypeError, ValueError):
        pad = 100
    out = []
    for text in texts:
        line = f"Coach Frank {rng.choice(verbs)}:" + " " * pad + text
        out.append(f"msg {steam_id} " + line.encode("utf-8")[:CHAT_MAX].decode("utf-8", "ignore"))
    out.append(f"msg {steam_id} {RULE}")
    return out


class Coach:
    """Follows who is on each server and answers /coach."""

    def __init__(self):
        self.online = {}      # (server, slot) -> {name: steam id}
        self.asked = {}       # (server, steam id) -> time

    def event(self, e, raw, now=None):
        """Handle one bridge event; returns [(server, slot, console line)] to type."""
        m = LINE.match(str(e.get("text", "")).rstrip("\r\n"))
        if not m:
            return []
        body, key = m.group(1), (str(e["server"]), e.get("slot"))
        people = self.online.setdefault(key, {})
        if UP.match(body):
            people.clear()
        elif j := JOINED.match(body):
            people[j.group(1)] = j.group(2)
        elif lv := LEFT.match(body):
            people.pop(lv.group(1), None)
        elif c := COMMAND.match(body):
            who = c.group(1)
            if any(who.startswith(o + ": /") for o in people):      # someone's typed text, not our command
                return []
            who_id = people.get(who)
            if who_id is None:
                return []
            now = time.time() if now is None else now
            if now - self.asked.get((key[0], who_id), 0) < ASK_EVERY:
                return []
            lines = coach_messages(raw, key[0], who_id)
            if lines:
                self.asked[(key[0], who_id)] = now
            return [(key[0], key[1], line) for line in lines]
        return []


def load_schedule(path=None):
    """The schedule file's JSON, or {} when there is none or it cannot be read."""
    try:
        with open(path or SCHEDULE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def lines_for(data, number):
    """The scheduled lines of one server: [(text, every_seconds, offset_seconds, say)]."""
    own = (data.get("servers") or {}).get(str(number))
    default = data.get("default") or []
    if isinstance(own, list):
        raw = own
    elif isinstance(own, dict):
        raw = list(default) + list(own.get("extra") or [])
    else:
        raw = default
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        try:
            every = int(item.get("every") or 0)
            offset = int(item.get("offset") or 0)
        except (TypeError, ValueError):
            continue
        if text and every > 0:
            out.append((text, every * 60, (offset % every) * 60, bool(item.get("say"))))
    return out


def due(every, offset, last, now):
    """True when a multiple of `every` (+ offset) lies in (last, now]; times in seconds."""
    return (now - offset) // every > (last - offset) // every


def doubled(number):
    """True when the server's own announcements are on as well (they would say everything twice)."""
    for env in common.server_settings(number).values():
        interval = str(env.get("ANNOUNCEMENT_INTERVAL_MINUTES") or "0").strip()
        lines = str(env.get("ANNOUNCEMENTS") or "").strip().lower()
        if interval not in ("", "0") and lines not in ("", "off", "none"):
            return True
    return False


class Schedule:
    def __init__(self):
        self.last = None
        self.warned = set()

    def tick(self, now):
        last, self.last = self.last, now
        if last is None:                                  # first tick: nothing is "missed" from before
            return
        data = load_schedule()
        if not data:
            return
        for row in bridgeclient.servers():
            number = str(row["server"])
            lines = lines_for(data, number)
            if not lines:
                continue
            if doubled(number):
                if number not in self.warned:
                    self.warned.add(number)
                    common.say(f"server {number}: its own ANNOUNCEMENTS are on, scheduled lines skipped")
                continue
            self.warned.discard(number)
            slots = [s for s, v in (row.get("slots") or {}).items() if v.get("running") and (v.get("players") or 0) > 0]
            for text, every, offset, say in lines:
                if due(every, offset, last, now):
                    for slot in slots:
                        bridgeclient.console(number, ("say " if say else "announce ") + text, BY, slot)

    def run(self):
        while True:
            try:
                self.tick(time.time())
            except bridgeclient.BridgeError as exc:
                common.say(f"schedule: {exc}")
            time.sleep(TICK)


def main():
    if not bridgeclient.TOKEN:
        sys.exit("announcer: BRIDGE_TOKEN is not set")
    threading.Thread(target=Schedule().run, daemon=True).start()
    coach = Coach()
    for e in bridgeclient.events():
        if e.get("kind", "console") != "console":
            continue
        raw = (load_schedule().get("coach") or {}) if "[command]" in str(e.get("text", "")) else {}
        for number, slot, line in coach.event(e, raw):
            bridgeclient.console(number, line, BY, slot)
        text = announcement(e["text"])
        if text:
            try:
                if wanted(e["server"], e.get("slot")):
                    bridgeclient.console(e["server"], f"say {text}", BY, e.get("slot"))
            except bridgeclient.BridgeError as exc:
                common.say(f"server {e['server']}: {exc}")


if __name__ == "__main__":
    main()
