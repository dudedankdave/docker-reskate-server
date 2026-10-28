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

    def test_new_mods_first_then_the_rest_after_done(self):
        self.write("MODS=A-Map\nMAP_POOL=X\n")
        self.check()
        self.write("MODS=A-Map,B-Map\nMAP_POOL=X,B\n")
        polls = []

        def call(method, path, body=None, timeout=40):
            if "after=999999999" in path:
                return {"events": [], "last": 7}
            polls.append(list(self.sent))
            return {"events": [{"id": 8, "server": "1", "text": "[20:00:00] [mods] done, maps reloaded"}], "last": 8}
        with mock.patch.object(mod_settings.bridgeclient, "call", side_effect=call):
            self.check()
        self.assertEqual(polls, [[("1", "a", "mods add B-Map")]])            # the pool waited for "done"
        self.assertEqual([x[2] for x in self.sent], ["mods add B-Map", "map-pool clear", "map-pool add X", "map-pool add B"])

    def test_no_done_still_sends_the_rest(self):
        self.write("MODS=A-Map\n")
        self.check()
        self.write("MODS=A-Map,B-Map\nWELCOME_MESSAGE=hi\n")
        with mock.patch.object(mod_settings.bridgeclient, "call", return_value={"events": [], "last": 7}), \
                mock.patch.object(mod_settings, "MODS_WAIT", 0.01):
            out = self.check()
        self.assertIn("no \"[mods] done\"", out)
        self.assertEqual([x[2] for x in self.sent], ["mods add B-Map", "welcome hi"])


class AnnouncerModuleTest(unittest.TestCase):
    def test_announcement(self):
        self.assertEqual(mod_announcer.announcement("[20:00:00] Bob was kicked until the server restarts."),
                         "Bob was kicked.")
        self.assertEqual(mod_announcer.announcement("[20:00:00] Bob was kicked for 30 minutes."),
                         "Bob was kicked for 30 minutes.")
        self.assertEqual(mod_announcer.announcement("[20:00:00] Bob was kicked for 1 minute."), "Bob was kicked for 1 minute.")
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


class CoachTest(unittest.TestCase):
    RAW = {"verbs": ["tells you"], "pad": 5, "lines": [{"text": "/top best"}, {"text": "/rank yours"}],
           "servers": {"1": {"extra": [{"text": "vote"}]}, "6": ["rules"]}}

    def feed(self, coach, server, *lines, raw=None):
        out = []
        for line in lines:
            out += coach.event({"server": server, "slot": "A", "text": f"[20:00:00] {line}"}, self.RAW if raw is None else raw)
        return out

    def test_messages(self):
        got = mod_announcer.coach_messages(self.RAW, "2", "7656")
        self.assertEqual(got, ["msg 7656 Coach Frank tells you:     /top best", "msg 7656 Coach Frank tells you:     /rank yours",
                               "msg 7656 " + "-" * 30])
        self.assertEqual(len(mod_announcer.coach_messages(self.RAW, "1", "7656")), 4)
        self.assertEqual(mod_announcer.coach_messages(self.RAW, "6", "7656")[0], "msg 7656 Coach Frank tells you:     rules")
        self.assertEqual(mod_announcer.coach_messages({}, "2", "7656"), [])
        long = mod_announcer.coach_messages({"pad": 100, "lines": ["x" * 300]}, "2", "1")[0]
        self.assertLessEqual(len(long.encode()), len("msg 1 ") + 200)

    def test_random_verbs_come_from_the_list(self):
        raw = {"verbs": ["a", "b", "c"], "lines": ["x"] * 30}
        verbs = {m.split(":")[0].split("Coach Frank ")[1] for m in mod_announcer.coach_messages(raw, "2", "1")[:-1]}
        self.assertTrue(verbs <= {"a", "b", "c"} and len(verbs) > 1)

    def test_follows_joins_and_answers_the_command_once(self):
        coach = mod_announcer.Coach()
        out = self.feed(coach, "2", "Bob joined (7656, admin), 1/100 players, loaded in 3 s", "[command] Bob /coach")
        self.assertEqual([x[2] for x in out][-1], "msg 7656 " + "-" * 30)
        self.assertEqual(out[0][:2], ("2", "A"))
        self.assertEqual(self.feed(coach, "2", "[command] Bob /coach"), [])                  # asked again at once
        self.assertEqual(self.feed(coach, "2", "[command] Eve /coach"), [])                  # nobody of that name on
        self.assertEqual(self.feed(coach, "2", "Bob left (quit) [rtt 5 ms]", "[command] Bob /coach"), [])
        self.assertEqual(self.feed(coach, "2", "Bob joined (7656), 1/100 players", "[command] Bob: /coach", raw={}), [])

    def test_typed_text_that_looks_like_the_command_is_ignored(self):
        coach = mod_announcer.Coach()
        out = self.feed(coach, "2", "Bob joined (1), 1/100 players", "Eve joined (2), 2/100 players",
                        "[command] Bob: /coach /coach")
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main()
