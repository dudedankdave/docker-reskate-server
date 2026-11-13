"""ReSkate 2.1.4-2: a release with a hotfix suffix is a valid, newer version than 2.1.4."""
import json
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [os.path.join(ROOT, "rollover"), os.path.join(ROOT, "app")]
import rollover  # noqa: E402
import updater  # noqa: E402

LAUNCHER = json.dumps({"server_linux": {"version": "2.1.4-2", "url": "asset:x.tar.gz"}}).encode()


class HotfixVersionTest(unittest.TestCase):
    def test_ranks_above_the_base_release(self):
        self.assertGreater(updater.numbers("2.1.4-2"), updater.numbers("2.1.4"))
        self.assertGreater(rollover.numbers("2.1.4-2"), rollover.numbers("2.1.4"))
        self.assertLess(updater.numbers("2.1.4-2"), updater.numbers("2.1.5"))

    def test_hotfix_suffix_orders_after_its_release(self):
        order = ["2.1.4-5", "2.1.4-5-hotfix", "2.1.4-5-hotfix-2", "2.1.5"]
        for mod in (updater, rollover):
            keys = [mod.numbers(v) for v in order]
            self.assertEqual(keys, sorted(keys))
            self.assertEqual(len(set(keys)), 4)

    def test_hotfix_versions_are_accepted(self):
        for v in ("2.1.4-5-hotfix", "2.1.4-5-hotfix-2"):
            body = json.dumps({"server_linux": {"version": v, "url": "asset:x.tar.gz"}}).encode()
            with mock.patch.object(rollover, "fetch", return_value=body):
                self.assertEqual(rollover.latest_release(), v)
            with mock.patch.object(updater, "curl", return_value=body):
                self.assertEqual(updater.latest()["version"], v)

    def test_rollover_accepts_it(self):
        with mock.patch.object(rollover, "fetch", return_value=LAUNCHER):
            self.assertEqual(rollover.latest_release(), "2.1.4-2")

    def test_updater_accepts_it(self):
        with mock.patch.object(updater, "curl", return_value=LAUNCHER):
            self.assertEqual(updater.latest()["version"], "2.1.4-2")

    def test_garbage_is_still_refused(self):
        bad = json.dumps({"server_linux": {"version": "2.1.4-rc"}}).encode()
        with mock.patch.object(rollover, "fetch", return_value=bad):
            with self.assertRaises(RuntimeError):
                rollover.latest_release()


if __name__ == "__main__":
    unittest.main()
