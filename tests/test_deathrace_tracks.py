"""plugins/tools/deathrace_tracks.py: Skate 3's races (the Skate 3 Improved map's data/races.json) as our race
tracks, and the plugin's `coco import` that takes them on the server's console."""
import importlib.util
import json
import math
import pathlib
import tempfile
import unittest

from test_tools_plugin import Session, lights_out, lupa, skate

HERE = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("deathrace_tracks", HERE / "plugins" / "tools" / "deathrace_tracks.py")
dt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dt)

MAP = "Skate_3_Improved"   # the stand-in's map, as the plugin keys it


def box(x, z, heading=90.0, size=(2.0, 5.0, 8.0), y=0.0):
    return {"position": [x, y, z], "size": list(size), "name": "", "heading": heading}


# A race along +x (heading 90): three start slots across at x = 0, checkpoints at x = 20 and 40.
RACES = {"format": 1, "races": [
    {"id": "race_test_01", "title": "Test Race 1", "kind": "gates",
     "starts": [{"position": [0.0, 0.0, z], "heading": 90.0} for z in (-1.0, 0.0, 1.0)],
     "checkpoints": [[box(20, 0)], [box(40, 0)]]},
    {"id": "race_empty", "title": "No checkpoints", "starts": [{"position": [0, 0, 0], "heading": 0}], "checkpoints": []},
]}


class ConvertTest(unittest.TestCase):
    def test_a_race_becomes_a_start_line_and_its_checkpoints(self):
        tracks = dt.convert(RACES)
        self.assertEqual([t["name"] for t in tracks], ["Test Race 1"], "a race without checkpoints is left out")
        start, first, finish = tracks[0]["gates"]
        self.assertEqual(start["p"], [4.0, 0.0, 0.0], "4 m ahead of the slots: the grid stands on them")
        self.assertEqual(start["n"], [1.0, 0.0])
        self.assertEqual(start["w"], 6.0, "the slots' spread and 2 m each side")
        self.assertEqual((first["p"], first["n"], first["w"], first["h"]), ([20.0, 0.0, 0.0], [1.0, 0.0], 8.0, 5.0))
        self.assertAlmostEqual(finish["heading"], math.pi / 2, places=3)
        self.assertFalse(tracks[0]["circuit"])

    def test_a_turned_box_is_as_wide_as_it_is_across_the_way_through(self):
        gate = dt.checkpoint_gate(box(0, 0, heading=0.0, size=(6.0, 5.0, 2.0)))    # facing +z: across is x
        self.assertEqual((gate["n"], gate["w"]), ([0.0, 1.0], 6.0))
        gate = dt.checkpoint_gate(box(0, 0, heading=45.0, size=(4.0, 20.0, 4.0)))
        self.assertAlmostEqual(gate["w"], 5.7, places=1)
        self.assertEqual(gate["h"], 12, "as high as the plugin takes")

    def test_the_lines_are_coco_import_with_base64_json(self):
        line = dt.import_line("bam", dt.convert(RACES)[0])
        self.assertTrue(line.startswith("coco import bam "))
        self.assertEqual(len(line.split()), 4, "one console line, no spaces in the data")


@unittest.skipUnless(lupa, "needs lupa (pip install lupa)")
class ImportTest(unittest.TestCase):
    def setUp(self):
        self.session = Session()
        self.a = self.session.add("76561198000000001", "Coco", staff=True)
        self.b = self.session.add("76561198000000002", "Jambo")
        self.server = self.session.server
        self.session.settle()

    def prints(self):
        return [c[len("print "):] for c in self.server.calls if c.startswith("print ")]

    def test_imported_races_reach_everyone_and_replace_by_name(self):
        self.server.console(dt.import_line(MAP, dt.convert(RACES)[0]))
        self.session.settle()
        for client in (self.a, self.b):
            tracks = client.tracks()
            self.assertEqual([t["name"] for t in tracks], ["Test Race 1"], client.name)
            self.assertEqual(tracks[0]["author"], "Skate 3")
            self.assertEqual(len(tracks[0]["gates"]), 3)
        self.assertIn("[coco race] imported 1 track(s) on Skate_3_Improved: Test Race 1", self.prints())
        first = self.a.tracks()[0]["id"]
        self.server.console(dt.import_line(MAP, {"tracks": dt.convert(RACES, challenge=True)}))
        self.session.settle()
        tracks = self.a.tracks()
        self.assertEqual(len(tracks), 1, "the same name is replaced")
        self.assertEqual(tracks[0]["id"], first)
        self.assertTrue(tracks[0]["challenge"])

    def test_what_is_not_a_track_is_refused(self):
        self.server.console(dt.import_line(MAP, {"name": "One gate", "gates": [dt.convert(RACES)[0]["gates"][0]]}))
        self.server.console("coco import " + MAP + " not-base64!")
        self.server.console("coco import")
        self.session.settle()
        self.assertEqual(self.a.tracks(), [])
        self.assertTrue(any(p.startswith("error: coco import: not a valid track: One gate") for p in self.prints()), self.prints())
        self.assertEqual(sum(p == "error: coco import <map> <base64 json>" for p in self.prints()), 2, self.prints())

    def test_an_imported_race_runs_as_a_challenge(self):
        self.server.console(dt.import_line(MAP, dt.convert(RACES, challenge=True)[0]))
        self.session.settle()
        self.b.position = [2.0, 0.0, 0.0]
        self.b.key("gate_add")           # F8 by the start
        self.assertIsNotNone(self.b.race())
        lights_out(self.session)
        for target in ((20.0, 0.0, 0.0), (40.0, 0.0, 0.0), (45.0, 0.0, 0.0)):
            skate(self.session, self.b, target)
        self.session.settle()
        self.assertEqual(self.b.race().phase, "done")
        self.assertIsNotNone(self.b.race().racers[self.b.id].ms)


class BundleTest(unittest.TestCase):
    def test_the_bundle_keeps_each_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "bundled.lua"
            dt.write_bundle("bam", dt.convert(RACES), path)
            dt.write_bundle("other", [], path)
            maps = dt.read_bundle(path)
            self.assertEqual(sorted(maps), ["bam", "other"])
            self.assertEqual(maps["bam"][0]["name"], "Test Race 1")
            self.assertTrue(path.read_text(encoding="utf-8").startswith("-- Race tracks"))


@unittest.skipUnless(lupa, "needs lupa (pip install lupa)")
class SeedTest(unittest.TestCase):
    """The plugin's server copy adds the bundled tracks to their map when it starts (here: the stand-in's map)."""

    def setUp(self):
        self.kept = dt.BUNDLE.read_text(encoding="utf-8") if dt.BUNDLE.is_file() else None
        maps = dt.read_bundle()
        maps[MAP] = dt.convert(RACES)
        dt.BUNDLE.parent.mkdir(parents=True, exist_ok=True)
        dt.BUNDLE.write_text(dt.BUNDLE_HEAD + json.dumps(maps) + dt.BUNDLE_TAIL, encoding="utf-8")

    def tearDown(self):
        if self.kept is None:
            dt.BUNDLE.unlink()
            if not any(dt.BUNDLE.parent.iterdir()):
                dt.BUNDLE.parent.rmdir()
        else:
            dt.BUNDLE.write_text(self.kept, encoding="utf-8")

    def test_bundled_tracks_are_added_once_when_the_server_starts(self):
        session = Session()
        a = session.add("76561198000000001", "Coco", staff=True)
        session.settle()
        self.assertEqual([t["name"] for t in a.tracks()], ["Test Race 1"])
        prints = [c for c in session.server.calls if c.startswith("print [coco race] added 1 bundled track(s) on " + MAP)]
        self.assertEqual(len(prints), 1)
        seeded = session.server.to_py(session.server.lua.eval('reskate.storage.get("seeded:' + MAP + '")'))
        self.assertEqual(seeded, {"Test Race 1": True})


if __name__ == "__main__":
    unittest.main()
