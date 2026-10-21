import importlib.util
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent.parent


def load():
    os.environ.setdefault("DASHBOARD_PASSWORD", "x" * 12)
    os.environ.setdefault("BRIDGE_TOKEN", "t")
    spec = importlib.util.spec_from_file_location("dashboard", HERE / "modules" / "dashboard.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PACKAGES = [
    {"owner": "brassy", "name": "Industrial_Zone", "package_url": "https://thunderstore.io/c/reskate/p/brassy/Industrial_Zone/",
     "rating_score": 3, "categories": ["Maps"], "is_deprecated": False,
     "versions": [{"version_number": "1.0.2", "icon": "https://gcdn.thunderstore.io/x.png", "description": "A big industrial map", "downloads": 50}]},
    {"owner": "a", "name": "Board_Pack", "categories": ["Decks"], "is_deprecated": False,
     "versions": [{"version_number": "1.0.0", "icon": "", "description": "boards", "downloads": 900}]},
    {"owner": "b", "name": "Old_Map", "categories": ["Maps"], "is_deprecated": True,
     "versions": [{"version_number": "1.0.0", "icon": "", "description": "gone", "downloads": 1}]},
]


class ThunderstoreTest(unittest.TestCase):
    def setUp(self):
        self.d = load()
        self.d.TS_CACHE.update(at=0, rows=[])

    def search(self, *a):
        with mock.patch.object(self.d, "ts_fetch", return_value=(json.dumps(PACKAGES).encode(), "application/json")):
            return self.d.ts_search(*a)

    def test_maps_only_and_deprecated_hidden(self):
        got = self.search("")
        self.assertEqual([r["package"] for r in got["results"]], ["brassy-Industrial_Zone"])
        self.assertEqual(got["results"][0]["name"], "Industrial Zone")

    def test_all_packages_and_terms(self):
        self.assertEqual(self.search("", False)["total"], 2)
        self.assertEqual([r["package"] for r in self.search("industrial")["results"]], ["brassy-Industrial_Zone"])
        self.assertEqual(self.search("nothing")["total"], 0)

    def test_image_host_is_restricted(self):
        for url in ("http://gcdn.thunderstore.io/a.png", "https://evil.example/a.png", "https://gcdn.thunderstore.io:8443/a.png"):
            with self.assertRaises(ValueError):
                self.d.ts_image(url)
        with mock.patch.object(self.d, "ts_fetch", return_value=(b"x", "image/svg+xml")), self.assertRaises(ValueError):
            self.d.ts_image("https://gcdn.thunderstore.io/a.svg")

    def test_store_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            pack = pathlib.Path(tmp, "packages", "brassy-Industrial_Zone-1.0.2")
            pack.mkdir(parents=True)
            (pack / "reskate-levels.json").write_text(json.dumps({"levels": [{"displayName": "Industrial Zone"}]}))
            self.d.MODS_STORE = tmp
            self.assertEqual(self.d.store_names("brassy-Industrial_Zone"), ["Industrial Zone"])
            self.assertEqual(self.d.store_names("brassy-Other"), [])
            with self.assertRaises(ValueError):
                self.d.store_names("../etc")


if __name__ == "__main__":
    unittest.main()
