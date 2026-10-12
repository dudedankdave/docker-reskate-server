"""rollover/bridge.py: the image's [event] lines become events of their scope."""
import os
import sys
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [os.path.join(ROOT, "rollover"), os.path.join(ROOT, "app")]
import bridge  # noqa: E402
import notifier  # noqa: E402


class BridgeEventTest(unittest.TestCase):
    def setUp(self):
        bridge.events.clear()

    def test_image_message(self):
        line = '[event] updates-admin {"ping":true,"text":"**UPDATE AVAILABLE**: ReSkate **2.0.8** is out"}'
        self.assertEqual(bridge.message(line), ("updates-admin", "**UPDATE AVAILABLE**: ReSkate **2.0.8** is out", True))

    def test_console_line_is_no_message(self):
        self.assertIsNone(bridge.message("[20:01:02] Dave joined (7656, admin), 3/100 players, loaded in 12 s"))
        self.assertIsNone(bridge.message("[event] admin {broken"))

    def test_same_format_as_the_notifier_prints(self):
        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            notifier.emit("admin", "**SERVER STOPPED** (exit code 1)\nsecond line", ping=True)
        self.assertEqual(bridge.message(out.getvalue().strip()), ("admin", "**SERVER STOPPED** (exit code 1)\nsecond line", True))

    def test_push_keeps_kind_and_ping(self):
        bridge.push("1", "2026-10-12T20:00:00Z", "Server up", ["admin"], "message", True)
        bridge.push("1", "2026-10-12T20:00:01Z", "[20:00:01] [chat] Dave: hi")
        self.assertEqual([(e["scopes"], e["kind"], e["ping"]) for e in bridge.events],
                         [(["admin"], "message", True), (["chat"], "console", False)])


if __name__ == "__main__":
    unittest.main()
