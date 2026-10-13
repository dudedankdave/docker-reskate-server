"""bot/console.py: when a console line needs a confirmation on a busy server."""
import os
import sys
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [os.path.join(ROOT, "bot"), os.path.join(ROOT, "rollover"), os.path.join(ROOT, "app")]
import console  # noqa: E402


class ConfirmationTest(unittest.TestCase):
    def test_busy_server_warns_only_for_more_than_talking(self):
        self.assertIsNotNone(console.needs_confirmation("map Grom", 4))
        self.assertIsNotNone(console.needs_confirmation("/kick Bob", 27))
        self.assertIn("27 players", console.needs_confirmation("kick Bob", 27))
        self.assertIsNone(console.needs_confirmation("say hi", 27))
        self.assertIsNone(console.needs_confirmation("announce Hello", 27))
        self.assertIsNone(console.needs_confirmation("map Grom", 3))      # at the cutoff: no warning
        self.assertIsNone(console.needs_confirmation("map Grom", None))
        self.assertIsNone(console.needs_confirmation("map Grom", 6, cutoff=10))

    def test_stop_words_stay_refused(self):
        self.assertIsNotNone(console.refused("restart"))


if __name__ == "__main__":
    unittest.main()
