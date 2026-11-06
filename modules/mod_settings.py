"""The settings module (RESKATE_MODE=settings): settings changes reach running servers without a restart.

For every server whose /shared/modules.json says "settings": "external", it watches what the settings
files (/shared/settings, see app/settings.py) say for that server. When that changes, the settings the
server can take while it runs (app/settings.py LIVE: welcome, map pool, announcements, votes, checks ...)
are typed into its console through the bridge, and the server saves them to its own file. New MODS entries
go in first (`mods add`, which installs and links them and reloads the maps); the rest waits until the server
says "[mods] done" (at most SETTINGS_MODS_WAIT seconds, default 300), so a pool can name the new maps. What it
cannot take (MAX_PLAYERS, a package leaving MODS, ...) is left to the rollover, which restarts the server once
nobody is on it (it compares only those, the "restart-digest").

Also: when /shared/roles.json changes, every running server that shares it reads it again ("roles reload").

What was applied last is kept in /shared/settings/.applied/server-<n>.json, so a restart of the module
neither repeats nor misses a change; without that file (the first run) the current state is taken as
applied. Secrets go to the console like the rest but never to the log.

Needs BRIDGE_URL, BRIDGE_TOKEN and /shared (read-write)."""
import json
import os
import sys
import time

sys.path[:0] = [os.path.dirname(os.path.abspath(__file__)), os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib")]
import bridgeclient  # noqa: E402
import common  # noqa: E402
import settings  # noqa: E402

BY = "module:settings"
CHECK = 20
ROLES = os.environ.get("ROLES_FILE") or "/shared/roles.json"
MODS_WAIT = float(os.environ.get("SETTINGS_MODS_WAIT") or 300)


def applied_path(number):
    return os.path.join(settings.DIR, ".applied", f"server-{number}.json")


def load_applied(number):
    try:
        with open(applied_path(number), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_applied(number, values):
    path = applied_path(number)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(values, f, sort_keys=True)
    os.chmod(path + ".tmp", 0o600)               # it holds the secrets' values too
    os.replace(path + ".tmp", path)


def wait_mods(number, after, count, clock=time.time):
    """True once the server said "[mods] done" `count` times after event `after`; False after MODS_WAIT."""
    end, done = clock() + MODS_WAIT, 0
    while clock() < end:
        wait = max(1, min(20, int(end - clock())))
        got = bridgeclient.call("GET", f"/v1/events?after={after}&wait={wait}&server={number}", timeout=wait + 20)
        for e in got["events"]:
            after = max(after, e["id"])
            done += "[mods] done" in e.get("text", "")
        if done >= count:
            return True
        if got["last"] < after:              # the bridge started again
            return False
    return False


class Module:
    def __init__(self):
        self.roles_mtime = self.mtime(ROLES)

    @staticmethod
    def mtime(path):
        try:
            return os.stat(path).st_mtime
        except OSError:
            return None

    def check_server(self, number):
        slots = common.server_settings(number, max_age=0)
        if not slots:
            return
        base = next(iter(slots.values()))                      # the container env the += build on
        now = settings.load(base, report=False)[0]
        before = load_applied(number)
        if before is None:
            save_applied(number, now)
            common.say(f"server {number}: settings taken as they are now")
            return
        if before == now:
            return
        commands, restart = settings.live_commands(before, now)
        changed = sorted(k for k in set(before) | set(now) if before.get(k) != now.get(k))
        mods = [line for line in commands if line.startswith("mods add ")]
        rest = [line for line in commands if line not in mods]
        sent = 0
        if mods:
            after = bridgeclient.call("GET", "/v1/events?after=999999999&wait=0")["last"]
            for slot in slots:
                for line in mods:
                    sent += bridgeclient.console(number, line, BY, slot if len(slot) == 1 else None)
            if not wait_mods(number, after, len(mods) * len(slots)):
                common.say(f"server {number}: no \"[mods] done\" after {MODS_WAIT:.0f} s, sending the rest anyway")
        for slot in slots:
            for line in rest:
                sent += bridgeclient.console(number, line, BY, slot if len(slot) == 1 else None)
        save_applied(number, now)
        common.say(f"server {number}: {', '.join(changed)} changed; {len(commands)} console command(s) sent"
                   + (f" ({sent} delivered)" if commands else "")
                   + (f"; {', '.join(restart)} need a restart, the rollover does it once nobody is on" if restart else ""))

    def check_roles(self, rows):
        now = self.mtime(ROLES)
        if now == self.roles_mtime:
            return
        self.roles_mtime = now
        for row in rows:
            if not row["running"]:
                continue
            for slot, env in common.server_settings(row["server"]).items():
                if env.get("ROLES_SCOPE", "shared").strip().lower() != "server":
                    bridgeclient.console(row["server"], "roles reload", BY, slot if len(slot) == 1 else None)
        common.say("roles.json changed: roles reload sent to the servers sharing it")

    def loop(self):
        while True:
            try:
                rows = bridgeclient.servers()
                for row in rows:
                    if row["running"] and common.handed_over(row["server"], "settings"):
                        try:
                            self.check_server(row["server"])
                        except (bridgeclient.BridgeError, OSError) as e:
                            common.say(f"server {row['server']}: {e}")
                self.check_roles(rows)
            except Exception as e:  # never stop
                common.say(f"settings: {e!r}")
            time.sleep(CHECK)


def main():
    if not bridgeclient.TOKEN:
        sys.exit("settings: BRIDGE_TOKEN is not set")
    Module().loop()


if __name__ == "__main__":
    main()
