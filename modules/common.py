"""What the modules share: each server's settings and whether a part is handed over to the modules.

A server's settings are what the bridge reports (its env and the settings files, no secrets) plus the
settings files themselves, read here with their secrets (the webhook URLs): the modules mount /shared.
Which servers a module serves comes from /shared/modules.json (app/handover.py): a module only acts
for a server that hands the part over, so nothing is done twice."""
import json
import os
import time

import bridgeclient
import handover
import settings

_cache = {}   # server -> (time, {slot: settings})


def say(message):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())} {message}", flush=True)


def server_settings(number, max_age=60):
    """{slot: {key: value}} for a server's running containers, refreshed every max_age seconds."""
    at, value = _cache.get(number, (0, None))
    if value is None or time.time() - at > max_age:
        base = bridgeclient.settings(number)
        files = settings.load({"RESKATE_SERVER": str(number)}, report=False)[0] if os.path.isdir(settings.DIR) else {}
        value = {slot: {**env, **files} for slot, env in base.items()}
        _cache[number] = (time.time(), value)
    return value


def handed_over(number, part, scope=None):
    """True when /shared/modules.json hands `part` (of `scope`) of this server to the modules."""
    try:
        with open(handover.FILE, encoding="utf-8") as f:
            parts = handover.parse(json.load(f), str(number))
    except (OSError, ValueError):
        return False
    value = parts.get(part)
    if isinstance(value, dict):
        value = value.get(scope) if scope else None
    return str(value or "").strip().lower() == "external"


def truthy(value, default=False):
    value = str(value or "").strip().lower()
    return default if not value else value in ("1", "true", "yes", "on")
