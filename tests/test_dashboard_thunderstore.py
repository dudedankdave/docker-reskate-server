import io
import importlib.util
import json
import os
import pathlib
import tarfile
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
        for url in ("http://gcdn.thunderstore.io/a.png", "https://evilthunderstore.io/a.png", "https://evil.example/a.png", "https://gcdn.thunderstore.io:8443/a.png"):
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
        if "/archive" in path:
            raise RuntimeError("docker 404")        # no settings file to read: the start environment counts
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

    def test_live_password_beats_the_start_environment(self):
        def docker(path):
            if "/archive" in path:
                buf = io.BytesIO()
                raw = json.dumps({"server": {"password": ""}}).encode()
                with tarfile.open(fileobj=buf, mode="w") as t:
                    info = tarfile.TarInfo("ReSkateServer.json")
                    info.size = len(raw)
                    t.addfile(info, io.BytesIO(raw))
                return buf.getvalue()
            return ServerDetailsTest.docker(self, path)
        with mock.patch.object(self.d, "docker_get", docker):
            self.assertFalse(self.d.server_info("9")["locked"])

    def test_off_means_no_password(self):
        def docker(path):
            raw = json.loads(ServerDetailsTest.docker(self, path)) if not path.startswith("/containers/c1/logs") else None
            if raw and "Config" in raw:
                raw["Config"]["Env"] = [e for e in raw["Config"]["Env"] if not e.startswith("SERVER_PASSWORD")] + ["SERVER_PASSWORD=off"]
                return json.dumps(raw).encode()
            return ServerDetailsTest.docker(self, path)
        with mock.patch.object(self.d, "docker_get", docker):
            self.assertFalse(self.d.server_info("9")["locked"])


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


class StatusLeftTest(unittest.TestCase):
    def test_leaving_while_downloading_the_map_counts_as_left(self):
        import re
        src = (HERE / "rollover" / "status.py").read_text()
        left = re.compile(eval(re.search(r"^LEFT = re\.compile\((r?\".*\")\)$", src, re.M).group(1)))
        self.assertTrue(left.match("[join] 76561199291693933 left without the map (Disconnected.)"))
        self.assertTrue(left.match("Bob left (Disconnected.)"))
        self.assertFalse(left.match("[map] 76561199291693933 is downloading the map"))


class LiveMapsTest(unittest.TestCase):
    def test_pool_and_mods_from_the_running_server(self):
        import io
        import tarfile
        d = load()

        def archive(path):
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as t:
                if path.endswith("ReSkateServer.json"):
                    raw = json.dumps({"maps": {"map": "Industrial Zone", "pool": ["Industrial Zone", "Pattonville"]}, "server": {"password": "x"}}).encode()
                    info = tarfile.TarInfo("ReSkateServer.json")
                    info.size = len(raw)
                    t.addfile(info, io.BytesIO(raw))
                else:
                    for name, target in (("A-One", "/mods/packages/A-One-1.0.0"), ("B-Two", "/mods/packages/B-Two-2.1.3")):
                        info = tarfile.TarInfo("Mods/" + name)
                        info.type = tarfile.SYMTYPE
                        info.linkname = target
                        t.addfile(info)
            return buf.getvalue()

        with mock.patch.object(d, "docker_get", lambda path: archive(path.split("path=")[1])):
            map_, pool, mods, flags = d.live_maps("c1")
            self.assertEqual((map_, pool, mods), ("Industrial Zone", ["Industrial Zone", "Pattonville"], ["A-One-1.0.0", "B-Two-2.1.3"]))
            self.assertEqual(flags, {"vote_map": False, "vote_kick": False, "vote_tod": False, "rotation": 0, "layer_sync": False, "layers_file": False, "layers_fit": "missing",
                                     "locked": True, "enforce_tuning": True, "score_check": "", "speed_check": "", "board_check": "", "ranked": False})


class DiskTest(unittest.TestCase):
    def test_disk_reports_free_space_of_the_store(self):
        d = load()
        with tempfile.TemporaryDirectory() as t, mock.patch.object(d, "MODS_STORE", t):
            k = d.disk()
        self.assertGreater(k["total"], 0)
        self.assertLessEqual(k["free"], k["total"])
        with mock.patch.object(d, "MODS_STORE", "/nonexistent-store"):
            self.assertIsNone(d.disk())


class BrandingTest(unittest.TestCase):
    def test_logo_and_favicon_are_valid_and_login_page_formats(self):
        d = load()
        self.assertTrue(d.LOGO_SVG.startswith("<svg"))
        self.assertTrue(d.FAVICON_PNG.startswith(b"\x89PNG"))
        self.assertIn("/logo.svg", d.LOGIN % "")


class CreatedSortTest(unittest.TestCase):
    def test_sort_by_created_newest_first(self):
        d = load()
        rows = [{"package": "a-A", "name": "A", "description": "", "categories": ["maps"], "downloads": 5, "rating": 1,
                 "updated": "2026-01-01", "created": "2025-01-01"},
                {"package": "b-B", "name": "B", "description": "", "categories": ["maps"], "downloads": 1, "rating": 0,
                 "updated": "2026-02-01", "created": "2026-02-01"}]
        with mock.patch.object(d, "ts_packages", return_value=rows):
            self.assertEqual([r["package"] for r in d.ts_search("", sort="created")["results"]], ["b-B", "a-A"])
            self.assertEqual([r["package"] for r in d.ts_search("", sort="created", asc=True)["results"]], ["a-A", "b-B"])


class ImageHostTest(unittest.TestCase):
    def test_every_thunderstore_cdn_is_allowed(self):
        d = load()
        with mock.patch.object(d, "ts_fetch", return_value=(b"x", "image/png")):
            for host in ("gcdn.thunderstore.io", "ccdn.thunderstore.io", "thunderstore.io"):
                self.assertEqual(d.ts_image(f"https://{host}/a.png")[0], "image/png")


class UploadLayersTest(unittest.TestCase):
    def test_validates_and_writes_to_the_running_containers(self):
        d = load()
        rows = {"4": {"containers": [{"id": "c4"}]}, "5": {"containers": [{"id": "c5a"}, {"id": "c5b"}]}}
        sent = []
        with mock.patch.object(d, "server_details", return_value=rows), \
                mock.patch.object(d, "docker_put_file", lambda cid, folder, name, data: sent.append((cid, folder, name))):
            d.upload_layers("4", '{"layers": []}')
            self.assertEqual(sent, [("c4", "/data", "world-layers.json"), ("c4", "/data", "world-layers.version")])
            sent.clear()
            d.upload_layers("*", "[]")
            self.assertEqual(len(sent), 6)
            for bad in ("nope", "3", '"text"'):
                with self.assertRaises(ValueError):
                    d.upload_layers("4", bad)
            with self.assertRaises(ValueError):
                d.upload_layers("9", "{}")


class LayersLoadedTest(unittest.TestCase):
    def _log(self, text):
        def tar(cid, path):
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as t:
                info = tarfile.TarInfo("ReSkateServer.log")
                info.size = len(text.encode())
                t.addfile(info, io.BytesIO(text.encode()))
            buf.seek(0)
            return tarfile.open(fileobj=buf)
        return tar

    def test_a_fresh_start_is_not_remembered_as_failed(self):
        import datetime
        d = load()
        d.LAYERS_SEEN.clear()
        fmt = "%Y-%m-%dT%H:%M:%S.0Z"
        now = datetime.datetime.now(datetime.timezone.utc)
        fresh = now.strftime(fmt)
        with mock.patch.object(d, "tar_of", self._log("[2000-01-01 00:00:00] booting\n")):
            self.assertIsNone(d.layers_loaded("c", fresh))
        loaded = "[%s] World layers: 261 from world-layers.json.\n" % now.strftime("%Y-%m-%d %H:%M:%S")
        with mock.patch.object(d, "tar_of", self._log(loaded)):
            self.assertTrue(d.layers_loaded("c", fresh))
        old = (now - datetime.timedelta(hours=1)).strftime(fmt)
        with mock.patch.object(d, "tar_of", self._log("[2000-01-01 00:00:00] booting\n")):
            self.assertFalse(d.layers_loaded("c2", old))


class PersistPoolTest(unittest.TestCase):
    def setUp(self):
        self.d = load()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d.SHARED = self.tmp.name

    def saved(self):
        return self.d.env_read("1")

    def test_pool_and_a_start_map_in_it_are_saved_for_the_next_start(self):
        lib = ["A", "B", "C"]
        rows = {"1": {"env": {"MAP": "C"}, "containers": []}}
        live = {"1": {"live": True, "pool": ["A", "B", "C"]}}
        with mock.patch.object(self.d, "server_details", return_value=rows), \
                mock.patch.object(self.d, "server_maps", return_value=live):
            self.d.persist_pool("1", ["A", "B"], lib)
            self.assertEqual(self.saved(), {"MAP_POOL": "A,B", "MAP": "A"})
            self.d.persist_pool("1", ["B", "C"], lib)          # the saved start map A is outside again
            self.assertEqual(self.saved(), {"MAP_POOL": "B,C", "MAP": "B"})
            self.d.persist_pool("1", ["B", "C"], lib)          # nothing to change when it is inside
            self.assertEqual(self.saved(), {"MAP_POOL": "B,C", "MAP": "B"})
            self.d.persist_pool("1", [], lib)                  # all maps: no start map needed
            self.assertEqual(self.saved(), {"MAP_POOL": "off"})
            live["1"]["pool"] = ["A", "B,x"]
            self.d.persist_pool("1", ["A", "B,x"], lib)        # a comma cannot be carried in the list
            self.assertEqual(self.saved(), {"MAP_POOL": "off"})
            live["1"]["pool"] = ["A", "C"]                     # the server refused B: it is not saved
            self.d.persist_pool("1", ["A", "B", "C"], lib + ["D"])
            self.assertEqual(self.saved(), {"MAP_POOL": "A,C"})   # the container start map C is in it


class LayersFitTest(unittest.TestCase):
    def test_fit_compares_the_recorded_version(self):
        d = load()
        for recorded, want in (("2.1.1", "ok"), ("2.0.9", "old"), (None, "unknown")):
            def tar(cid, path, recorded=recorded):
                if recorded is None:
                    raise RuntimeError("missing")
                buf = io.BytesIO()
                with tarfile.open(fileobj=buf, mode="w") as t:
                    info = tarfile.TarInfo("world-layers.version")
                    info.size = len(recorded)
                    t.addfile(info, io.BytesIO(recorded.encode()))
                buf.seek(0)
                return tarfile.open(fileobj=buf)
            with mock.patch.object(d, "tar_of", tar):
                self.assertEqual(d.layers_fit("c", "2.1.1"), want)


class PoolsTest(unittest.TestCase):
    def setUp(self):
        self.d = load()
        self.tmp = tempfile.TemporaryDirectory()
        self.d.POOLS_FILE = self.tmp.name + "/pools.json"
        self.live = {"1": [], "2": ["B"]}
        self.sent = []
        lib = ["A", "B", "C"]
        maps = lambda *a: {n: {"pool": list(p), "available": lib, "live": True} for n, p in self.live.items()}  # noqa: E731
        self.patches = [mock.patch.object(self.d, "server_maps", maps),
                        mock.patch.object(self.d, "server_details", lambda: {"1": {"name": "x - Main"}, "2": {"name": "x - Skate 2"}}),
                        mock.patch.object(self.d, "moderate", lambda body: self.sent.append((body["server"], body["text"]))),
                        mock.patch.object(self.d, "sync_links", lambda numbers=None, apply=True: self.synced.append(numbers))]
        self.synced = []
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_a_pool_change_links_missing_packages_first(self):
        self.d.pools_action({"action": "assign", "server": "1", "name": "Skate 2"})
        self.d.pools_action({"action": "sync"})
        self.assertEqual(self.synced, [{"1"}, None])

    def test_seed_names_pools_after_the_role_and_all_maps_stays_builtin(self):
        snap = self.d.pools_action({"action": "reapply", "server": "1"})
        self.assertEqual(snap["pools"], {"Skate 2": ["B"]})
        self.assertEqual(snap["assign"], {"1": "Alle Maps", "2": "Skate 2"})
        self.assertEqual(self.sent, [])

    def test_assign_set_and_delete_apply_console_commands(self):
        d = self.d
        d.pools_action({"action": "create", "server": "1", "name": "Mine"})
        self.assertEqual(self.sent, [])   # a copy of "all maps" is still all maps; the live pool was empty already
        d.pools_action({"action": "set", "server": "1", "name": "Mine", "map": "C", "on": False})
        self.assertEqual(self.sent, [("1", "map-pool remove C")])   # empty live pool: removal materializes all-but-C
        self.live["1"] = ["A", "B"]
        self.sent.clear()
        d.pools_action({"action": "set", "server": "1", "name": "Mine", "map": "C", "on": True})
        self.assertEqual(self.sent, [("1", "map-pool clear")])
        self.sent.clear()
        d.pools_action({"action": "assign", "server": "1", "name": "Skate 2"})
        self.assertEqual(self.sent, [("1", "map-pool remove A")])
        with self.assertRaises(ValueError):
            d.pools_action({"action": "delete", "server": "1", "name": "Skate 2"})
        d.pools_action({"action": "delete", "server": "1", "name": "Mine"})
        with self.assertRaises(ValueError):
            d.pools_action({"action": "set", "server": "1", "name": "Skate 2", "map": "B", "on": False})


class AnnouncementsTest(unittest.TestCase):
    def setUp(self):
        self.d = load()
        self.tmp = tempfile.TemporaryDirectory()
        self.d.ANN_FILE = self.tmp.name + "/ansagen.json"
        self.live = {"1": (["Join", "One"], 10), "2": (["Join"], 60), "9": ([], 5)}
        self.sent = []
        names = {"1": "x - Main", "2": "x - Skate 2", "9": "x - Test"}
        self.patches = [mock.patch.object(self.d, "live_announcements", lambda n: self.live.get(n)),
                        mock.patch.object(self.d, "server_details", lambda: {n: {"name": v} for n, v in names.items()}),
                        mock.patch.object(self.d, "moderate", lambda body: self.sent.append((body["server"], body["text"])))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_seed_moves_shared_lines_to_global_and_ignores_the_test_server(self):
        out = self.d.ann_action({"action": "reapply", "scope": "1"})
        self.assertEqual([m["text"] for m in out["global"]], ["Join"])
        self.assertEqual([m["text"] for m in out["servers"]["1"]["messages"]], ["One"])
        self.assertEqual(out["servers"]["1"]["interval"], 10)
        self.assertEqual(self.sent, [])   # nothing differs from what runs

    def test_edits_rewrite_the_list_and_interval_through_the_console(self):
        d = self.d
        d.ann_action({"action": "reapply", "scope": "1"})
        d.ann_action({"action": "add", "scope": "2", "text": "  New   line "})
        self.assertEqual(self.sent, [("2", "announcements clear"), ("2", "announcements add Join"), ("2", "announcements add New line")])
        self.sent.clear()
        d.ann_action({"action": "interval", "server": "1", "minutes": 20})
        self.assertEqual(self.sent, [("1", "announcements interval 20")])
        for bad in ({"action": "add", "scope": "1", "text": " "}, {"action": "edit", "scope": "1", "index": 9},
                    {"action": "interval", "server": "1", "minutes": 0}, {"action": "add", "scope": "7", "text": "x"}):
            with self.assertRaises(ValueError):
                d.ann_action(bad)


class AnnouncementScheduleTest(unittest.TestCase):
    def test_one_common_interval_is_a_plain_rotation(self):
        d = load()
        self.assertEqual(d.ann_schedule([("a", 10, 0), ("b", 10, 5)]), {"messages": ["a", "b"], "interval": 10, "deviation": 0})

    def test_different_intervals_get_slots_of_their_own(self):
        d = load()
        out = d.ann_schedule([("a", 10, 0), ("b", 10, 0), ("c", 20, 0)])
        self.assertEqual((out["interval"], out["deviation"]), (4, 0))
        slots = out["messages"]
        self.assertEqual((len(slots), slots.count("a"), slots.count("b"), slots.count("c")), (5, 2, 2, 1))
        out = d.ann_schedule([("a", 10, 0), ("b", 60, 0)])
        self.assertEqual((len(out["messages"]), out["messages"].count("b")), (7, 1))
        self.assertLess(out["deviation"], 0.1)

    def test_offset_moves_the_first_slot(self):
        d = load()
        out = d.ann_schedule([("a", 10, 0), ("b", 30, 20)])
        self.assertEqual(out["messages"].index("b"), 2)
        with self.assertRaises(ValueError):
            d.ann_schedule([("a", 97, 0), ("b", 89, 0)])


class StorePackageTest(unittest.TestCase):
    def zipped(self, files):
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for name, data in files.items():
                z.writestr(name, data)
        return buf.getvalue()

    def fake(self, data):
        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return lambda req, timeout=0: Resp(data)

    def test_unpacks_into_the_store_once_and_refuses_unsafe_zips(self):
        d = load()
        with tempfile.TemporaryDirectory() as tmp:
            d.SHARED = tmp
            good = self.zipped({"manifest.json": "{}", "map/level.bundle": "x"})
            with mock.patch.object(d.urllib.request, "urlopen", self.fake(good)):
                folder = d.store_package("Own", "Map", "1.0.0")
            self.assertTrue(os.path.exists(folder + "/map/level.bundle") and os.path.exists(folder + "/.thunderstore.json"))
            with mock.patch.object(d.urllib.request, "urlopen", side_effect=AssertionError("downloaded twice")):
                self.assertEqual(d.store_package("Own", "Map", "1.0.0"), folder)
            for bad in ({"../evil": "x", "manifest.json": "{}"}, {"a": "x"}):
                with mock.patch.object(d.urllib.request, "urlopen", self.fake(self.zipped(bad))), self.assertRaises(ValueError):
                    d.store_package("Own", "Bad", "1.0.0")
            self.assertFalse(os.path.exists(tmp + "/packages/Own-Bad-1.0.0"))


class ServerMapsOnlyTest(unittest.TestCase):
    def test_refreshing_one_server_keeps_the_others(self):
        d = load()
        rows = {"1": {"env": {}, "containers": []}, "2": {"env": {"MAP": "B"}, "containers": []}}
        d.MAPS.update(at=0, rows={"1": {"map": "keep", "pool": [], "mods": [], "live": False, "available": []}})
        with mock.patch.object(d, "server_details", return_value=rows):
            out = d.server_maps(True, "2")
        self.assertEqual(out["1"]["map"], "keep")
        self.assertEqual(out["2"]["map"], "B")


class OnlineCountTest(unittest.TestCase):
    def test_online_counts_joined_players_not_the_status_count(self):
        d = load()
        rows = [{"server": "5", "running": True, "players": 1, "slots": {"a": {"running": True, "players": 1}}}]
        joined = [{"name": "X", "slot": "B"}, {"name": "Y", "slot": "a"}]

        def fake(path):
            return rows if path == "/v1/servers" else joined

        with mock.patch.object(d, "bridge", fake), mock.patch.object(d, "server_details", return_value={}):
            out = d.servers()["servers"][0]
        self.assertEqual(out["online"], 2)
        self.assertEqual(out["slots"]["a"]["online"], 1)
        self.assertEqual(out["players"], 1)


class LivePlayersTest(unittest.TestCase):
    LOG = ("[20:00:00] [24-7] - X is up on Map for 100 players.\n"
           "[20:01:00] Old joined (76561198000000009, player), 1/100 players, loaded in 5 s\n"
           "[20:02:00] Ann joined (76561198000000001, admin), 2/100 players, loaded in 5 s\n"
           "[20:03:00] Ben joined (76561198000000002, player), 3/100 players, loaded in 5 s\n"
           "[20:04:00] Cy joined (76561198000000003), 4/100 players, loaded in 5 s\n"
           "[20:05:00] Old left (Disconnected.) [closed by their game, Steam reason 1000]\n"
           "[20:05:30] [join] 76561198000000044 left without the map (Disconnected.)\n")

    def test_players_come_from_the_log_whatever_their_role_and_the_bridge_misses_them(self):
        d = load()
        d.DOCKER = "/x"
        d.LIVE.clear()

        def docker(path):
            if path == "/containers/json":
                return json.dumps([{"Id": "c1", "Labels": {"reskate.server": "1", "reskate.slot": "a"}}]).encode()
            if path.endswith("/json"):
                return json.dumps({"State": {"StartedAt": "2026-10-21T19:47:00.123Z"}}).encode()
            return self.LOG.encode()

        def bridge(path, body=None):
            return [{"name": "Ann", "steam_id": "76561198000000001", "slot": "a", "team": "CRW"}]
        with mock.patch.object(d, "docker_get", docker), mock.patch.object(d, "bridge", bridge):
            out = d.server_players("1")
        self.assertEqual([(p["name"], p["team"]) for p in out], [("Ann", "CRW"), ("Ben", ""), ("Cy", "")])


class LeaderboardTest(unittest.TestCase):
    def setUp(self):
        import sqlite3
        self.d = load()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d.SHARED = self.tmp.name
        con = sqlite3.connect(f"{self.tmp.name}/leaderboard-unranked.db")
        con.executescript(
            "CREATE TABLE boards (id TEXT PRIMARY KEY, name TEXT, kind TEXT, created_at REAL, closed_at REAL);"
            "CREATE TABLE players (steam_id TEXT PRIMARY KEY, name TEXT);"
            "CREATE TABLE scores (board TEXT, steam_id TEXT, points INT, wins INT, played INT);"
            "CREATE TABLE ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL, board TEXT, steam_id TEXT, points INT, reason TEXT, server TEXT);"
            "INSERT INTO boards VALUES ('general','All Time','general',1,NULL),('week-2026-43','Week 43','week',2,NULL),"
            "('week-2026-43:jam','Week 43 - Jam','mode',2,NULL);"
            "INSERT INTO players VALUES ('1','Ann'),('2','Bo'),('3','Cy');"
            "INSERT INTO scores VALUES ('general','1',50,1,3),('general','2',80,2,4),('general','3',0,0,1),('week-2026-43:jam','1',9,0,1);"
            "INSERT INTO ledger (at,board,steam_id,points,reason,server) VALUES (5,'general','2',30,'jam win','2'),(5,'week-2026-43','2',30,'jam win','2'),(5,'general:jam','2',30,'jam win','2');")
        con.commit()
        con.close()
        con = sqlite3.connect(f"{self.tmp.name}/teams.db")
        con.executescript("CREATE TABLE teams (id INTEGER PRIMARY KEY, name TEXT, tag TEXT);"
                          "CREATE TABLE members (steam_id TEXT PRIMARY KEY, team INT, role TEXT, joined REAL);"
                          "INSERT INTO teams VALUES (1,'Crew','CRW');INSERT INTO members VALUES ('1',1,'member',0),('2',1,'member',0);")
        con.commit()
        con.close()

    def test_ranks_teams_and_boards(self):
        out = self.d.lb_snapshot("unranked")
        self.assertEqual([r["name"] for r in out["rows"]], ["Bo", "Ann"])
        self.assertEqual([r["rank"] for r in out["rows"]], [1, 2])
        self.assertEqual(out["rows"][0]["tag"], "CRW")
        self.assertEqual(out["teams"], [{"tag": "CRW", "name": "Crew", "points": 130, "wins": 3, "members": 2}])
        self.assertEqual([b["id"] for b in out["boards"]], ["general", "week-2026-43", "week-2026-43:jam"])
        self.assertEqual(out["boards"][2]["kind"], "week")
        self.assertEqual(out["recent"][0]["name"], "Bo")
        self.assertEqual(len(out["recent"]), 1)          # one award counted on three boards shows once
        self.assertEqual(out["recent"][0]["boards"], 3)
        self.assertEqual(out["stats"]["awards"], 1)

    def test_mode_board_and_unknown_board(self):
        self.assertEqual([r["name"] for r in self.d.lb_snapshot("unranked", "week-2026-43:jam")["rows"]], ["Ann"])
        self.assertEqual(self.d.lb_snapshot("unranked", "nope")["board"], "general")

    def test_missing_league_and_bad_league(self):
        out = self.d.lb_snapshot("ranked")
        self.assertFalse(out["exists"])
        self.assertEqual(out["rows"], [])
        with self.assertRaises(ValueError):
            self.d.lb_snapshot("../x")


class RestartTest(unittest.TestCase):
    def setUp(self):
        self.d = load()
        self.restarted = []
        self.d.DETAILS.update(at=0, rows={})
        self.rows = {"1": {"containers": [{"name": "reskate-server-1", "slot": "A", "id": "cid1"}]}}
        self.status = [{"server": 1, "players": 2, "slots": {"A": {"players": 2}}}]
        self.d.server_details = lambda: self.rows
        self.d.bridge = lambda path, body=None: self.status
        self.d.docker_restart = lambda cid, grace=20: self.restarted.append(cid)

    def test_restarts_within_the_rule(self):
        out = self.d.restart_server({"server": "1"})
        self.assertEqual((out["forced"], out["players"], self.restarted), (False, 2, ["cid1"]))

    def test_busy_server_needs_force_and_the_current_count(self):
        self.status[0]["slots"]["A"]["players"] = 9
        with self.assertRaises(ValueError):
            self.d.restart_server({"server": "1"})
        with self.assertRaises(ValueError):
            self.d.restart_server({"server": "1", "force": True, "players": 7})
        self.assertEqual(self.restarted, [])
        out = self.d.restart_server({"server": "1", "force": True, "players": 9})
        self.assertTrue(out["forced"])
        self.assertEqual(self.restarted, ["cid1"])

    def test_unknown_count_counts_as_busy(self):
        self.status[0]["slots"]["A"]["players"] = None
        with self.assertRaises(ValueError):
            self.d.restart_server({"server": "1"})
        self.d.restart_server({"server": "1", "force": True, "players": None})
        self.assertEqual(self.restarted, ["cid1"])

    def test_unknown_server_and_ambiguous_slots(self):
        with self.assertRaises(ValueError):
            self.d.restart_server({"server": "7"})
        self.rows["1"]["containers"].append({"name": "reskate-server-1b", "slot": "B", "id": "cid2"})
        with self.assertRaises(ValueError):
            self.d.restart_server({"server": "1"})
        self.d.restart_server({"server": "1", "slot": "B"})
        self.assertEqual(self.restarted, ["cid2"])


class ServerOptionTest(unittest.TestCase):
    def setUp(self):
        self.d = load()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d.PW_FILE = f"{self.tmp.name}/passwords.json"
        self.d.SHARED = self.tmp.name
        self.sent = []
        self.config = {"server": {"password": ""}}
        self.rows = {"1": {"password": "", "containers": [{"name": "s1", "slot": "a", "id": "c1"}]}}
        self.d.server_details = lambda: self.rows
        self.d.live_config = lambda cid: self.config
        self.d.moderate = lambda body: self.sent.append(body["text"])

    def test_set_unlock_lock_roundtrip(self):
        self.d.server_option({"server": "1", "action": "password", "value": "  secret word "})
        self.assertEqual(self.sent, ["password secret word"])
        self.config["server"]["password"] = "secret word"
        self.d.server_option({"server": "1", "action": "unlock"})
        self.assertEqual(self.sent[-1], "password off")
        self.config["server"]["password"] = ""
        self.d.server_option({"server": "1", "action": "lock"})
        self.assertEqual(self.sent[-1], "password secret word")
        self.assertEqual(oct(os.stat(self.d.PW_FILE).st_mode & 0o777), "0o600")

    def test_lock_without_any_password_and_bad_passwords(self):
        with self.assertRaises(ValueError):
            self.d.server_option({"server": "1", "action": "lock"})
        for word in ("", "off", "None", "x" * 65, "a\nb"):
            with self.assertRaises(ValueError):
                self.d.server_option({"server": "1", "action": "password", "value": word})
        self.assertEqual(self.sent, [])

    def test_lock_falls_back_to_the_start_password(self):
        self.rows["1"]["password"] = "fromenv"
        self.d.server_option({"server": "1", "action": "lock"})
        self.assertEqual(self.sent, ["password fromenv"])

    def test_ranked_on_and_off(self):
        self.d.server_option({"server": "1", "action": "ranked", "on": True})
        self.assertEqual(self.sent, ["tuning on", "score-check kick", "speed-check kick"])
        self.sent.clear()
        self.d.server_option({"server": "1", "action": "ranked", "on": False})
        self.assertEqual(self.sent, ["score-check warn", "speed-check warn"])

    def test_single_checks_and_tuning(self):
        self.d.server_option({"server": "1", "action": "tuning", "on": False})
        self.d.server_option({"server": "1", "action": "score", "value": "kick"})
        self.d.server_option({"server": "1", "action": "speed", "value": "off"})
        self.d.server_option({"server": "1", "action": "board", "value": "warn"})
        self.assertEqual(self.sent, ["tuning off", "score-check kick", "speed-check off", "board-check warn"])
        with self.assertRaises(ValueError):
            self.d.server_option({"server": "1", "action": "score", "value": "ban"})

    def test_unknown_server_action_and_slots(self):
        with self.assertRaises(ValueError):
            self.d.server_option({"server": "7", "action": "lock"})
        with self.assertRaises(ValueError):
            self.d.server_option({"server": "1", "action": "boom"})
        self.rows["1"]["containers"].append({"name": "s1b", "slot": "b", "id": "c2"})
        with self.assertRaises(ValueError):
            self.d.server_option({"server": "1", "action": "unlock"})

    def test_persist_writes_settings_files_that_the_server_reads_at_start(self):
        self.d.server_option({"server": "1", "action": "score", "value": "kick", "persist": True})
        self.d.server_option({"server": "1", "action": "tuning", "on": False, "persist": True})
        self.d.server_option({"server": "1", "action": "score", "value": "warn", "persist": True})
        text = pathlib.Path(self.tmp.name, "settings", "server-1.env").read_text(encoding="utf-8")
        self.assertEqual(text.count("SCORE_CHECK="), 1)
        self.assertEqual(text.count("# set by the dashboard"), 1)
        self.assertIn("SCORE_CHECK=warn\n", text)
        self.assertIn("ENFORCE_TUNING=false\n", text)
        out = self.d.server_option({"server": "1", "action": "password", "value": 'a "b" # c', "persist": True})
        self.assertEqual(out["saved"], ["SERVER_PASSWORD"])
        secret = f"{self.tmp.name}/settings/secrets/server-1.env"
        self.assertIn('SERVER_PASSWORD="a \\"b\\" # c"', pathlib.Path(secret).read_text(encoding="utf-8"))
        self.assertEqual(oct(os.stat(secret).st_mode & 0o777), "0o600")
        self.assertEqual(self.d.env_read("1"), {"SCORE_CHECK": "warn", "ENFORCE_TUNING": "false", "SERVER_PASSWORD": "set"})

    def test_without_persist_nothing_is_written_and_forget_drops_it(self):
        self.d.server_option({"server": "1", "action": "speed", "value": "kick"})
        self.assertFalse(os.path.exists(f"{self.tmp.name}/settings"))
        self.d.server_option({"server": "1", "action": "league", "value": "unranked", "persist": True})
        self.assertEqual(self.d.env_read("1"), {"LEADERBOARD_LEAGUE": "unranked"})
        self.d.server_option({"server": "1", "action": "speed", "value": "kick", "persist": True})
        self.d.server_option({"server": "1", "action": "forget"})
        self.assertEqual(self.d.env_read("1"), {})
        self.assertFalse(os.path.exists(f"{self.tmp.name}/settings/server-1.env"))     # nothing else was in it
        with self.assertRaises(ValueError):
            self.d.server_option({"server": "1", "action": "league", "value": "ranked"})

    def test_other_lines_in_the_file_stay(self):
        os.makedirs(f"{self.tmp.name}/settings")
        path = f"{self.tmp.name}/settings/server-1.env"
        pathlib.Path(path).write_text("# mine\nMAX_PLAYERS=30\nSPEED_CHECK=off\n")
        self.d.server_option({"server": "1", "action": "speed", "value": "kick", "persist": True})
        self.assertEqual(pathlib.Path(path).read_text(), "# mine\nMAX_PLAYERS=30\nSPEED_CHECK=kick\n")
        self.d.server_option({"server": "1", "action": "forget"})
        self.assertEqual(pathlib.Path(path).read_text(), "# mine\nMAX_PLAYERS=30\n")


class SyncLinksTest(unittest.TestCase):
    def setUp(self):
        self.d = load()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d.MODS_STORE = self.tmp.name

    def package(self, folder, *names, levels=True):
        path = pathlib.Path(self.tmp.name, "packages", folder)
        path.mkdir(parents=True)
        if levels:
            (path / "reskate-levels.json").write_text(json.dumps({"levels": [{"displayName": n} for n in names]}))

    def test_store_maps_takes_the_newest_version_with_maps(self):
        self.package("a-One-1.0.0", "One")
        self.package("a-One-1.10.0", "One")
        self.package("a-One-1.9.0", "One")
        self.package("b-Deck-1.0.0", levels=False)
        self.assertEqual(self.d.store_maps(), {"a-One": {"folder": "a-One-1.10.0", "names": {"one"}}})

    def test_plan_links_what_is_missing_and_keeps_name_clashes_out(self):
        store = {"a-One": {"folder": "a-One-1.0.0", "names": {"one"}},
                 "b-Two": {"folder": "b-Two-1.0.0", "names": {"two"}},
                 "builders-Olay": {"folder": "builders-Olay-1.0.0", "names": {"olay way"}},
                 "snuf-Olay": {"folder": "snuf-Olay-1.0.0", "names": {"olay way"}}}
        linked = {"1": ["a-One-1.0.0", "snuf-Olay-1.0.0"], "2": ["a-One-1.0.0"], "3": []}
        plan = self.d.sync_plan(store, linked)
        self.assertEqual(plan["1"], ["b-Two"])
        self.assertEqual(plan["2"], ["snuf-Olay", "b-Two"])           # the Olay Way more servers have wins
        self.assertEqual(plan["3"], ["a-One", "snuf-Olay", "b-Two"])
        self.assertNotIn("builders-Olay", sum(plan.values(), []))

    def test_nothing_missing_nothing_planned(self):
        store = {"a-One": {"folder": "a-One-1.0.0", "names": {"one"}}}
        self.assertEqual(self.d.sync_plan(store, {"1": ["a-One-1.0.0"]}), {})


class PlayerTest(unittest.TestCase):
    ANN, BO, NEW = "76561198000000001", "76561198000000002", "76561198000000005"

    def setUp(self):
        import sqlite3
        self.d = load()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d.SHARED = self.tmp.name
        con = sqlite3.connect(f"{self.tmp.name}/teams.db")
        con.executescript(
            "CREATE TABLE teams (id INTEGER PRIMARY KEY, name TEXT, tag TEXT, motto TEXT DEFAULT '', description TEXT DEFAULT '',"
            " logo TEXT DEFAULT '', league TEXT DEFAULT '', category TEXT DEFAULT '', open INTEGER DEFAULT 0, created REAL DEFAULT 0);"
            "CREATE TABLE members (steam_id TEXT PRIMARY KEY, team INT, role TEXT, joined REAL);"
            "CREATE TABLE known (steam_id TEXT PRIMARY KEY, name TEXT, seen REAL);"
            "CREATE TABLE invites (team INT, steam_id TEXT, at REAL);"
            "CREATE TABLE applications (id INTEGER PRIMARY KEY, steam_id TEXT, applicant TEXT, name TEXT, tag TEXT, status TEXT, created REAL,"
            " decided_by TEXT, decided_at REAL, message TEXT DEFAULT '');"
            "CREATE TABLE links (steam_id TEXT, discord_id TEXT, at REAL);"
            "INSERT INTO teams (id, name, tag) VALUES (1, 'Crew', 'crw'), (2, 'Other', 'oth');"
            f"INSERT INTO members VALUES ('{self.ANN}', 1, 'member', 2), ('{self.BO}', 1, 'owner', 1);"
            f"INSERT INTO known VALUES ('{self.ANN}', 'Ann', 0), ('{self.BO}', 'Bo', 0), ('{self.NEW}', 'Newbie', 0);"
            f"INSERT INTO links VALUES ('{self.ANN}', '123456789012345678', 0);")
        con.commit()
        con.close()
        con = sqlite3.connect(f"{self.tmp.name}/leaderboard-unranked.db")
        con.executescript(
            "CREATE TABLE boards (id TEXT, name TEXT, kind TEXT, created_at REAL, closed_at REAL);"
            "CREATE TABLE players (steam_id TEXT, name TEXT);"
            "CREATE TABLE scores (board TEXT, steam_id TEXT, points INT, wins INT, played INT);"
            "INSERT INTO boards VALUES ('general', 'All Time', 'general', 0, NULL), ('general:skate', 'All Time - S.K.A.T.E.', 'mode', 0, NULL),"
            " ('week-2026-42', 'Week 42', 'week', 0, NULL), ('week-2026-43', 'Week 43', 'week', 1, NULL);"
            "INSERT INTO players VALUES ('name:Ann', 'Ann'), ('name:Zed', 'Zed');"
            "INSERT INTO scores VALUES ('general', 'name:Ann', 12, 2, 5), ('general', 'name:Zed', 40, 4, 6), ('general:skate', 'name:Ann', 7, 1, 2),"
            " ('week-2026-43', 'name:Ann', 3, 0, 1), ('week-2026-42', 'name:Ann', 99, 9, 9);")
        con.commit()
        con.close()
        self.calls = []

        def bridge(path, body=None):
            self.calls.append(body)
            return {"ok": True, "lines": ["Done."]}
        self.d.bridge = bridge

    def test_profile_links_team_and_stats_per_mode_and_period(self):
        out = self.d.player_profile(self.ANN)
        self.assertEqual(out["steam"], f"https://steamcommunity.com/profiles/{self.ANN}")
        self.assertEqual(out["discord"], "https://discord.com/users/123456789012345678")
        self.assertEqual((out["name"], out["team"]["tag"]), ("Ann", "CRW"))
        self.assertEqual([t["tag"] for t in out["teams"]], ["CRW", "OTH"])
        cells = out["stats"]["unranked"]["cells"]
        self.assertEqual(cells[""]["general"], {"points": 12, "wins": 2, "played": 5, "rank": 2, "of": 2})   # Zed leads
        self.assertEqual(cells["skate"]["general"]["points"], 7)
        self.assertEqual(cells[""]["week"]["points"], 3)                  # the latest week, not week 42
        self.assertEqual(out["stats"]["ranked"]["cells"], {})
        self.assertEqual(self.d.player_profile(self.BO)["discord"], "")

    def test_steam_status_from_the_public_profile(self):
        xml = (b"<profile><steamID64>76561198000000001</steamID64><steamID><![CDATA[Ann]]></steamID><privacyState>public</privacyState>"
               b"<avatarMedium><![CDATA[https://avatars.fastly.steamstatic.com/abc_medium.jpg]]></avatarMedium><vacBanned>1</vacBanned>"
               b"<tradeBanState>None</tradeBanState><isLimitedAccount>0</isLimitedAccount><memberSince>June 24, 2012</memberSince></profile>")

        class Res:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n=-1):
                return xml
        calls = []
        with mock.patch.object(self.d.urllib.request, "urlopen", lambda *a, **k: calls.append(a) or Res()):
            info = self.d.steam_info(self.ANN)
            self.d.steam_info(self.ANN)
        self.assertEqual((info["name"], info["vac"], info["limited"], info["since"]), ("Ann", True, False, "June 24, 2012"))
        self.assertTrue(info["avatar"].startswith("https://avatars."))
        self.assertEqual(len(calls), 1)                                  # cached

    def test_steam_unreachable_is_a_message_not_a_crash(self):
        def down(*a, **k):
            raise OSError("no route")
        with mock.patch.object(self.d.urllib.request, "urlopen", down):
            self.assertIn("not reachable", self.d.steam_info("76561198000000077")["error"])

    def test_bad_steam_id_is_refused(self):
        with self.assertRaises(ValueError):
            self.d.player_profile("12")

    def test_known_players_include_offline_ones_with_team_and_last_seen(self):
        import sqlite3
        con = sqlite3.connect(f"{self.tmp.name}/teams.db")
        con.execute(f"UPDATE known SET seen = 50 WHERE steam_id = '{self.NEW}'")
        con.commit()
        con.close()
        con = sqlite3.connect(f"{self.tmp.name}/leaderboard.db")
        con.executescript("CREATE TABLE players (steam_id TEXT, name TEXT);"
                          "INSERT INTO players VALUES ('76561198000000077', 'Boardonly'), ('name:Nobody', 'Nobody');")
        con.commit()
        con.close()
        out = self.d.players_known()
        self.assertEqual([p["name"] for p in out], ["Newbie", "Ann", "Bo", "Boardonly", "Zed"])      # newest seen first
        self.assertEqual({p["name"]: p["team"] for p in out}, {"Newbie": "", "Ann": "CRW", "Bo": "CRW", "Boardonly": "", "Zed": ""})
        self.assertEqual([p["steam_id"] for p in out if p["name"] == "Zed"], [""])             # name-only: no Steam ID on file
        zed = self.d.player_profile("", "Zed")
        self.assertEqual((zed["team"], zed["stats"]["unranked"]["cells"][""]["general"]["points"]), (None, 40))

    def test_assign_invites_as_the_owner_then_joins(self):
        self.d.player_team({"steam_id": self.NEW, "team": "crw"})
        self.assertEqual([(c["verb"], c["who"]["steam_id"]) for c in self.calls], [("invite", self.BO), ("join", self.NEW)])
        self.assertEqual(self.calls[1]["args"], {"team": "CRW"})

    def test_assign_leaves_the_old_team_first_and_none_removes(self):
        self.d.player_team({"steam_id": self.ANN, "team": ""})
        self.assertEqual([c["verb"] for c in self.calls], ["leave"])
        self.calls.clear()
        self.assertEqual(self.d.player_team({"steam_id": self.ANN, "team": "CRW"}), "already in that team")
        self.assertEqual(self.calls, [])
        with self.assertRaises(ValueError):
            self.d.player_team({"steam_id": self.NEW, "team": "oth"})        # no owner to invite

    def test_a_refusal_of_the_teams_module_is_reported(self):
        self.d.bridge = lambda path, body=None: {"ok": False, "lines": ["That team is full."]}
        with self.assertRaisesRegex(ValueError, "full"):
            self.d.player_team({"steam_id": self.NEW, "team": "crw"})


class TeamsTest(unittest.TestCase):
    def setUp(self):
        import sqlite3
        self.d = load()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d.SHARED = self.tmp.name
        con = sqlite3.connect(f"{self.tmp.name}/teams.db")
        con.executescript(
            "CREATE TABLE teams (id INTEGER PRIMARY KEY, name TEXT, tag TEXT, motto TEXT DEFAULT '', description TEXT DEFAULT '', logo TEXT DEFAULT '',"
            " league TEXT DEFAULT '', category TEXT DEFAULT '', open INTEGER DEFAULT 0, created REAL DEFAULT 0);"
            "CREATE TABLE members (steam_id TEXT PRIMARY KEY, team INT, role TEXT, joined REAL);"
            "CREATE TABLE known (steam_id TEXT PRIMARY KEY, name TEXT, seen REAL);"
            "CREATE TABLE invites (team INT, steam_id TEXT, at REAL);"
            "CREATE TABLE applications (id INTEGER PRIMARY KEY, steam_id TEXT, applicant TEXT, name TEXT, tag TEXT, status TEXT, created REAL,"
            " decided_by TEXT, decided_at REAL, message TEXT DEFAULT '');"
            "INSERT INTO teams (id, name, tag, open) VALUES (1, 'Crew', 'crw', 1);"
            "INSERT INTO members VALUES ('76561198000000001', 1, 'member', 2), ('76561198000000002', 1, 'owner', 1);"
            "INSERT INTO known VALUES ('76561198000000001', 'Ann', 0), ('76561198000000002', 'Bo', 0);"
            "INSERT INTO invites VALUES (1, '76561198000000009', 0);"
            "INSERT INTO applications (id, steam_id, applicant, name, tag, status, created) VALUES (1, '3', 'Cy', 'New', 'NEW', 'pending', 5),"
            " (2, '4', 'Di', 'Old', 'OLD', 'denied', 4);")
        con.commit()
        con.close()
        con = sqlite3.connect(f"{self.tmp.name}/leaderboard-unranked.db")
        con.executescript("CREATE TABLE scores (board TEXT, steam_id TEXT, points INT);"
                          "INSERT INTO scores VALUES ('general', '76561198000000002', 30), ('general', 'name:Ann', 12), ('week-1', 'name:Ann', 99);")
        con.commit()
        con.close()
        self.calls = []

        def bridge(path, body=None):
            self.calls.append((path, body))
            return {"ok": True, "lines": ["Done."], "text": "ok"}
        self.d.bridge = bridge

    def test_snapshot_members_points_and_applications(self):
        out = self.d.teams_snapshot()
        team = out["teams"][0]
        self.assertEqual((team["tag"], team["open"], team["invites"]), ("CRW", True, 1))
        self.assertEqual([m["name"] for m in team["members"]], ["Bo", "Ann"])          # owner first
        self.assertEqual(team["points"], {"unranked": 42, "ranked": 0})                 # a name-keyed board counts too
        self.assertEqual([a["tag"] for a in out["pending"]], ["NEW"])
        self.assertEqual([a["status"] for a in out["recent"]], ["denied"])

    def test_no_database_is_not_an_error(self):
        self.d.SHARED = tempfile.mkdtemp()
        out = self.d.teams_snapshot()
        self.assertEqual((out["exists"], out["teams"], out["pending"]), (False, [], []))

    def test_actions_run_as_the_owner_through_the_bridge(self):
        self.d.team_action({"action": "set", "team": "crw", "key": "motto", "value": "  skate  "})
        self.d.team_action({"action": "set", "team": "CRW", "key": "open", "value": "no"})
        self.d.team_action({"action": "kick", "team": "CRW", "player": "76561198000000001"})
        self.d.team_action({"action": "disband", "team": "CRW"})
        self.assertEqual([c[1]["verb"] for c in self.calls], ["set", "set", "kick", "disband"])
        self.assertEqual(self.calls[0][1]["who"], {"steam_id": "76561198000000002", "name": "Bo"})
        self.assertEqual(self.calls[0][1]["args"], {"key": "motto", "value": "skate"})
        self.assertEqual(self.calls[1][1]["args"]["value"], "no")
        self.assertEqual(self.calls[3][1]["args"], {"confirm": "yes"})

    def test_decide_and_refusals(self):
        self.d.team_action({"action": "decide", "id": 1, "accept": True})
        self.assertEqual(self.calls[0], ("/v1/teams/decide", {"id": 1, "accept": True, "by": "dashboard"}))
        for body in ({"action": "set", "team": "NOPE", "key": "motto", "value": "x"},
                     {"action": "set", "team": "CRW", "key": "owner", "value": "x"},
                     {"action": "kick", "team": "CRW", "player": "76561198999999999"},
                     {"action": "boom", "team": "CRW"}):
            with self.assertRaises(ValueError):
                self.d.team_action(body)
        self.d.bridge = lambda path, body=None: {"ok": False, "lines": ["Only the owner can remove a leader."]}
        with self.assertRaisesRegex(ValueError, "owner can remove"):
            self.d.team_action({"action": "kick", "team": "CRW", "player": "76561198000000001"})
