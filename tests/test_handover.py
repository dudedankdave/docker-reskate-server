"""app/handover.py: /shared/modules.json read at run time (the same cases as slim/handover_test.go)."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import handover  # noqa: E402


class HandoverTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        handover.FILE = os.path.join(self.dir.name, "modules.json")

    def tearDown(self):
        self.dir.cleanup()

    def reset(self, server):
        os.environ["RESKATE_SERVER"] = server
        handover._state.update(at=0.0, mtime=None, parts={})

    def external(self, server, part, scope=None):
        self.reset(server)
        with redirect_stdout(io.StringIO()):
            return handover.external(part, scope)

    def test_without_file_everything_is_internal(self):
        self.assertFalse(self.external("1", "leaderboard"))
        self.assertFalse(self.external("1", "webhooks", "public"))

    def test_parts_scopes_and_servers(self):
        with open(handover.FILE, "w") as f:
            json.dump({"leaderboard": "external", "webhooks": {"public": "external"},
                       "servers": {"6": {"leaderboard": "internal", "webhooks": "external"}}}, f)
        cases = [("1", "leaderboard", None, True), ("1", "webhooks", "public", True),
                 ("1", "webhooks", "admin", False), ("1", "announcer", None, False),
                 ("6", "leaderboard", None, False), ("6", "webhooks", "admin", True)]
        for server, part, scope, want in cases:
            with self.subTest(server=server, part=part, scope=scope):
                self.assertEqual(self.external(server, part, scope), want)

    def test_broken_file_keeps_what_was_set(self):
        with open(handover.FILE, "w") as f:
            f.write('{"leaderboard": "external"}')
        self.assertTrue(self.external("1", "leaderboard"))
        with open(handover.FILE, "w") as f:
            f.write("{broken")
        os.utime(handover.FILE, (1, 1))
        handover._state["at"] = 0.0
        with redirect_stdout(io.StringIO()):
            self.assertTrue(handover.external("leaderboard"))


if __name__ == "__main__":
    unittest.main()
