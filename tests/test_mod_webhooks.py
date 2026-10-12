"""modules/mod_webhooks.py: handed-over scopes are posted from the module, as the server would."""
import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [os.path.join(ROOT, "modules"), os.path.join(ROOT, "rollover"), os.path.join(ROOT, "app")]
import common  # noqa: E402
import mod_webhooks  # noqa: E402

ENV = {"WEBHOOK_URL_pub": "https://example.invalid/public", "WEBHOOK_SCOPE_pub": "public",
       "WEBHOOK_URL_adm": "https://example.invalid/admin", "WEBHOOK_SCOPE_adm": "admin,log",
       "DISCORD_MENTION_IDS": "42", "SERVER_NAME": "Yard 1 | discord.gg/x"}


class WebhooksModuleTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.modules = os.path.join(self.dir.name, "modules.json")
        with open(self.modules, "w") as f:
            json.dump({"webhooks": {"public": "external", "admin": "external"}}, f)
        self.posted = []
        self.patches = [
            mock.patch.object(common.handover, "FILE", self.modules),
            mock.patch.object(mod_webhooks.bridgeclient, "servers", return_value=[{"server": "1", "running": True}]),
            mock.patch.object(common, "server_settings", return_value={"A": ENV}),
            mock.patch.object(mod_webhooks.notifier, "request",
                              side_effect=lambda url, payload, *a, **k: self.posted.append((url, payload)) or {}),
        ]
        for p in self.patches:
            p.start()
        self.module = mod_webhooks.Module()
        with redirect_stdout(io.StringIO()):
            self.module.refresh()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.dir.cleanup()

    def test_only_handed_over_scopes(self):
        self.assertEqual(self.module.servers["1"].scopes, {"public", "admin"})

    def test_message_goes_to_its_scope_with_ping(self):
        self.module.servers["1"].event({"server": "1", "slot": "A", "kind": "message", "scopes": ["admin"],
                                        "text": "**SERVER STOPPED**", "ping": True})
        url, payload = self.posted[-1]
        self.assertEqual(url, "https://example.invalid/admin")
        self.assertEqual(payload["content"], "<@42> **SERVER STOPPED**")
        self.assertEqual(payload["username"], "Yard 1")

    def test_console_join_on_public_is_cleaned(self):
        server = self.module.servers["1"]
        server.event({"server": "1", "slot": "A", "kind": "console",
                      "text": "[20:00:00] Dave joined (76561198000000001), 3/100 players, loaded in 5 s"})
        for feed in server.feeds["A"]:
            feed.first = time.time() - 60          # due now
        server.flush()
        public = [p["content"] for u, p in self.posted if u.endswith("/public")]
        self.assertEqual(public, ["```\nDave joined, 3/100 players\n```"])
        self.assertNotIn("7656", public[0])

    def test_scope_not_handed_over_is_not_posted(self):
        self.module.servers["1"].event({"server": "1", "slot": "A", "kind": "message", "scopes": ["updates"],
                                        "text": "ReSkate 2.0.8 is out", "ping": False})
        self.assertEqual(self.posted, [])


if __name__ == "__main__":
    unittest.main()
