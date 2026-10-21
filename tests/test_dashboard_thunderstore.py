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
            self.assertEqual(flags, {"vote_map": False, "vote_kick": False, "vote_tod": False, "rotation": 0, "layer_sync": False, "layers_file": False, "layers_fit": "missing"})


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
                        mock.patch.object(self.d, "moderate", lambda body: self.sent.append((body["server"], body["text"])))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

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
