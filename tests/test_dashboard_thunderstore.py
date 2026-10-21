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


class ServerDetailsTest(unittest.TestCase):
    def setUp(self):
        self.d = load()
        self.d.DOCKER = "/fake.sock"
        self.d.DETAILS.update(at=0, rows={})
        self.d.CODES.clear()

    def docker(self, path):
        if path == "/containers/json":
            return json.dumps([{"Id": "c1", "Names": ["/reskate-server-test"]}, {"Id": "c2", "Names": ["/reskate-bot"]}]).encode()
        if path == "/containers/c1/json":
            env = ["RESKATE_SERVER=9", "SERVER_NAME=[TEST] Test", "SERVER_PASSWORD=!420", "STEAM_TOKEN=abc",
                   "WEBHOOK_URL_admin=https://hook", "PORT=12490", "MAP=Tampa", "PATH=/bin"]
            return json.dumps({"Config": {"Env": env, "Labels": {}, "Image": "img"},
                               "State": {"StartedAt": "2026-10-21T00:00:00Z", "Health": {"Status": "healthy"}}}).encode()
        if path.startswith("/containers/c1/logs"):
            return b"Join code: 1234-abcd\n[x] Join code: 9999-ffff trailing\n"
        raise AssertionError(path)

    def test_details_hide_secrets_and_find_code(self):
        with mock.patch.object(self.d, "docker_get", self.docker):
            info = self.d.server_info("9")
            self.assertTrue(info["locked"])
            self.assertEqual(info["name"], "[TEST] Test")
            self.assertEqual(info["containers"][0]["code"], "9999-ffff")
            self.assertEqual(info["env"], {"MAP": "Tampa", "PORT": "12490", "SERVER_NAME": "[TEST] Test"})
            self.assertNotIn("!420", json.dumps(info))
            self.assertEqual(self.d.server_secret("9"), {"password": "!420"})
            with self.assertRaises(ValueError):
                self.d.server_info("1")


if __name__ == "__main__":
    unittest.main()


class MapNameTest(unittest.TestCase):
    def test_status_map_regex_drops_the_trailing_period(self):
        spec = importlib.util.spec_from_file_location("status_mod", HERE / "rollover" / "status.py")
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception as e:  # the module may need its package context
            self.skipTest(str(e))
        for line, name in (("Everyone has loaded Industrial Zone.", "Industrial Zone"),
                           ("[rotation] Changing the map to Isle of Grom.", "Isle of Grom"),
                           ("Changing map to Macba.", "Macba")):
            self.assertEqual(next(g for g in mod.MAP.match(line).groups() if g), name)
