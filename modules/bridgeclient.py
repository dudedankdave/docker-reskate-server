"""Client of the bridge (rollover/bridge.py) for the modules: events in, actions out.

BRIDGE_URL (default http://127.0.0.1:8780) and BRIDGE_TOKEN. Standard library only."""
import json
import os
import time
import urllib.error
import urllib.request

URL = (os.environ.get("BRIDGE_URL") or "http://127.0.0.1:8780").rstrip("/")
TOKEN = os.environ.get("BRIDGE_TOKEN", "").strip()


class BridgeError(RuntimeError):
    pass


def call(method, path, body=None, timeout=40):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(URL + path, data=data, method=method,
                                 headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return json.loads(res.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read() or b"{}").get("error", "")
        except ValueError:
            detail = ""
        raise BridgeError(f"{method} {path}: HTTP {e.code} {detail}".strip()) from None
    except (OSError, ValueError) as e:
        raise BridgeError(f"{method} {path}: {e}") from None


def servers():
    return call("GET", "/v1/servers")


def settings(number):
    """{slot: {key: value}} of a server's running containers (no secrets)."""
    return call("GET", f"/v1/servers/{number}/settings")


def do(number, action, args=None, by="", slot=None):
    body = {"action": action, "args": args or {}, "by": by}
    if slot:
        body["slot"] = slot
    return call("POST", f"/v1/servers/{number}/do", body)


def players(number):
    """[{name, steam_id, slot, team}] on a server now."""
    return call("GET", f"/v1/servers/{number}/players")


def team_tags():
    """{steam_id: tag} of every team member (empty when the bridge keeps no teams)."""
    return call("GET", "/v1/teams/tags")


def team_profiles(team=""):
    """Team profiles (one when `team` is given): name, tag, motto, description, logo, league, category, members."""
    from urllib.parse import quote
    return call("GET", "/v1/teams/profiles" + (f"?team={quote(team)}" if team else ""))


def team_act(who, verb, args=None):
    """Run a /team command; {"ok", "lines", "notify", "application"}."""
    return call("POST", "/v1/teams/act", {"who": who, "verb": verb, "args": args or {}})


def team_applications(status="pending"):
    return call("GET", f"/v1/teams/applications?status={status}")


def team_decide(application, accept, by):
    return call("POST", "/v1/teams/decide", {"id": application, "accept": accept, "by": by})


def team_posted(application, message):
    return call("POST", f"/v1/teams/applications/{application}/posted", {"message": str(message)})


def console(number, line, by, slot=None):
    """Type one console line (say, msg, announce ...) into a server; False when it did not get there."""
    try:
        do(number, "console", {"command": line}, by, slot)
        return True
    except BridgeError:
        return False


def events(scope=None, server=None, wait=25, start="now"):
    """Yield events as they come (forever), from now on (start="now") or from after an event id.
    Waits and tries again while the bridge is away."""
    after, delay = None, 1
    while True:
        try:
            if after is None:
                after = call("GET", "/v1/events?after=999999999&wait=0")["last"] if start == "now" else int(start)
            q = f"/v1/events?after={after}&wait={wait}" + (f"&scope={scope}" if scope else "") + \
                (f"&server={server}" if server else "")
            got = call("GET", q, timeout=wait + 15)
            if got["last"] < after:              # the bridge started again: its ids begin anew
                after = 0
                continue
            for e in got["events"]:
                after = max(after, e["id"])
                yield e
            if not got["events"]:
                after = max(after, got["last"]) if not (scope or server) else after
            delay = 1
        except BridgeError as e:
            print(f"[bridge] {e}; trying again in {delay} s", flush=True)
            time.sleep(delay)
            delay = min(delay * 2, 60)
