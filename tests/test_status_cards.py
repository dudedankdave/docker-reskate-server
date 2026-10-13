"""rollover/status.py cards: the join code of each running slot while a rollover runs two."""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "rollover"))
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


if __name__ == "__main__":
    unittest.main()
