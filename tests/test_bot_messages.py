"""bot/messages.py: the texts /message-set writes into the settings files."""
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [os.path.join(ROOT, "bot"), os.path.join(ROOT, "app")]
import messages  # noqa: E402
import settings  # noqa: E402


class MessagesTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        p = mock.patch.object(messages, "DIR", self.dir.name)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.dir.cleanup)

    def test_set_replaces_only_its_key_and_the_settings_parser_reads_it_back(self):
        with open(messages.path("6"), "w") as f:
            f.write("MAX_PLAYERS=20\nWELCOME_MESSAGE=old\n")
        messages.set_text("6", "join", 'Say "hi" \\ welcome')
        with open(messages.path("6")) as f:
            parsed = {k: v for k, v, _ in settings.parse(f.read())}
        self.assertEqual(parsed, {"MAX_PLAYERS": "20", "WELCOME_MESSAGE": 'Say "hi" \\ welcome'})

    def test_all_and_reset(self):
        messages.set_text("all", "ranked", "You are RANKED")
        self.assertTrue(messages.path("all").endswith("common.env"))
        self.assertEqual(messages.current("all"), {"ranked": "You are RANKED"})
        messages.unset("all", "ranked")
        self.assertEqual(messages.current("all"), {})
        with self.assertRaises(ValueError):
            messages.unset("all", "ranked")

    def test_limits(self):
        for text in ("", " ", "x" * 201):
            with self.assertRaises(ValueError):
                messages.set_text("1", "join", text)

    def test_any_documented_setting_secrets_apart_and_unknown_refused(self):
        messages.set_key("2", "map_pool", "A,B")
        messages.set_key("2", "SERVER_PASSWORD", "geheim")
        messages.set_key("all", "MAX_PLAYERS", "30")
        self.assertTrue(os.path.exists(os.path.join(self.dir.name, "secrets", "server-2.env")))
        self.assertEqual(messages.current_keys("2"), {"MAP_POOL": "A,B", "SERVER_PASSWORD": "(secret)"})
        self.assertEqual(messages.current_keys("all"), {"MAX_PLAYERS": "30"})
        for key in ("STEAM_TOKEN", "PORT", "NOPE", ""):
            with self.assertRaises(ValueError):
                messages.set_key("2", key, "x")
        self.assertTrue(messages.live("MAP_POOL"))
        self.assertFalse(messages.live("MAX_PLAYERS"))
        messages.unset_key("2", "map_pool")
        self.assertNotIn("MAP_POOL", messages.current_keys("2"))


if __name__ == "__main__":
    unittest.main()
