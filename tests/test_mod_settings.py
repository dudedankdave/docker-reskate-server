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
        self.assertEqual(mod_announcer.announcement("[20:00:00] Bob was banned."), "Bob was banned.")
        self.assertIsNone(mod_announcer.announcement("[20:00:00] [chat] Server: Bob was banned."))


class ScheduleTest(unittest.TestCase):
    DATA = {"default": [{"text": "cmds", "every": 10}, {"text": "discord", "every": 20, "offset": 5, "say": True}],
            "servers": {"1": {"extra": [{"text": "maps", "every": 10}]}, "3": [], "6": [{"text": "rules", "every": 5}]}}

    def test_lines_for(self):
        texts = lambda n: [x[0] for x in mod_announcer.lines_for(self.DATA, n)]  # noqa: E731
        self.assertEqual(texts("2"), ["cmds", "discord"])
        self.assertEqual(texts("1"), ["cmds", "discord", "maps"])
        self.assertEqual(texts("3"), [])
        self.assertEqual(texts("6"), ["rules"])
        self.assertEqual(mod_announcer.lines_for(self.DATA, "2")[1], ("discord", 1200, 300, True))

    def test_due(self):
        self.assertTrue(mod_announcer.due(600, 0, 599, 601))
        self.assertFalse(mod_announcer.due(600, 0, 601, 700))
        self.assertTrue(mod_announcer.due(1200, 300, 1499, 1501))     # 25 min = offset 5 of the second period
        self.assertFalse(mod_announcer.due(1200, 300, 1501, 1999))

    def test_tick_sends_to_slots_with_players_only(self):
        sent = []
        row = {"server": 2, "slots": {"A": {"running": True, "players": 3}, "B": {"running": False, "players": None}}}
        with mock.patch.object(mod_announcer, "load_schedule", return_value=self.DATA), \
                mock.patch.object(mod_announcer.bridgeclient, "servers", return_value=[row]), \
                mock.patch.object(mod_announcer, "doubled", return_value=False), \
                mock.patch.object(mod_announcer.bridgeclient, "console",
                                  side_effect=lambda n, line, by, slot=None: sent.append((n, slot, line)) or True):
            s = mod_announcer.Schedule()
            s.tick(599)                                               # first tick: only remembers the time
            s.tick(601)
        self.assertEqual(sent, [("2", "A", "announce cmds")])

    def test_own_announcements_skip_the_server(self):
        with mock.patch.object(common, "server_settings", return_value={"A": {"ANNOUNCEMENT_INTERVAL_MINUTES": "10",
                                                                             "ANNOUNCEMENTS": "hi"}}):
            self.assertTrue(mod_announcer.doubled("2"))
        with mock.patch.object(common, "server_settings", return_value={"A": {"ANNOUNCEMENT_INTERVAL_MINUTES": "0"}}):
            self.assertFalse(mod_announcer.doubled("2"))


if __name__ == "__main__":
    unittest.main()
