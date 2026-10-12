"""app/notifier.py: [event] lines and the webhook hand-over."""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import notifier  # noqa: E402


def events(out):
    return [(line.split(" ", 2)[1], json.loads(line.split(" ", 2)[2]))
            for line in out.splitlines() if line.startswith("[event] ")]


class EventTest(unittest.TestCase):
    def send(self, hook, *args, **kw):
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(notifier, "request", return_value={}) as req, \
                mock.patch.object(notifier.handover, "external", return_value=False):
            hook.send(*args, **kw)
        return events(out.getvalue()), req

    def test_message_is_one_event_line(self):
        got, _ = self.send(notifier.Hook([], "x", ["1"], "admin"), "**UPDATE** <x>\nnext", ping=True)
        self.assertEqual(got, [("admin", {"ping": True, "text": "**UPDATE** <x>\nnext"})])

    def test_console_feeds_are_no_events(self):
        for scope in ("log", "chat", "leaderboard"):
            self.assertEqual(self.send(notifier.Hook([], "x", scope=scope), "line")[0], [])
        self.assertEqual(self.send(notifier.Hook([], "x", scope="public"), "line", event=False)[0], [])

    def test_handed_over_scope_still_emits_but_does_not_post(self):
        hook = notifier.Hook(["https://example.invalid/hook"], "x", scope="public")
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(notifier, "request", return_value={}) as req, \
                mock.patch.object(notifier.handover, "external", return_value=True):
            hook.send("Bob joined")
        self.assertEqual(len(events(out.getvalue())), 1)
        req.assert_not_called()


if __name__ == "__main__":
    unittest.main()
