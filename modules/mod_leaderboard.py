"""The leaderboard module (RESKATE_MODE=leaderboard): the same leaderboard as inside the server
(app/leaderboard.py), for every server whose /shared/modules.json says "leaderboard": "external".

It follows each server's console through the bridge, keeps one leaderboard per running container
(server and slot), answers in game through the bridge (msg, say, announce) and posts the boards to the
server's leaderboard webhooks (else its public ones) from the settings files. The databases are the
servers' own, in /shared; a server with LEADERBOARD_SCOPE=server keeps its board inside (it is in its /data).

Hand a server over while it is empty: players already on are counted from their next join.
Needs BRIDGE_URL, BRIDGE_TOKEN and /shared (read-write)."""
import os
import sys
import threading
import time

sys.path[:0] = [os.path.dirname(os.path.abspath(__file__)), os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib")]
import bridgeclient  # noqa: E402
import common  # noqa: E402
import leaderboard  # noqa: E402
import notifier  # noqa: E402

BY = "module:leaderboard"
REFRESH = 60


def webhook_post(env):
    """post(text) to the server's leaderboard webhooks (else public), or None without any."""
    hooks = notifier.webhooks(env)
    urls = hooks["leaderboard"] or hooks["public"]
    if not urls:
        return None
    hook = notifier.Hook(urls, notifier.username(env))

    def post(text):
        hook.send(text, event=False)
    return post


class Module:
    def __init__(self):
        self.boards = {}         # (server, slot) -> Leaderboard
        self.lock = threading.Lock()
        self.noted = set()

    def note(self, key, message):
        if key not in self.noted:
            self.noted.add(key)
            common.say(message)

    def refresh(self):
        """Start a board for each running container whose server hands the leaderboard over; drop the rest."""
        want = {}
        for row in bridgeclient.servers():
            number = row["server"]
            if not row["running"] or not common.handed_over(number, "leaderboard"):
                continue
            for slot, env in common.server_settings(number).items():
                if not common.truthy(env.get("LEADERBOARD")):
                    continue
                if env.get("LEADERBOARD_SCOPE", "shared").strip().lower() == "server":
                    self.note(("own", number), f"server {number}: LEADERBOARD_SCOPE=server, its board is in its /data; "
                                               "it stays inside the server")
                    continue
                want[(number, slot)] = env
        with self.lock:
            for key in list(self.boards):
                if key not in want:
                    common.say(f"server {key[0]} slot {key[1]}: leaderboard stopped here")
                    del self.boards[key]
            for key, env in want.items():
                if key in self.boards:
                    continue
                number, slot = key
                board = leaderboard.Leaderboard(lambda line, n=number, s=slot: bridgeclient.console(n, line, BY, s),
                                                webhook_post(env), env)
                if board.ok:
                    self.boards[key] = board
                    common.say(f"server {number} slot {slot}: leaderboard runs here ({board.league}, {board.label})")

    def follow(self):
        for e in bridgeclient.events():
            if e.get("kind", "console") != "console":
                continue
            with self.lock:
                board = self.boards.get((e["server"], e.get("slot")))
            if board:
                try:
                    board.feed(e["text"])
                except Exception as exc:  # one bad line never stops the module
                    common.say(f"server {e['server']}: {exc!r}")

    def tick(self):
        last = 0.0
        while True:
            if time.time() - last >= REFRESH:
                last = time.time()
                try:
                    self.refresh()
                except Exception as exc:
                    common.say(f"leaderboard: {exc!r}")
            with self.lock:
                boards = list(self.boards.values())
            for board in boards:
                try:
                    board.tick()
                except Exception as exc:
                    common.say(f"leaderboard: {exc!r}")
            time.sleep(1)


def main():
    if not bridgeclient.TOKEN:
        sys.exit("leaderboard: BRIDGE_TOKEN is not set")
    module = Module()
    threading.Thread(target=module.follow, daemon=True).start()
    module.tick()


if __name__ == "__main__":
    main()
