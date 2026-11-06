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

    def test_kick_times(self):
        commands, _ = settings.live_commands({}, {"KICK_MINUTES": "30", "VOTE_KICK_MINUTES": "0"})
        self.assertEqual(commands, ["kick-time 30", "votes kick-minutes restart"])

    def test_restart_digest_ignores_live_keys(self):
        a = settings.restart_digest({"MAX_PLAYERS": "10", "WELCOME_MESSAGE": "hi"})
        self.assertEqual(a, settings.restart_digest({"MAX_PLAYERS": "10", "WELCOME_MESSAGE": "other"}))
        self.assertNotEqual(a, settings.restart_digest({"MAX_PLAYERS": "11", "WELCOME_MESSAGE": "hi"}))

    def test_new_mods_go_first_and_only_the_added_ones(self):
        old = {"MODS": "A-Map, B-Map", "MAP_POOL": "X"}
        new = {"MODS": "A-Map,B-Map,C-Map, D-Map", "MAP_POOL": "X,C"}
        commands, restart = settings.live_commands(old, new)
        self.assertEqual(commands, ["mods add C-Map D-Map", "map-pool clear", "map-pool add X", "map-pool add C"])
        self.assertEqual(restart, [])

    def test_removed_mods_send_nothing(self):
        commands, restart = settings.live_commands({"MODS": "A-Map,B-Map"}, {"MODS": "A-Map"})
        self.assertEqual((commands, restart), ([], []))
        self.assertEqual(settings.live_commands({"MODS": "A-Map"}, {})[1], ["MODS"])

    def test_long_mods_lists_are_split(self):
        names = [f"Owner-Package{i:03d}" for i in range(40)]
        lines = settings.live_commands({}, {"MODS": ",".join(names)})[0]
        self.assertGreater(len(lines), 1)
        self.assertTrue(all(len(x) <= settings.MODS_LINE and x.startswith("mods add ") for x in lines))
        self.assertEqual(" ".join(x[len("mods add "):] for x in lines).split(), names)

    def test_restart_digests_accept_older_images(self):
        values = {"MAX_PLAYERS": "10", "MODS": "A-Map"}
        older = settings.digest({"MAX_PLAYERS": "10", "MODS": "A-Map"})          # MODS was not LIVE before
        self.assertIn(older, settings.restart_digests(values))
        self.assertIn(settings.restart_digest(values), settings.restart_digests(values))
        self.assertEqual(settings.restart_digest(values), settings.restart_digest({"MAX_PLAYERS": "10", "MODS": "B"}))


if __name__ == "__main__":
    unittest.main()
