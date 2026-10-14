"""rollover/status.py cards: the join code of each running slot while a rollover runs two, and
the mod and version of the map being played."""
import json
import os
import tempfile
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [os.path.join(ROOT, "rollover"), os.path.join(ROOT, "app")]
import mods  # noqa: E402
import status  # noqa: E402

SERVER = {"role": "Main", "slots": {"a": {"port": 12400}, "b": {"port": 12402}}}


def info(id, port, version, health="healthy"):
    return {"Id": id, "Name": f"/reskate-server-1-{version}",
            "Config": {"Env": [f"PORT={port}", f"RESKATE_IMAGE_VERSION={version}"], "Labels": {}},
            "State": {"Running": True, "Health": {"Status": health}, "StartedAt": "2026-10-13T22:00:00Z"}}


class CardTest(unittest.TestCase):
    def code_row(self, infos, logs):
        board = status.Board({"servers": {"1": SERVER}})
        follow = lambda i: SimpleNamespace(ok=True, players=3, max=16, map="Isle of Grom", code=logs[i["Id"]])  # noqa: E731
        with mock.patch.object(board, "follow", side_effect=follow):
            card, _, _ = board.card("1", SERVER, infos)
        return next(f["value"] for f in card["fields"] if f["name"] == "Join code")

    def test_one_slot_shows_its_code(self):
        self.assertEqual(self.code_row([info("x", 12400, "2.0.8")], {"x": "ABCD"}), "`ABCD`")

    def test_both_slots_during_a_rollover(self):
        infos = [info("new", 12402, "2.0.8"), info("old", 12400, "2.0.7")]
        self.assertEqual(self.code_row(infos, {"new": "NEW1", "old": "OLD1"}), "A · `OLD1` (2.0.7)\nB · `NEW1` (2.0.8)")

    def test_a_slot_not_up_has_no_code(self):
        infos = [info("new", 12402, "2.0.8"), info("old", 12400, "2.0.7", health="unhealthy")]
        self.assertEqual(self.code_row(infos, {"new": "NEW1", "old": "OLD1"}), "`NEW1`")


def log_state(*lines):
    s = status.Server({"Id": "x", "State": {}})
    for line in lines:
        s.line(line)
    s.ok = True
    return s


class MapVersionTest(unittest.TestCase):
    def card(self, infos, states):
        board = status.Board({"servers": {"1": SERVER}})
        with mock.patch.object(board, "follow", side_effect=lambda i: states[i["Id"]]):
            card, _, _ = board.card("1", SERVER, infos)
        return {f["name"]: f["value"] for f in card["fields"]}

    def test_the_log_line_names_the_mod_of_the_map(self):
        s = log_state("[mods] map Skate3Improved: 333-Skate_3_Improved 1.0.8",
                      "[22:00:00] Main is up on Skate3Improved for 100 players.")
        self.assertEqual(status.map_mod(s), ("333-Skate_3_Improved", "1.0.8"))
        self.assertIsNone(status.map_mod(log_state("[22:00:00] Main is up on Super Ultra Mega Resort for 100 players.")))

    def test_the_map_row_shows_the_mod_version(self):
        s = log_state("[mods] map Skate3Improved: 333-Skate_3_Improved 1.0.8",
                      "[22:00:00] Main is up on Skate3Improved for 100 players.")
        self.assertEqual(self.card([info("x", 12400, "2.0.7")], {"x": s})["Map"],
                         "Skate3Improved\n`333-Skate_3_Improved 1.0.8`")

    def test_an_old_copy_from_a_map_update_shows_its_map_version(self):
        new = log_state("[mods] map Skate3Improved: 333-Skate_3_Improved 1.0.8",
                        "[22:00:00] Main is up on Skate3Improved for 100 players.")
        old = log_state("[mods] map Skate3Improved: 333-Skate_3_Improved 1.0.7",
                        "[20:00:00] Main is up on Skate3Improved for 100 players.",
                        "[21:00:00] [network] 12 players, 1 KB/s out")
        infos = [info("new", 12402, "2.0.7"), info("old", 12400, "2.0.7")]
        self.assertEqual(self.card(infos, {"new": new, "old": old})["Old version"],
                         "**12** on 2.0.7, 333-Skate_3_Improved 1.0.7")

    def test_an_old_release_keeps_just_its_version(self):
        new = log_state("[mods] map Skate3Improved: 333-Skate_3_Improved 1.0.8",
                        "[22:00:00] Main is up on Skate3Improved for 100 players.")
        old = log_state("[mods] map Skate3Improved: 333-Skate_3_Improved 1.0.7",
                        "[20:00:00] Main is up on Skate3Improved for 100 players.")
        infos = [info("new", 12402, "2.0.8"), info("old", 12400, "2.0.7")]
        self.assertEqual(self.card(infos, {"new": new, "old": old})["Old version"], "**0** on 2.0.7")


class LogMapsTest(unittest.TestCase):
    def test_one_line_per_map_with_the_version_in_place(self):
        with tempfile.TemporaryDirectory() as d:
            mod = os.path.join(d, "Skate_3_Improved")
            os.makedirs(mod)
            with open(os.path.join(mod, "manifest.json"), "w") as f:
                json.dump({"version_number": "1.0.8"}, f)
            with open(os.path.join(mod, "reskate-levels.json"), "w") as f:
                json.dump({"levels": [{"displayName": "Skate3Improved"}, {"name": "no display name"}]}, f)
            with mock.patch.object(mods, "MODS_DIR", d), mock.patch.object(mods, "log") as log:
                mods.log_maps("333", "Skate_3_Improved")
                mods.log_maps("333", "Not_Installed")
        self.assertEqual([c.args[0] for c in log.call_args_list], ["map Skate3Improved: 333-Skate_3_Improved 1.0.8"])
        line = "[mods] " + log.call_args_list[0].args[0]
        self.assertEqual(log_state(line).mods, {"skate3improved": ("333-Skate_3_Improved", "1.0.8")})


if __name__ == "__main__":
    unittest.main()
