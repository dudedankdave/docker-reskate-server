"""A release ReSkate pulls again (latest goes back to an older version): rollover, updater and
notifier go back to it instead of staying on the pulled one."""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [os.path.join(ROOT, "rollover"), os.path.join(ROOT, "app")]
import rollover  # noqa: E402
import updater  # noqa: E402


def container(name, version, port, slot, generation, running=True):
    return {"Id": name, "Name": "/" + name, "State": {"Running": running},
            "Config": {"Env": [f"PORT={port}"], "Image": f"dudedankdave/reskate-server:{version}",
                       "Labels": {"reskate.rollover": "true", "reskate.version": version,
                                  "reskate.generation": str(generation)}}}


class RolloverTest(unittest.TestCase):
    server = {"slots": {"a": {"port": 12400}, "b": {"port": 12402}}}

    def run_server(self, *infos, empty_for=0):
        ro = rollover.Rollover({"dry_run": True, "servers": {"1": self.server}})
        instances = [rollover.Instance(i, "a" if i["Config"]["Env"] == ["PORT=12400"] else "b") for i in infos]
        with mock.patch.object(ro, "roll") as roll, mock.patch.object(ro, "stop") as stop, \
                mock.patch.object(ro, "empty_for", return_value=empty_for), \
                mock.patch.object(rollover, "hub_has", return_value=True), redirect_stdout(io.StringIO()):
            ro.server("1", self.server, "2.0.7", instances)
        return roll, stop

    def test_a_pulled_version_ranks_below_the_latest(self):
        old = rollover.Instance(container("s-2.0.7", "2.0.7", 12400, "a", 1), "a")
        pulled = rollover.Instance(container("s-2.0.9", "2.0.9", 12402, "b", 2), "b")
        self.assertGreater(old.rank("2.0.7"), pulled.rank("2.0.7"))
        self.assertLess(old.rank("2.0.9"), pulled.rank("2.0.9"))

    def test_the_pulled_copy_drains_not_the_latest(self):
        roll, stop = self.run_server(container("s-2.0.7", "2.0.7", 12400, "a", 1),
                                     container("s-2.0.9", "2.0.9", 12402, "b", 2), empty_for=3600)
        self.assertEqual([c.args[0].name for c in stop.call_args_list], ["s-2.0.9"])
        roll.assert_not_called()

    def test_only_the_pulled_version_runs_goes_back(self):
        roll, stop = self.run_server(container("s-2.0.9", "2.0.9", 12402, "b", 2))
        stop.assert_not_called()
        self.assertEqual(roll.call_args.args[5], "2.0.7")
        self.assertTrue(roll.call_args.kwargs["back"])


class PrepareTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = self.tmp.name
        self.dirs = {k: os.path.join(base, k) for k in ("app", "server", "server.new", "server.old")}
        os.makedirs(self.dirs["app"])
        os.makedirs(self.dirs["server"])
        for d in ("app", "server"):
            with open(os.path.join(self.dirs[d], "ReSkateServer"), "w") as f:
                f.write(d)
        updater.write_version(self.dirs["server"], "2.0.7")
        self.patch = mock.patch.multiple(updater, IMAGE=self.dirs["app"], DIR=self.dirs["server"],
                                         STAGING=self.dirs["server.new"], OLD=self.dirs["server.old"])
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def server_binary(self):
        with open(os.path.join(self.dirs["server"], "ReSkateServer")) as f:
            return f.read()

    def test_keeps_the_release_it_went_back_to(self):
        with open(os.path.join(self.dirs["server"], updater.PULLED_FILE), "w") as f:
            f.write("2.0.8\n")
        updater.prepare("2.0.8", lambda m: None)
        self.assertEqual(updater.version_of(self.dirs["server"]), "2.0.7")
        self.assertEqual(self.server_binary(), "server")

    def test_a_newer_image_replaces_it(self):
        with open(os.path.join(self.dirs["server"], updater.PULLED_FILE), "w") as f:
            f.write("2.0.8\n")
        updater.prepare("2.0.9", lambda m: None)
        self.assertEqual(updater.version_of(self.dirs["server"]), "2.0.9")
        self.assertEqual(self.server_binary(), "app")
        self.assertEqual(updater.pulled_from(self.dirs["server"]), "")

    def test_without_the_mark_a_newer_image_replaces_it(self):
        updater.prepare("2.0.8", lambda m: None)
        self.assertEqual(updater.version_of(self.dirs["server"]), "2.0.8")


class NotifierCheckTest(unittest.TestCase):
    def setUp(self):
        import notifier
        self.notifier = notifier

    def check(self, n, latest, now=False):
        with mock.patch.object(updater, "latest", return_value={"version": latest}), \
                mock.patch.object(self.notifier, "read_json", return_value={}), \
                mock.patch.object(self.notifier, "note"), \
                mock.patch.object(self.notifier.threading, "Thread") as thread:
            self.notifier.Notifier.check(n, now=now)
        return thread

    def fake(self, mode="auto", running="2.0.9"):
        return SimpleNamespace(mode=mode, pulled=None, busy=None, lock=mock.MagicMock(),
                               news=mock.Mock(), running=lambda: running, pinned_notice=mock.Mock(),
                               handle=mock.Mock(), apply=mock.Mock())

    def test_goes_back_on_the_second_check(self):
        n = self.fake()
        self.check(n, "2.0.7").assert_not_called()
        thread = self.check(n, "2.0.7")
        self.assertIs(thread.call_args.kwargs["target"], n.handle)

    def test_console_update_goes_back_at_once(self):
        n = self.fake()
        self.assertIs(self.check(n, "2.0.7", now=True).call_args.kwargs["target"], n.apply)

    def test_pinned_says_so(self):
        n = self.fake(mode="pinned")
        self.check(n, "2.0.7")
        self.check(n, "2.0.7")
        n.pinned_notice.assert_called_once_with("2.0.7", "2.0.9")

    def test_same_version_is_nothing_to_do(self):
        n = self.fake(running="2.0.7")
        self.check(n, "2.0.7").assert_not_called()
        self.check(n, "2.0.7").assert_not_called()


if __name__ == "__main__":
    unittest.main()
