"""rollover.py: stopped containers of a server are removed after cleanup_hours, keeping the newest one."""
import io
import os
import sys
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [os.path.join(ROOT, "rollover"), os.path.join(ROOT, "app")]
import rollover  # noqa: E402


def stamp(hours_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000000000Z", time.gmtime(time.time() - hours_ago * 3600))


class Inst:
    def __init__(self, name, running, hours_ago=0, healthy=True):
        self.name, self.running, self.healthy = name, running, healthy
        self.info = {"Id": name, "Name": "/" + name, "State": {"FinishedAt": stamp(hours_ago) if not running else ""}}


class CleanupTest(unittest.TestCase):
    def run_cleanup(self, instances, dry=False, server=None, protect=()):
        ro = rollover.Rollover({"dry_run": dry, "protect": list(protect), "servers": {}})
        with mock.patch.object(rollover, "docker") as docker, redirect_stdout(io.StringIO()):
            ro.cleanup("1", server or {}, instances)
        return [c.args[1] for c in docker.call_args_list if c.args[0] == "DELETE"]

    def test_removes_old_keeps_newest_and_running(self):
        gone = self.run_cleanup([Inst("live", True), Inst("prev", False, 80), Inst("older", False, 200), Inst("recent", False, 5)])
        self.assertEqual(sorted(gone), ["/containers/older", "/containers/prev"])

    def test_nothing_mid_rollover_or_unhealthy(self):
        self.assertEqual(self.run_cleanup([Inst("a", True), Inst("b", True), Inst("x", False, 500), Inst("y", False, 600)]), [])
        self.assertEqual(self.run_cleanup([Inst("a", True, healthy=False), Inst("x", False, 500), Inst("y", False, 600)]), [])

    def test_dry_run_deletes_nothing(self):
        self.assertEqual(self.run_cleanup([Inst("live", True), Inst("a", False, 80), Inst("b", False, 200)], dry=True), [])

    def test_off_and_protected(self):
        many = [Inst("live", True), Inst("a", False, 80), Inst("b", False, 200)]
        self.assertEqual(self.run_cleanup(many, server={"cleanup_hours": 0}), [])
        self.assertEqual(self.run_cleanup(many, protect=["b"]), [])

    def test_leftover_pattern(self):
        for name in ("/reskate-bot-prev", "/reskate-bridge-prev-teams", "/reskate-rollover-old", "/reskate-bot-admin-prev-teams"):
            self.assertTrue(rollover.LEFTOVER.match(name), name)
        for name in ("/reskate-bot", "/reskate-server-1-2.1.1-prev", "/reskate-dashboard-prev", "/reskate-server-test"):
            self.assertFalse(rollover.LEFTOVER.match(name), name)


if __name__ == "__main__":
    unittest.main()
