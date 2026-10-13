"""The kick announcer module (RESKATE_MODE=announcer): app/announcer.py from outside the server, for every
server whose /shared/modules.json says "announcer": "external" and that has ANNOUNCE_KICKS on (default).
An admin kick or ban in a server's console becomes "Name was kicked." / "Name was banned." in its chat.

Needs BRIDGE_URL and BRIDGE_TOKEN, and /shared (read-only) for modules.json."""
import os
import re
import sys
import time

sys.path[:0] = [os.path.dirname(os.path.abspath(__file__)), os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib")]
import announcer  # noqa: E402
import bridgeclient  # noqa: E402
import common  # noqa: E402

BY = "module:announcer"
LINE = re.compile(r"^\[\d\d:\d\d:\d\d\] (.*)$")


def announcement(line):
    """The chat line for one console line as the bridge has it ("[HH:MM:SS] text"), or None."""
    m = LINE.match(line.rstrip("\r\n"))
    if not m:
        return None
    return announcer.said(m.group(1))


def wanted(number, slot, cache={}):
    """Handed over and ANNOUNCE_KICKS on; looked up at most once a minute per server."""
    at, value = cache.get(number, (0, None))
    if value is None or time.time() - at > 60:
        value = common.handed_over(number, "announcer") and {
            s for s, env in common.server_settings(number).items() if common.truthy(env.get("ANNOUNCE_KICKS"), True)}
        cache[number] = (time.time(), value)
    return bool(value) and (slot in value or slot is None)


def main():
    if not bridgeclient.TOKEN:
        sys.exit("announcer: BRIDGE_TOKEN is not set")
    for e in bridgeclient.events():
        if e.get("kind", "console") != "console":
            continue
        text = announcement(e["text"])
        if text:
            try:
                if wanted(e["server"], e.get("slot")):
                    bridgeclient.console(e["server"], f"say {text}", BY, e.get("slot"))
            except bridgeclient.BridgeError as exc:
                common.say(f"server {e['server']}: {exc}")


if __name__ == "__main__":
    main()
