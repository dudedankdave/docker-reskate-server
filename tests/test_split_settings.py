"""stack/split_settings.py: container env -> settings files that read back the same."""
import os
import sys
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [os.path.join(ROOT, "stack"), os.path.join(ROOT, "rollover"), os.path.join(ROOT, "app")]
import settings  # noqa: E402
import split_settings  # noqa: E402

TRICKY = ["plain", "two words", "", " lead", "a #hash", "it's", 'say "hi"', "back\\slash", "line\nbreak",
          '[{"name": "discord", "reply": "Join us"}]', "Join|Be nice", "x=y", "tab\there"]


class SplitTest(unittest.TestCase):
    def test_quote_round_trip(self):
        for value in TRICKY:
            with self.subTest(value=value):
                self.assertEqual(settings.parse(f"KEY={split_settings.quote(value)}\n"), [("KEY", value, False)])

    def test_split(self):
        envs = {
            "1": {"PORT": "12400", "STEAM_TOKEN": "a", "PATH": "/usr/bin", "WELCOME_MESSAGE": "Hi", "MAP": "Grom",
                  "SERVER_PASSWORD": "pw", "WEBHOOK_URL_admin": "https://h/1", "ADMINS": "1"},
            "2": {"PORT": "12410", "STEAM_TOKEN": "b", "PATH": "/usr/bin", "WELCOME_MESSAGE": "Hi", "MAP": "Plaza",
                  "SERVER_PASSWORD": "pw", "WEBHOOK_URL_admin": "https://h/2", "ADMINS": "1"},
        }
        files = split_settings.split(envs)
        self.assertEqual(files["common.env"], {"WELCOME_MESSAGE": "Hi", "ADMINS": "1"})
        self.assertEqual(files[os.path.join("secrets", "common.env")], {"SERVER_PASSWORD": "pw"})
        self.assertEqual(files["server-1.env"], {"MAP": "Grom"})
        self.assertEqual(files[os.path.join("secrets", "server-2.env")], {"WEBHOOK_URL_admin": "https://h/2"})
        everything = " ".join(str(v) for v in files.values())
        for kept in ("12400", "/usr/bin", "STEAM_TOKEN"):
            self.assertNotIn(kept, everything)


if __name__ == "__main__":
    unittest.main()
