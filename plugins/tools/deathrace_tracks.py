"""Skate 3's races as COJA tools race tracks.

The Skate 3 Improved map (Thunderstore: 333-Skate_3_Improved) ships Skate 3's ten races, taken from the retail game,
in its data/races.json for its Deathraces: per race its start slots and its checkpoints, each an axis-aligned box
({position: its bottom centre, size: its extents along x, y, z, heading: the way through, degrees}). This turns them
into our tracks (plugin/main.lua, clean_track): gate 1 a start line across the start slots, then one gate per
checkpoint, the last the finish. It prints a `coco import <map> <base64 json>` line per track for the server's
console, which checks and saves them as Track Studio does.

    python plugins/tools/deathrace_tracks.py <races.json> [--map bam] [--challenge] [--only race_univ_02,...]
"""
import argparse
import base64
import json
import math
import pathlib
import sys

MAX_GATES = 40
START_AHEAD = 4.0   # metres from the start slots to gate 1: the plugin's grid starts 4 m behind gate 1
START_HEIGHT = 5.0


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def facing(heading_deg):
    """A heading's way through, level: the game's heading h faces (sin h, cos h) in x and z."""
    h = math.radians(heading_deg)
    return math.sin(h), math.cos(h)


def checkpoint_gate(box):
    """A checkpoint box -> our gate: its bottom centre, its heading, as wide as the box is across that heading."""
    nx, nz = facing(float(box.get("heading", 0)))
    sx, sy, sz = (float(s) for s in (box.get("size") or [8, 5, 8]))
    width = abs(nz) * sx + abs(nx) * sz   # the box's extent along (nz, -nx), the way across
    return {
        "p": [round(float(c), 2) for c in box["position"]],
        "n": [round(nx, 3), round(nz, 3)],
        "w": round(clamp(width, 2, 30), 1),
        "h": round(clamp(sy, 1.5, 12), 1),
        "heading": round(math.radians(float(box.get("heading", 0))), 3),
    }


def start_gate(starts):
    """Gate 1: a line across the start slots, START_AHEAD in front of them, so the grid stands where they were. Only
    the slots facing as slot 1 does count (University Race 2 has a crowd of 12 at three headings)."""
    heading = float(starts[0].get("heading", 0))
    nx, nz = facing(heading)
    pts = [[float(c) for c in s["position"]] for s in starts if abs(float(s.get("heading", 0)) - heading) < 1]
    mid = [sum(p[i] for p in pts) / len(pts) for i in range(3)]
    across = [abs((p[0] - mid[0]) * nz - (p[2] - mid[2]) * nx) for p in pts]
    return {
        "p": [round(mid[0] + nx * START_AHEAD, 2), round(min(p[1] for p in pts), 2), round(mid[2] + nz * START_AHEAD, 2)],
        "n": [round(nx, 3), round(nz, 3)],
        "w": round(clamp(2 * max(across) + 4, 2, 30), 1),
        "h": START_HEIGHT,
        "heading": round(math.radians(float(starts[0].get("heading", 0))), 3),
    }


def convert(doc, challenge=False, only=None):
    """races.json -> our tracks, in its order. A race without starts or checkpoints is left out."""
    tracks = []
    for race in doc.get("races") or []:
        if only and race.get("id") not in only:
            continue
        stages = [s for s in race.get("checkpoints") or [] if s]
        if not race.get("starts") or not stages:
            continue
        gates = [start_gate(race["starts"])] + [checkpoint_gate(stage[0]) for stage in stages]
        if len(gates) > MAX_GATES:
            raise ValueError(f"{race.get('id')}: {len(gates)} gates, the plugin takes {MAX_GATES}")
        tracks.append({"name": str(race.get("title") or race["id"])[:32], "author": "Skate 3",
                       "circuit": False, "challenge": challenge, "gates": gates})
    return tracks


def import_line(map_key, value):
    data = base64.b64encode(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).decode("ascii")
    return f"coco import {map_key} {data}"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("races", type=pathlib.Path, help="the Skate 3 Improved map's data/races.json")
    ap.add_argument("--map", default="bam", help="the map's key on the server (Skate 3: bam)")
    ap.add_argument("--challenge", action="store_true", help="make each a challenge (solo, timed, a top 10)")
    ap.add_argument("--only", default="", help="race ids, comma separated (race_univ_02,...)")
    args = ap.parse_args(argv)
    doc = json.loads(args.races.read_text(encoding="utf-8"))
    only = {s.strip() for s in args.only.split(",") if s.strip()} or None
    for track in convert(doc, args.challenge, only):
        print(import_line(args.map, track))
    return 0


if __name__ == "__main__":
    sys.exit(main())
