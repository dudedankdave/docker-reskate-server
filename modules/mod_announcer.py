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

Needs BRIDGE_URL and BRIDGE_TOKEN, and /shared (read-only) for modules.json and announcements.json."""
import json
import os
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
    for e in bridgeclient.events():
        if e.get("kind", "console") != "console":
            continue
        text = announcement(e["text"])
        if text:
            try:
                if wanted(e["server"], e.get("slot")):
                    bridgeclient.console(e["server"], f"say {text}", BY, e.get("slot"))
            except bridgeclient.BridgeError as exc:
                common.say(f"server {e['server']}: {exc}")


if __name__ == "__main__":
    main()
