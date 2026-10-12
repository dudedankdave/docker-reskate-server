"""rollover.py: a newer image revision of the same ReSkate version replaces an empty server."""
import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [os.path.join(ROOT, "rollover"), os.path.join(ROOT, "app")]
import rollover  # noqa: E402


class Inst:
    name, slot, version = "reskate-server-1-2.0.7", "a", "2.0.7"

    def __init__(self, image, revision):
        self.env = {"RESKATE_IMAGE_REVISION": revision} if revision else {}
        self.info = {"Id": "abc", "Config": {"Image": image}}


class RevisionTest(unittest.TestCase):
    def run_update(self, inst, on_hub, players=False, on=True):
        ro = rollover.Rollover({"dry_run": True, "image_updates": on, "servers": {}})
        with mock.patch.object(rollover, "hub_revision", return_value=on_hub), \
                mock.patch.object(ro, "empty_for", return_value=0 if players else 120), \
                mock.patch.object(ro, "start_copy") as copy, redirect_stdout(io.StringIO()):
            pending = ro.image_update("1", {}, inst, [])
        return pending, copy.call_args

    def test_newer_revision_on_an_empty_server(self):
        pending, call = self.run_update(Inst("dudedankdave/reskate-server:2.0.7", "r40"), 42)
        self.assertTrue(pending)
        self.assertEqual(call.kwargs, {"suffix": "-r42", "tag": "2.0.7-r42"})

    def test_waits_while_players_are_on(self):
        self.assertEqual(self.run_update(Inst("dudedankdave/reskate-server:2.0.7", "r40"), 42, players=True), (True, None))

    def test_up_to_date_or_off_or_other_image(self):
        self.assertEqual(self.run_update(Inst("dudedankdave/reskate-server:2.0.7", "r42"), 42), (False, None))
        self.assertEqual(self.run_update(Inst("dudedankdave/reskate-server:2.0.7", "r40"), 42, on=False), (False, None))
        self.assertEqual(self.run_update(Inst("dudedankdave/reskate-server:2.0.7-slim", "r40"), 42), (False, None))
        self.assertEqual(self.run_update(Inst("reskate-server:dev", ""), 42), (False, None))

    def test_revision_of(self):
        self.assertEqual(rollover.revision_of({"RESKATE_IMAGE_REVISION": "r12"}), 12)
        self.assertEqual(rollover.revision_of({"RESKATE_IMAGE_REVISION": "dev"}), 0)
        self.assertEqual(rollover.revision_of({}), 0)


if __name__ == "__main__":
    unittest.main()
