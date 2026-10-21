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
                mock.patch.object(self.ro, "few_enough", return_value=not players), \
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


class Info(Inst):
    def __init__(self, env, mounts=(), started="2026-10-21T10:00:00.000Z"):
        super().__init__(env)
        self.info = {"Id": "abc", "Mounts": [{"Destination": m} for m in mounts], "State": {"StartedAt": started}}


class RestartTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.ro = rollover.Rollover({"dry_run": False, "settings_dir": self.dir.name, "shared_volume": "reskate-shared",
                                     "servers": {}})

    def tearDown(self):
        self.dir.cleanup()

    def ask(self, when):
        os.makedirs(os.path.join(self.dir.name, ".restart"))
        path = os.path.join(self.dir.name, ".restart", "server-1")
        open(path, "w").close()
        os.utime(path, (when, when))

    def request(self, count):
        inst = Info({})
        with mock.patch.object(rollover, "players", return_value=count), \
                mock.patch.object(self.ro, "restart") as restart, redirect_stdout(io.StringIO()):
            return self.ro.restart_requested("1", {}, inst), restart.called

    def test_few_players_is_fine_more_is_not(self):
        inst = Info({})
        with mock.patch.object(rollover, "players", return_value=3):
            self.assertTrue(self.ro.few_enough({}, inst))
        with mock.patch.object(rollover, "players", return_value=4):
            self.assertFalse(self.ro.few_enough({}, inst))
        with mock.patch.object(rollover, "players", return_value=1):
            self.assertFalse(self.ro.few_enough({"restart_max_players": 0}, inst))

    def test_no_request_file(self):
        self.assertEqual(self.request(0), (False, False))

    def test_request_restarts_once_then_is_old(self):
        self.ask(rollover.timegm((2026, 10, 21, 11, 0, 0)))
        self.assertEqual(self.request(2), (True, True))
        os.utime(os.path.join(self.dir.name, ".restart", "server-1"), (1, 1))   # the server started after it
        self.assertEqual(self.request(2), (False, False))

    def test_request_waits_while_busy(self):
        self.ask(rollover.timegm((2026, 10, 21, 11, 0, 0)))
        self.assertEqual(self.request(9), (True, False))

    def test_missing_standard_mounts(self):
        ro = self.ro
        self.assertEqual(ro.missing_mounts({}, Info({}, ["/data", "/mods"])), ["/shared"])
        self.assertEqual(ro.missing_mounts({}, Info({}, ["/data", "/mods", "/shared"])), [])
        self.assertTrue(ro.lacks_mounts({}, Info({}, ["/data"])))
        self.assertEqual(ro.missing_mounts({"repair_mods": False}, Info({}, ["/data"])), [])

    def test_missing_console(self):
        inst = Info({}, ["/data", "/mods", "/shared"])
        inst.info["Config"] = {"OpenStdin": False}
        self.assertEqual(self.ro.missing_mounts({}, inst), ["a console"])
        inst.info["Config"] = {"OpenStdin": True}
        self.assertFalse(self.ro.lacks_mounts({}, inst))


if __name__ == "__main__":
    unittest.main()
