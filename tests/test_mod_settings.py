"""modules/mod_settings.py and mod_announcer.py."""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [os.path.join(ROOT, "modules"), os.path.join(ROOT, "rollover"), os.path.join(ROOT, "app")]
import common  # noqa: E402
import mod_announcer  # noqa: E402
import mod_settings  # noqa: E402


class SettingsModuleTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.sent = []
        self.patches = [
            mock.patch.object(mod_settings.settings, "DIR", self.dir.name),
            mock.patch.object(common, "server_settings", return_value={"a": {"RESKATE_SERVER": "1"}}),
            mock.patch.object(mod_settings.bridgeclient, "console",
                              side_effect=lambda n, line, by, slot=None: self.sent.append((n, slot, line)) or True),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.dir.cleanup()

    def write(self, text):
        with open(os.path.join(self.dir.name, "server-1.env"), "w") as f:
            f.write(text)

    def check(self):
        out = io.StringIO()
        with redirect_stdout(out):
            mod_settings.Module().check_server("1")
        return out.getvalue()

    def test_first_run_takes_the_state_then_types_changes(self):
        self.write("WELCOME_MESSAGE=hi\nMAX_PLAYERS=10\n")
        self.check()
        self.assertEqual(self.sent, [])                                  # first run: nothing typed
        self.write("WELCOME_MESSAGE=hello\nMAX_PLAYERS=12\nSERVER_PASSWORD=secret\n")
        out = self.check()
        self.assertEqual(self.sent, [("1", "a", "password secret"), ("1", "a", "welcome hello")])
        self.assertIn("MAX_PLAYERS need a restart", out)
        self.assertNotIn("secret", out)
        self.check()
        self.assertEqual(len(self.sent), 2)                              # nothing new: nothing again


class AnnouncerModuleTest(unittest.TestCase):
    def test_announcement(self):
        self.assertEqual(mod_announcer.announcement("[20:00:00] Bob was kicked until the server restarts."),
                         "Bob was kicked.")
        self.assertEqual(mod_announcer.announcement("[20:00:00] Bob was kicked for 30 minutes."),
                         "Bob was kicked for 30 minutes.")
        self.assertEqual(mod_announcer.announcement("[20:00:00] Bob was kicked for 1 minute."), "Bob was kicked for 1 minute.")
        self.assertEqual(mod_announcer.announcement("[20:00:00] Bob was banned."), "Bob was banned.")
        self.assertIsNone(mod_announcer.announcement("[20:00:00] [chat] Server: Bob was banned."))


if __name__ == "__main__":
    unittest.main()
