"""app/settings.py LIVE: settings typed into a running server; slim knows the same keys."""
import os
import re
import sys
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "app"))
import settings  # noqa: E402


class LiveTest(unittest.TestCase):
    def test_slim_has_the_same_live_keys(self):
        with open(os.path.join(ROOT, "slim", "settings.go"), encoding="utf-8") as f:
            block = re.search(r"var liveKeys = map\[string\]bool\{(.*?)\n\}", f.read(), re.S).group(1)
        self.assertEqual(set(re.findall(r'"([A-Z0-9_]+)": true', block)), set(settings.LIVE))

    def test_commands(self):
        old = {"MAP_POOL": "A,B", "MAX_PLAYERS": "10", "WELCOME_MESSAGE": "hi", "SCORE_CHECK": "warn",
               "ANNOUNCEMENTS": "x|y", "VOTE_TIME_OF_DAY_ENABLED": "false", "OBJECT_LIMIT": "100"}
        new = {"MAP_POOL": "A,C", "MAX_PLAYERS": "20", "WELCOME_MESSAGE": "off", "SCORE_CHECK": "kick",
               "ANNOUNCEMENTS": "z", "VOTE_TIME_OF_DAY_ENABLED": "true", "OBJECT_LIMIT": "0",
               "DISTANCE_LOW_RATE_START": "170"}
        commands, restart = settings.live_commands(old, new)
        self.assertEqual(commands, ["announcements clear", "announcements add z", "map-pool clear", "map-pool add A",
                                    "map-pool add C", "objects off", "score-check kick", "votes tod on", "welcome off"])
        self.assertEqual(restart, ["DISTANCE_LOW_RATE_START", "MAX_PLAYERS"])

    def test_restart_digest_ignores_live_keys(self):
        a = settings.restart_digest({"MAX_PLAYERS": "10", "WELCOME_MESSAGE": "hi"})
        self.assertEqual(a, settings.restart_digest({"MAX_PLAYERS": "10", "WELCOME_MESSAGE": "other"}))
        self.assertNotEqual(a, settings.restart_digest({"MAX_PLAYERS": "11", "WELCOME_MESSAGE": "hi"}))


if __name__ == "__main__":
    unittest.main()
