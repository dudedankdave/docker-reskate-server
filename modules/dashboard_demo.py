"""Look at the dashboard on your own PC with made-up data (no bridge, no Docker, no servers needed):

    python modules/dashboard_demo.py      # then open http://localhost:8790, password: demo12345

Only for trying the page; the real thing is `RESKATE_MODE=dashboard` (modules/dashboard.py)."""
import os
import time

os.environ.setdefault("DASHBOARD_PASSWORD", "demo12345")
os.environ.setdefault("BRIDGE_TOKEN", "demo")
os.environ.setdefault("DASHBOARD_DOCKER", "demo")
import dashboard  # noqa: E402

SERVERS = [
    {"server": "1", "role": "Main", "running": True, "players": 12, "max": 20, "map": "Super Ultra Mega Resort",
     "slots": {"A": {"running": True, "players": 12, "max": 20, "map": "Super Ultra Mega Resort", "version": "2.1.1"},
               "B": {"running": False}}},
    {"server": "2", "role": "Skate 2", "running": True, "players": 2, "max": 20, "map": "New San Vanelona",
     "slots": {"A": {"running": True, "players": 2, "max": 20, "map": "New San Vanelona", "version": "2.1.1"},
               "B": {"running": False}}},
    {"server": "3", "role": "Skate 3", "running": True, "players": None, "max": 20, "map": "Skate3Improved",
     "slots": {"A": {"running": True, "players": None, "max": 20, "map": "Skate3Improved", "version": "2.1.1"},
               "B": {"running": False}}},
    {"server": "6", "role": "Ranked", "running": False, "players": None, "max": None, "map": None,
     "slots": {"A": {"running": False}, "B": {"running": False}}},
]
CONTAINERS = [{"name": f"reskate-{n}", "image": "dudedankdave/reskate-tools:latest", "state": "running",
               "status": "Up 3 hours", "tool": True} for n in ("rollover", "bridge", "bot")]
CONTAINERS += [{"name": "reskate-server-1-2.1.1", "image": "dudedankdave/reskate-server:2.1.1", "state": "running",
                "status": "Up 2 days", "tool": False}]
sent = [0]


PEOPLE = {"1": [{"name": "Bob", "steam_id": "76561198000000001", "slot": "A", "team": "CRW"},
                {"name": "Alice", "steam_id": "76561198000000002", "slot": "A", "team": ""}],
          "2": [{"name": "Carl", "steam_id": "76561198000000003", "slot": "A", "team": "SK8"}]}


def bridge(path, body=None):
    if body is not None:
        print("demo: would send", path, body)     # nothing is sent anywhere
        return {"ok": True}
    if path.endswith("/players"):
        return PEOPLE.get(path.split("/")[3], [])
    if path.startswith("/v1/servers"):
        return [dict(s, slots={k: dict(v) for k, v in s["slots"].items()}) for s in SERVERS]
    sent[0] += 1
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {"events": [{"id": sent[0], "server": "1", "slot": "A", "time": stamp,
                        "text": f"Player{sent[0]} joined (1), {sent[0] % 20}/20 players"}], "last": sent[0]}


dashboard.bridge = bridge
dashboard.containers = lambda: {"docker": True, "containers": CONTAINERS}
dashboard.tool_log = lambda name, lines=60: {"name": name, "log": f"{name}: demo log, nothing real here\n"}
dashboard.main()
