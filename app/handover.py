"""Which parts this server leaves to the modules outside it (bridge, leaderboard, webhooks ...),
read from /shared/modules.json while the server runs, so a part can be handed over and back
without a restart. Without the file everything runs inside the server, as before.

    {"leaderboard": "external", "announcer": "internal",
     "webhooks": {"public": "external", "chat": "external"},
     "servers": {"6": {"leaderboard": "internal"}}}

Parts: leaderboard, announcer, webhooks. "webhooks" is one value for every scope, or one per scope
(admin, log, public, chat, leaderboard, updates, updates-admin; a scope not listed stays internal).
"servers" overrides per server, by RESKATE_SERVER. The [event] lines are printed either way."""
import json
import os
import threading
import time

FILE = os.environ.get("MODULES_FILE") or "/shared/modules.json"
PARTS = ("leaderboard", "announcer", "webhooks")
CHECK_EVERY = 60

_lock = threading.Lock()
_state = {"at": 0.0, "mtime": None, "parts": {}}


def parse(data, server=""):
    """{part: "external"/"internal" or, for webhooks, {scope: ...}} from the file's JSON."""
    if not isinstance(data, dict):
        return {}
    parts = {k: data[k] for k in PARTS if k in data}
    own = (data.get("servers") or {}).get(str(server)) if server else None
    if isinstance(own, dict):
        parts.update({k: own[k] for k in PARTS if k in own})
    return parts


def _load():
    try:
        mtime = os.stat(FILE).st_mtime
    except OSError:
        mtime = None
    if mtime == _state["mtime"]:
        return
    parts = {}
    if mtime is not None:
        try:
            with open(FILE, encoding="utf-8") as f:
                parts = parse(json.load(f), os.environ.get("RESKATE_SERVER", "").strip())
        except (OSError, ValueError) as e:
            print(f"[modules] cannot read {FILE}, keeping what was set: {e}", flush=True)
            return
    old = _state["parts"]
    _state["mtime"], _state["parts"] = mtime, parts
    if parts != old:
        shown = ", ".join(f"{k}={json.dumps(v)}" for k, v in sorted(parts.items())) or "everything inside the server"
        print(f"[modules] {shown}", flush=True)


def external(part, scope=None):
    """True when `part` (for webhooks: of `scope`) is run outside the server now."""
    with _lock:
        now = time.monotonic()
        if now - _state["at"] >= CHECK_EVERY or not _state["at"]:
            _state["at"] = now
            _load()
        value = _state["parts"].get(part)
    if isinstance(value, dict):
        value = value.get(scope) if scope else None
    return str(value or "").strip().lower() == "external"
