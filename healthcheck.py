"""Container health from the server log (/data/ReSkateServer.log).

The log is appended to across restarts, so a stale "is up on" line must not count.
Healthy = the most recent lifecycle marker is "is up on" (i.e. no newer
"Signing in to Steam..." that never completed, and no newer "Config problem").
"""
import sys

LOG = "/data/ReSkateServer.log"
MARKERS = {
    "is up on": True,
    "Signing in to Steam": False,
    "Config problem": False,
}

state = None
try:
    with open(LOG, encoding="utf-8", errors="replace") as f:
        for line in f:
            for marker, ok in MARKERS.items():
                if marker in line:
                    state = (marker, ok)
except OSError:
    sys.exit("no log yet")

if state is None:
    sys.exit("no startup markers in log")
if not state[1]:
    sys.exit(f"last lifecycle event: {state[0]}")
