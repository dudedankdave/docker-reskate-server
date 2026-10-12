"""modules/mod_leaderboard.py: a server that hands the leaderboard over is counted by the module,
which answers through the bridge; one that does not is left alone."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [os.path.join(ROOT, "modules"), os.path.join(ROOT, "rollover"), os.path.join(ROOT, "app")]
import common  # noqa: E402
import mod_leaderboard  # noqa: E402


class LeaderboardModuleTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.dir.name, "leaderboard-unranked.db")
        self.modules = os.path.join(self.dir.name, "modules.json")
        with open(self.modules, "w") as f:
            json.dump({"leaderboard": "external", "servers": {"2": {"leaderboard": "internal"}}}, f)
        env = {"LEADERBOARD": "true", "LEADERBOARD_LEAGUE": "unranked", "LEADERBOARD_FILE": self.db,
               "SERVER_NAME": "Yard 1", "SCORE_CHECK": "warn"}
        self.sent = []
        self.patches = [
            mock.patch.object(common.handover, "FILE", self.modules),
            mock.patch.object(mod_leaderboard.bridgeclient, "servers",
                              return_value=[{"server": "1", "running": True}, {"server": "2", "running": True}]),
            mock.patch.object(common, "server_settings", return_value={"A": env}),
            mock.patch.object(mod_leaderboard.bridgeclient, "console",
                              side_effect=lambda n, line, by, slot=None: self.sent.append((n, slot, by, line)) or True),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.dir.cleanup()

    def test_only_handed_over_servers(self):
        m = mod_leaderboard.Module()
        with redirect_stdout(io.StringIO()):
            m.refresh()
        self.assertEqual(list(m.boards), [("1", "A")])

    def test_counts_a_throwdown_and_answers_through_the_bridge(self):
        m = mod_leaderboard.Module()
        with redirect_stdout(io.StringIO()):
            m.refresh()
        board = m.boards[("1", "A")]
        for line in ("[20:00:00] Anna joined (76561198000000001), 1/100 players, loaded in 5 s",
                     "[20:00:01] Ben joined (76561198000000002), 2/100 players, loaded in 5 s",
                     "[20:10:00] [throwdown] Anna's Jam has finished: 1. Anna 12,345, 2. Ben 9,876"):
            board.feed(line)
        rows = board.store.run(lambda cur: mod_leaderboard.leaderboard.Store.top(cur, "general", 10))
        points = {r["name"]: r["points"] for r in rows}
        self.assertEqual(points, {"Anna": 10, "Ben": 6})
        board.send("say hello")
        self.assertEqual(self.sent[-1], ("1", "A", "module:leaderboard", "say hello"))


if __name__ == "__main__":
    unittest.main()
