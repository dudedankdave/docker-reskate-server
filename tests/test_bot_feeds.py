"""bot/feeds.py: which channel a console line goes to, and how the public channel shows it."""
import os
import sys
import types
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [os.path.join(ROOT, "bot"), os.path.join(ROOT, "rollover"), os.path.join(ROOT, "app")]
sys.modules.setdefault("discord", types.ModuleType("discord"))   # the feeds only need it to post
import feeds  # noqa: E402

LINES = {
    "[20:01:02] Dave joined (76561198065850675, admin), 3/100 players, loaded in 12 s":
        ({"admin", "log", "public"}, "Dave joined, 3/100 players"),
    "[20:05:00] Bob left (timeout) [rtt 80 ms]": ({"admin", "log", "public"}, "Bob left"),
    "[20:06:00] COCOJAMBOS YARD 1 is up on Pattonville for 100 players.":
        ({"log", "public"}, "Up on Pattonville for 100 players."),
    "[20:07:00] [throwdown] Jam has finished: Dave wins": ({"log", "public"}, "[throwdown] Jam has finished: Dave wins"),
    "[20:08:00] Everyone has loaded Southbank": ({"log", "public"}, "Everyone has loaded Southbank"),
    "[20:09:00] [chat] Dave: hi": ({"chat"}, None),
    "[20:10:00] [kick] Bob kicked by Dave": ({"admin", "log"}, None),
}


class FeedsTest(unittest.TestCase):
    def test_scopes(self):
        for line, (scopes, _) in LINES.items():
            with self.subTest(line):
                self.assertEqual(feeds.classify(line), scopes)

    def test_public_has_no_steam_ids(self):
        for line, (scopes, public) in LINES.items():
            if "public" in scopes:
                with self.subTest(line):
                    self.assertEqual(feeds.show(line, "public"), public)
                    self.assertNotIn("7656", feeds.show(line, "public"))

    def test_admin_keeps_the_full_line(self):
        line = "[20:01:02] Dave joined (76561198065850675, admin), 3/100 players, loaded in 12 s"
        self.assertIn("76561198065850675", feeds.show(line, "admin"))
        self.assertEqual(feeds.show("[20:09:00] [chat] Dave: hi", "chat"), "Dave: hi")


if __name__ == "__main__":
    unittest.main()
