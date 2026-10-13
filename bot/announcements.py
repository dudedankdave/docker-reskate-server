"""Scheduled announcements, edited from Discord: bot/bot.py's /schedule-* commands read and write
/shared/announcements.json, which the announcer module (modules/mod_announcer.py) works through.

    {"default": [{"text": "...", "every": 10, "offset": 0, "say": false}],
     "servers": {"1": {"extra": [...]}, "6": [...]}}

"all" is the default list (every server); a server's own `extra` adds to it. A server that has a plain list
(set by hand) keeps it: lines are added to and removed from that list instead."""
import json
import os

FILE = os.environ.get("ANNOUNCEMENTS_FILE") or "/shared/announcements.json"
MAX_TEXT = 200


def load(path=None):
    try:
        with open(path or FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def save(data, path=None):
    path = path or FILE
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(path + ".tmp", path)


def scope_list(data, scope, create=False):
    """The list of lines that `scope` ("all" or a server number) owns; None when there is none (and not create)."""
    if scope == "all":
        if create:
            data.setdefault("default", [])
        return data.get("default")
    servers = data.setdefault("servers", {}) if create else data.get("servers") or {}
    own = servers.get(scope)
    if isinstance(own, list):
        return own
    if isinstance(own, dict):
        if create:
            own.setdefault("extra", [])
        return own.get("extra")
    if create:
        servers[scope] = {"extra": []}
        return servers[scope]["extra"]
    return None


def add(data, scope, text, every, offset=0, say=False):
    text = text.strip()
    if not text or len(text) > MAX_TEXT:
        raise ValueError(f"The text must be 1-{MAX_TEXT} characters.")
    if not 1 <= every <= 1440:
        raise ValueError("every: 1-1440 minutes.")
    if not 0 <= offset < every:
        raise ValueError("offset: 0 up to (but not including) every.")
    entry = {"text": text, "every": every, "offset": offset}
    if say:
        entry["say"] = True
    lines = scope_list(data, scope, create=True)
    lines.append(entry)
    return len(lines)


def remove(data, scope, number):
    lines = scope_list(data, scope) or []
    if not 1 <= number <= len(lines):
        raise ValueError("No such line; /schedule-list shows the numbers.")
    return lines.pop(number - 1)


def describe(entry, number):
    extra = []
    if entry.get("offset"):
        extra.append(f"+{entry['offset']} min")
    if entry.get("say"):
        extra.append("chat")
    tail = f" ({', '.join(extra)})" if extra else ""
    return f"{number}. every {entry.get('every')} min{tail}: {entry.get('text')}"


def listing(data, scope=None):
    """Text for /schedule-list: all lists, or one scope's."""
    sections = []
    scopes = [scope] if scope else ["all", *sorted((data.get("servers") or {}), key=lambda s: (len(s), s))]
    for sc in scopes:
        lines = scope_list(data, sc) or []
        own = (data.get("servers") or {}).get(sc)
        head = "All servers" if sc == "all" else f"Server {sc}" + (
            " (own list, replaces all)" if isinstance(own, list) else " (adds to all)")
        sections.append(f"**{head}**\n" + ("\n".join(describe(e, n) for n, e in enumerate(lines, 1)) or "nothing"))
    return "\n\n".join(sections)
