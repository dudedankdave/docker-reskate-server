"""The webhooks module (RESKATE_MODE=webhooks): the server's Discord webhooks, posted from outside, for
every server and scope that /shared/modules.json hands over ("webhooks": "external", or per scope).

The same variables as inside the server (WEBHOOK_URL_<n> / WEBHOOK_SCOPE_<n>, DISCORD_MENTION_IDS,
DISCORD_USERNAME, the older DISCORD_WEBHOOK_* names), read from the server's settings files (the URLs
are secrets, so they have to be in /shared/settings, see split-settings), and the same feeds: console
lines through the notifier's filters (admin, log, public, chat), the image's own messages ([event]
lines: server up, crashes, updates, mods) to their scope. Not here: the titled answer block of an info
command typed into the console (the log scope still gets its lines).

Needs BRIDGE_URL, BRIDGE_TOKEN and /shared (read-only)."""
import os
import sys
import threading
import time

sys.path[:0] = [os.path.dirname(os.path.abspath(__file__)), os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib")]
import bridgeclient  # noqa: E402
import common  # noqa: E402
import notifier  # noqa: E402

REFRESH = 60
FEEDS = (("admin", "essentials", notifier.essential_line), ("log", "log", notifier.log_line),
         ("public", "public", notifier.public_line), ("chat", "chat", notifier.chat_line))


class Server:
    """One server's webhooks for the scopes it hands over: hooks for messages, feeds per slot."""

    def __init__(self, number, env, scopes):
        self.number, self.scopes = number, scopes
        hooks = notifier.webhooks(env)
        name = notifier.username(env)
        mentions = [i.strip() for i in env.get("DISCORD_MENTION_IDS", "").split(",") if i.strip()]
        # scope=None: these post whatever modules.json says (it is what sent them here)
        self.hooks = {
            "admin": notifier.Hook(hooks["essentials"], name, mentions),
            "log": notifier.Hook(hooks["log"], name),
            "public": notifier.Hook(hooks["public"], name),
            "chat": notifier.Hook(hooks["chat"], name),
            "updates": notifier.Hook(hooks["updates"], name),
            "updates-admin": notifier.Hook(hooks["updates-admin"] or hooks["essentials"], name, mentions),
        }
        self.key = (tuple(sorted(scopes)), tuple((k, tuple(h.urls)) for k, h in sorted(self.hooks.items())), name,
                    tuple(mentions))
        self.feeds = {}          # slot -> [Feed]
        self.lock = threading.Lock()

    def slot_feeds(self, slot):
        if slot not in self.feeds:
            self.feeds[slot] = [notifier.Feed(self.hooks[scope], convert, scope == "log")
                                for scope, _, convert in FEEDS if scope in self.scopes and self.hooks[scope].url]
        return self.feeds[slot]

    def event(self, e):
        with self.lock:
            self._event(e)

    def _event(self, e):
        if e.get("kind") == "message":
            scope = (e.get("scopes") or [""])[0]
            hook = self.hooks.get(scope)
            if scope in self.scopes and hook and hook.url:
                hook.send(e["text"], ping=bool(e.get("ping")), event=False)
        elif e.get("kind", "console") == "console":
            for feed in self.slot_feeds(e.get("slot")):
                feed.add([e["text"]])

    def flush(self):
        with self.lock:
            feeds_now = list(self.feeds.values())
        for feeds in feeds_now:
            for feed in feeds:
                feed.flush_if_due()


class Module:
    def __init__(self):
        self.servers = {}
        self.lock = threading.Lock()

    def refresh(self):
        want = {}
        for row in bridgeclient.servers():
            number = row["server"]
            scopes = {s for s in self.scopes_of(number)}
            if not row["running"] or not scopes:
                continue
            envs = common.server_settings(number)
            env = next(iter(envs.values()), None)
            if env is None:
                continue
            server = Server(number, env, scopes)
            if not any(server.hooks[s].url for s in scopes):
                continue
            want[number] = server
        with self.lock:
            for number in list(self.servers):
                if number not in want:
                    common.say(f"server {number}: webhooks back inside the server")
                    del self.servers[number]
            for number, server in want.items():
                old = self.servers.get(number)
                if old is None or old.key != server.key:
                    common.say(f"server {number}: posts {', '.join(sorted(server.scopes))} from here")
                    self.servers[number] = server

    @staticmethod
    def scopes_of(number):
        return [s for s in ("admin", "log", "public", "chat", "updates", "updates-admin")
                if common.handed_over(number, "webhooks", s)]

    def follow(self):
        for e in bridgeclient.events():
            with self.lock:
                server = self.servers.get(e["server"])
            if server:
                try:
                    server.event(e)
                except Exception as exc:
                    common.say(f"server {e['server']}: {exc!r}")

    def loop(self):
        last = 0.0
        while True:
            if time.time() - last >= REFRESH:
                last = time.time()
                try:
                    self.refresh()
                except Exception as exc:
                    common.say(f"webhooks: {exc!r}")
            with self.lock:
                servers = list(self.servers.values())
            for server in servers:
                server.flush()
            time.sleep(1)


def main():
    if not bridgeclient.TOKEN:
        sys.exit("webhooks: BRIDGE_TOKEN is not set")
    module = Module()
    threading.Thread(target=module.follow, daemon=True).start()
    module.loop()


if __name__ == "__main__":
    main()
