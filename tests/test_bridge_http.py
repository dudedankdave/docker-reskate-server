"""The bridge's HTTP API end to end, with the bot's backend and the modules' client on the other side.
Docker is faked: two servers, server 1 running in slot a."""
import os
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [os.path.join(ROOT, "bot"), os.path.join(ROOT, "modules"), os.path.join(ROOT, "rollover"),
                os.path.join(ROOT, "app")]
import bridge  # noqa: E402
import bridgeclient  # noqa: E402

INFO = {"Id": "c1", "Name": "/reskate-server-1-2.0.7", "State": {"Running": True},
        "Config": {"Env": ["PORT=12400", "SERVER_PASSWORD=pw", "WELCOME_MESSAGE=hi", "LEADERBOARD=true"],
                   "Labels": {}, "Tty": True, "OpenStdin": True}}


class Follow:
    players, max, map = 3, 100, "Pattonville"


class FakeBoard:
    cfg = {"servers": {"1": {"role": "Main", "slots": {"a": {"port": 12400}, "b": {"port": 12402}}},
                       "2": {"role": "Skate 2", "slots": {"a": {"port": 12410}}}}}

    def gather(self):
        return {"1": [INFO], "2": []}

    def follow(self, info):
        return Follow()

    def message(self):
        return {"embeds": [{"title": "Main"}, {"title": "Total"}]}


class BridgeHTTPTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.typed = []
        bridge.board = FakeBoard()
        cls.patches = [mock.patch.object(bridge, "TOKEN", "t0k"), mock.patch.object(bridgeclient, "TOKEN", "t0k"),
                       mock.patch.object(bridge, "type_into", side_effect=lambda info, line: cls.typed.append(line)),
                       mock.patch.object(bridge, "settings", None),
                       mock.patch.object(bridge.status, "version_of", return_value="2.0.7")]
        for p in cls.patches:
            p.start()
        cls.http = ThreadingHTTPServer(("127.0.0.1", 0), bridge.Handler)
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()
        bridgeclient.URL = f"http://127.0.0.1:{cls.http.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        for p in cls.patches:
            p.stop()

    def test_servers_and_settings_without_secrets(self):
        rows = bridgeclient.servers()
        self.assertEqual([(r["server"], r["running"]) for r in rows], [("1", True), ("2", False)])
        self.assertEqual(rows[0]["slots"]["a"]["players"], 3)
        env = bridgeclient.settings("1")["a"]
        self.assertEqual(env["WELCOME_MESSAGE"], "hi")
        self.assertNotIn("SERVER_PASSWORD", env)

    def test_wrong_token(self):
        with mock.patch.object(bridgeclient, "TOKEN", "nope"), self.assertRaises(bridgeclient.BridgeError):
            bridgeclient.servers()

    def test_module_action_is_audit_only_and_slot_any_case(self):
        before = bridge.next_id
        self.assertTrue(bridgeclient.console("1", "say hi", "module:leaderboard", "A"))
        self.assertEqual(self.typed[-1], "say hi")
        audit = [e for e in bridge.events if e["id"] >= before]
        self.assertEqual(audit[-1]["scopes"], ["audit"])
        self.assertFalse(bridgeclient.console("1", "quit", "module:x"))     # refused: stops the server

    def test_bot_backend(self):
        import backend
        with mock.patch.dict(os.environ, {"BRIDGE_TOKEN": "t0k"}), mock.patch.object(backend.rollover, "say"):
            b = backend.create()
        self.assertEqual(b.kind, "bridge")
        self.assertEqual(b.servers()["1"], {"role": "Main"})
        self.assertEqual(b.message()["embeds"][0]["title"], "Main")
        self.assertEqual(b.lines(), [])                                     # first call: from now on
        mark = b.marker()
        bridge.push("1", "2026-10-12T20:00:00Z", "[20:00:00] Dave joined (7656, admin), 3/100 players, loaded in 5 s", slot="a")
        bridge.push("1", "2026-10-12T20:00:01Z", "**SERVER STOPPED**", ["admin"], "message", True, "a")
        lines = b.lines()
        self.assertEqual([(n, kind) for n, _s, kind, *_ in lines], [("1", "console"), ("1", "message")])
        (target,) = b.targets("1")
        self.assertEqual((target.slot, target.players, target.map), ("a", 3, "Pattonville"))
        target.send("announce hello", "Admin")
        self.assertEqual(self.typed[-1], "announce hello")
        self.assertIn("Dave joined (7656, admin), 3/100 players, loaded in 5 s", target.answer(mark))
        self.assertEqual(b.targets("2"), [])


if __name__ == "__main__":
    unittest.main()
