"""The leaderboard's feed for the COJA tools HUD (app/leaderboard.py, TOOLS_HUD): when it is on, what goes to the
console as `coco player` / `coco top` / `coco info`, and when."""
import base64
import json
import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [os.path.join(ROOT, "app")]
import leaderboard  # noqa: E402

A, B, C = "76561198000000001", "76561198000000002", "76561198000000003"


def decoded(line):
    """`coco <verb> [id] <base64>` -> (verb, id or None, the JSON)."""
    words = line.split(" ")
    value = json.loads(base64.b64decode(words[-1]).decode("utf-8"))
    return words[1], (words[2] if len(words) == 4 else None), value


class OnTest(unittest.TestCase):
    def test_on_with_the_plugin_required_or_by_hand(self):
        self.assertTrue(leaderboard.hud_on({"PLUGINS_REQUIRED": "SmokeyDawg58-ReSkate_Throwdown, COCOJAMBO-TOOLS"}))
        self.assertTrue(leaderboard.hud_on({"PLUGINS_REQUIRED": "cocojambo-tools-1.0.0"}))
        self.assertFalse(leaderboard.hud_on({"PLUGINS_REQUIRED": "COCOJAMBO-TOOLSX"}))
        self.assertFalse(leaderboard.hud_on({}))
        self.assertTrue(leaderboard.hud_on({"TOOLS_HUD": "true"}))
        self.assertFalse(leaderboard.hud_on({"TOOLS_HUD": "false", "PLUGINS_REQUIRED": "COCOJAMBO-TOOLS"}))

    def test_the_server_info(self):
        info = leaderboard.hud_info({"SERVER_NAME": "COCOJAMBOS YARD", "TOOLS_LINKS": "Discord=https://discord.gg/x | broken | Web=https://cocojambo.net",
                                     "TOOLS_RULES": "Be nice| |No hacks"})
        self.assertEqual(info, {"name": "COCOJAMBOS YARD", "links": [{"label": "Discord", "url": "https://discord.gg/x"},
                                                                     {"label": "Web", "url": "https://cocojambo.net"}],
                                "rules": ["Be nice", "No hacks"]})

    def test_a_line_is_base64_json(self):
        line = leaderboard.hud_line("player", A, {"points": 5, "team": "Café"})
        self.assertEqual(decoded(line), ("player", A, {"points": 5, "team": "Café"}))
        self.assertNotIn(" ", line.split(" ", 3)[3])


class FeedTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.sent = []
        self.env = {"LEADERBOARD": "true", "LEADERBOARD_FILE": os.path.join(self.dir.name, "lb.db"),
                    "PLUGINS_REQUIRED": "COCOJAMBO-TOOLS", "SERVER_NAME": "Yard", "TOOLS_LINKS": "Discord=d.gg/x"}

    def tearDown(self):
        self.dir.cleanup()

    def board(self, **env):
        b = leaderboard.Leaderboard(self.sent.append, None, {**self.env, **env})
        self.assertTrue(b.ok)
        return b

    def coco(self):
        return [decoded(line) for line in self.sent if line.startswith("coco ")]

    def later(self, b, seconds):
        b.hud_tick(time.time() + seconds)

    def test_off_without_the_plugin(self):
        b = self.board(PLUGINS_REQUIRED="")
        b.feed("[12:00:00] Anna joined (%s, player), 1/16 players" % A)
        b.tick()
        self.assertEqual(self.coco(), [])

    def test_up_then_joins_then_points(self):
        b = self.board()
        b.feed("[12:00:00] Yard is up on Skate3Improved for 16 players.")
        self.later(b, leaderboard.HUD_AFTER_UP + 1)
        sent = self.coco()
        self.assertEqual(sent[0], ("info", None, {"name": "Yard", "links": [{"label": "Discord", "url": "d.gg/x"}], "rules": []}))
        self.assertEqual(sent[1], ("top", None, {"board": "All Time (Ranked)", "rows": []}))
        self.sent.clear()

        b.feed("[12:00:01] Anna joined (%s, player), 1/16 players" % A)
        b.feed("[12:00:02] Ben joined (%s, admin), 2/16 players" % B)
        self.later(b, 1)
        self.assertEqual(self.coco(), [])                         # not yet: their game loads the plugin first
        self.later(b, leaderboard.HUD_AFTER_JOIN + 1)
        players = {who: v for verb, who, v in self.coco() if verb == "player"}
        self.assertEqual(set(players), {A, B})
        self.assertEqual(players[A]["points"], 0)
        self.assertEqual(players[A]["board"], "All Time (Ranked)")
        self.assertIn("no points yet", players[A]["lines"][0]["v"])
        self.sent.clear()

        b.waiting.clear()   # the ranked check is done
        b.feed("[12:05:00] [throwdown] Anna's Jam has finished: 1. Anna 5,000, 2. Ben 1,000")
        b.tick()
        sent = self.coco()
        players = {who: v for verb, who, v in sent if verb == "player"}
        self.assertEqual((players[A]["points"], players[A]["rank"], players[A]["of"]), (10, 1, 2))
        self.assertEqual((players[B]["points"], players[B]["rank"]), (6, 2))
        self.assertTrue(players[A]["lines"][0]["v"].startswith("#1, 10 pts"))
        top = [v for verb, _, v in sent if verb == "top"][0]
        self.assertEqual(top["rows"], [{"n": "Anna", "p": 10}, {"n": "Ben", "p": 6}])

    def test_who_left_gets_nothing(self):
        b = self.board()
        b.feed("[12:00:01] Anna joined (%s, player), 1/16 players" % A)
        b.feed("[12:00:02] Anna left (quit)")
        self.later(b, leaderboard.HUD_AFTER_JOIN + 1)
        self.assertEqual([x for x in self.coco() if x[0] == "player"], [])

    def test_everyone_again_every_few_minutes(self):
        b = self.board()
        b.online = {"Anna": A, "Cleo": C}
        self.later(b, leaderboard.HUD_EVERY + 1)
        verbs = sorted((verb, who or "") for verb, who, _ in self.coco())
        self.assertEqual(verbs, [("player", A), ("player", C), ("top", "")])

    def test_another_board_and_no_week_line_on_the_week(self):
        b = self.board(TOOLS_HUD_BOARD="week")
        b.online = {"Anna": A}
        self.later(b, leaderboard.HUD_EVERY + 1)
        player = [v for verb, _, v in self.coco() if verb == "player"][0]
        self.assertTrue(player["board"].startswith("Week "), player)
        self.assertNotIn("lines", player)


if __name__ == "__main__":
    unittest.main()
