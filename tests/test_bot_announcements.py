"""bot/announcements.py: the lines /schedule-add, /schedule-remove and /schedule-list edit."""
import os
import sys
import tempfile
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [os.path.join(ROOT, "bot")]
import announcements as an  # noqa: E402


class AnnouncementsTest(unittest.TestCase):
    def test_add_remove_list(self):
        data = {}
        self.assertEqual(an.add(data, "all", "Commands: /top", 10), 1)
        self.assertEqual(an.add(data, "all", "Discord", 20, 5, True), 2)
        self.assertEqual(an.add(data, "6", "Rules", 5), 1)
        self.assertEqual(data["servers"]["6"], {"extra": [{"text": "Rules", "every": 5, "offset": 0}]})
        text = an.listing(data)
        self.assertIn("2. every 20 min (+5 min, chat): Discord", text)
        self.assertIn("Server 6 (adds to all)", text)
        self.assertEqual(an.remove(data, "all", 1)["text"], "Commands: /top")
        self.assertEqual([e["text"] for e in data["default"]], ["Discord"])

    def test_own_list_keeps_being_a_list(self):
        data = {"servers": {"3": []}}
        an.add(data, "3", "x", 10)
        self.assertEqual(data["servers"]["3"][0]["text"], "x")
        self.assertIn("own list, replaces all", an.listing(data, "3"))

    def test_checks(self):
        for args in (("all", "", 10), ("all", "x", 0), ("all", "x", 10, 10), ("all", "x" * 201, 10)):
            with self.assertRaises(ValueError):
                an.add({}, *args)
        with self.assertRaises(ValueError):
            an.remove({}, "all", 1)

    def test_file_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "announcements.json")
            self.assertEqual(an.load(path), {})
            an.save({"default": []}, path)
            self.assertEqual(an.load(path), {"default": []})


if __name__ == "__main__":
    unittest.main()
