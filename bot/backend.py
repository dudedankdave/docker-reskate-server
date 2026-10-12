"""How the bot reaches the servers: through the bridge (BRIDGE_URL and BRIDGE_TOKEN set, no Docker socket
needed), or, as before, straight through the Docker socket. Everything else in the bot only uses this.

    backend.servers()          {number: {"role": ...}}: the servers in servers.json
    backend.message()          the status cards (as status.py builds them)
    backend.targets(number)    the running containers of a server, each a Target: label, slot, players,
                               max, map, version, send(line, who), answer(since)
    backend.lines()            new console lines and image messages: [(number, slot, kind, scopes, text, ping)]
"""
import json
import os
import time

import rollover
import status

try:
    import bridgeclient
except ImportError:          # the old bot image without modules/
    bridgeclient = None


class Target:
    def __init__(self, label, slot, players, max_players, map_name, version, send, answer):
        self.label, self.slot, self.players, self.max, self.map, self.version = label, slot, players, max_players, map_name, version
        self.send, self.answer = send, answer


# ---- through the bridge -----------------------------------------------------------------------------
class Bridge:
    kind = "bridge"

    def __init__(self):
        self.cache, self.after = (0, None), None

    def rows(self, max_age=10):
        at, rows = self.cache
        if rows is None or time.time() - at > max_age:
            rows = bridgeclient.servers()
            self.cache = (time.time(), rows)
        return rows

    def servers(self):
        return {r["server"]: {"role": r.get("role", "")} for r in self.rows(max_age=300)}

    def message(self):
        return bridgeclient.call("GET", "/v1/status")

    def last_id(self):
        return bridgeclient.call("GET", "/v1/events?after=999999999&wait=0")["last"]

    def targets(self, number):
        row = next((r for r in self.rows(max_age=0) if r["server"] == number), None)
        out = []
        for slot, s in sorted(((row or {}).get("slots") or {}).items()):
            if not s.get("running"):
                continue

            def send(line, who, slot=slot):
                bridgeclient.do(number, "console", {"command": line}, who, slot)

            def answer(since, slot=slot):
                got = bridgeclient.call("GET", f"/v1/events?after={since}&wait=0&server={number}")["events"]
                return [rollover.LINE.sub(r"\1", e["text"]) for e in got
                        if e.get("kind", "console") == "console" and e.get("slot") in (slot, None)]
            out.append(Target(f"{s.get('version') or slot.upper()} (slot {slot.upper()})", slot, s.get("players"),
                              s.get("max"), s.get("map"), s.get("version"), send, answer))
        return out

    def marker(self):
        return self.last_id()

    def lines(self):
        if self.after is None:
            self.after = self.last_id()                          # from now on, nothing replayed
            return []
        got = bridgeclient.call("GET", f"/v1/events?after={self.after}&wait=0")
        if got["last"] < self.after:                             # the bridge started again
            self.after = got["last"]
            return []
        out = []
        for e in got["events"]:
            self.after = max(self.after, e["id"])
            out.append((e["server"], e.get("slot"), e.get("kind", "console"), e.get("scopes") or [], e["text"],
                        bool(e.get("ping"))))
        return out


# ---- through the Docker socket (as before) ---------------------------------------------------------
class Docker:
    kind = "docker"

    def __init__(self):
        import console
        self.console = console
        with open(rollover.CONFIG, encoding="utf-8") as f:
            self.board = status.Board(json.load(f))
        self.last = {}           # container id -> (started, (sec, nsec)) of the last line seen

    def servers(self):
        return self.board.cfg["servers"]

    def message(self):
        return self.board.message()

    def targets(self, number):
        found = self.board.gather()
        out = []
        for info in (i for i in found.get(number, []) if i["State"].get("Running")):
            s = self.board.follow(info)
            version = status.version_of(info)

            def send(line, who, info=info):
                self.console.send(info, line)

            def answer(since, info=info):
                import coach
                return coach.answer_since(info, since)
            out.append(Target(version or info["Name"].lstrip("/"), None, s.players, s.max, s.map, version, send, answer))
        return out

    def marker(self):
        return time.time()

    def lines(self):
        from datetime import datetime, timezone
        out = []
        for number, infos in self.board.gather().items():
            for info in infos:
                if not info["State"].get("Running"):
                    continue
                key, started = info["Id"], info["State"].get("StartedAt", "")
                seen = self.last.get(key)
                first = seen is None or seen[0] != started
                since = None if first else f"{seen[1][0]}.{seen[1][1]:09d}"
                pos = (0, 0) if first else seen[1]
                for line in rollover.logs(info, since=since, timestamps=True):
                    m = status.STAMP.match(line)
                    if not m:
                        continue
                    at = (int(datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()),
                          int((m.group(2) or "0").ljust(9, "0")[:9]))
                    if at <= pos:
                        continue
                    pos = max(pos, at)
                    if not first:           # a freshly seen container: remember where it is, replay nothing
                        out.append((number, None, "console", [], m.group(3), False))
                self.last[key] = (started, pos if pos != (0, 0) else (int(time.time()), 0))
        return out


def create():
    if bridgeclient is not None and os.environ.get("BRIDGE_TOKEN", "").strip():
        rollover.say(f"servers through the bridge at {bridgeclient.URL}")
        return Bridge()
    return Docker()
