"""rollover.py: a server whose settings files changed is restarted once it is empty."""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [os.path.join(ROOT, "rollover"), os.path.join(ROOT, "app")]
import rollover  # noqa: E402
import settings  # noqa: E402


class Inst:
    name = "reskate-server-1-2.0.7"

    def __init__(self, env):
        self.env, self.info = env, {"Id": "abc"}


class SettingsChangedTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        with open(os.path.join(self.dir.name, "server-1.env"), "w") as f:
            f.write("MAX_PLAYERS=20\n")
        self.ro = rollover.Rollover({"dry_run": False, "settings_dir": self.dir.name, "servers": {}})
        self.inst = Inst({"RESKATE_SERVER": "1"})
        values, _, _ = settings.load(self.inst.env, self.dir.name)
        self.now = settings.digest(values)

    def tearDown(self):
        self.dir.cleanup()

    def check(self, digest_in_log, players):
        lines = [f"[settings] digest {digest_in_log}"] if digest_in_log else []
        with mock.patch.object(rollover, "logs", return_value=lines), \
                mock.patch.object(self.ro, "empty_for", return_value=0 if players else 120), \
                mock.patch.object(self.ro, "restart") as restart, redirect_stdout(io.StringIO()):
            pending = self.ro.settings_changed("1", {}, self.inst)
        return pending, restart.called

    def test_unchanged(self):
        self.assertEqual(self.check(self.now, players=False), (False, False))

    def test_changed_and_empty_restarts(self):
        self.assertEqual(self.check("0" * 16, players=False), (True, True))

    def test_changed_with_players_waits(self):
        self.assertEqual(self.check("0" * 16, players=True), (True, False))

    def test_old_image_without_digest_is_left_alone(self):
        self.assertEqual(self.check(None, players=False), (False, False))


if __name__ == "__main__":
    unittest.main()
