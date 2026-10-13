"""Settings shared by the servers of a stack, or their own, from files in /shared/settings
(SETTINGS_DIR). Each layer overrides the one before:

    container env                  identity: PORT, QUERY_PORT, STEAM_TOKEN, RESKATE_SERVER (and everything on a
                                   single server). These differ per container (a server's A and B slot run at
                                   the same time with their own ports and token), so the files cannot set them.
    common.env                     what every server has
    secrets/common.env             the same, for secrets (values are never logged)
    server-<n>.env                 what only server <n> (RESKATE_SERVER) has
    secrets/server-<n>.env         its secrets (SERVER_PASSWORD, webhook URLs ...)

The files use the env-file format (KEY=value, # comments, "double" or 'single' quotes).
KEY+=value appends to the value of the layers before: a comma list (MODS, MAP_POOL, ADMINS ...),
a | list (ANNOUNCEMENTS) or a JSON list (COMMANDS, VOTES_CUSTOM, PARK_MODS). Without the folder
nothing changes. The result goes into the process environment before anything reads it, so the
server, the notifier and the leaderboard all see it.

Also read by the rollover (the same file in its image) to tell when a server's settings changed:
digest() is printed at start as "[settings] digest <hex>". slim/settings.go does the same."""
import hashlib
import json
import os

DIR = os.environ.get("SETTINGS_DIR") or "/shared/settings"
IDENTITY = ("PORT", "QUERY_PORT", "STEAM_TOKEN", "RESKATE_SERVER")   # per container, never from the files
PIPE_LISTS = ("ANNOUNCEMENTS",)


def parse(text):
    """[(key, value, append)] from env-file text."""
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        append = key.endswith("+")
        key = key.rstrip("+").strip()
        if not key.replace("_", "").isalnum():
            continue
        value = value.strip()
        if value[:1] in ('"', "'"):
            quote, body, i = value[0], [], 1
            while i < len(value) and value[i] != quote:
                if quote == '"' and value[i] == "\\" and i + 1 < len(value):
                    i += 1
                    body.append({"n": "\n", "t": "\t"}.get(value[i], value[i]))
                else:
                    body.append(value[i])
                i += 1
            value = "".join(body)
        else:
            cut = min((i for i in (value.find(" #"), value.find("\t#")) if i >= 0), default=-1)
            if cut >= 0:
                value = value[:cut].rstrip()
        out.append((key, value, append))
    return out


def joined(key, base, extra):
    """base with extra appended, as the list the key is."""
    if not base:
        return extra
    if not extra:
        return base
    if base.lstrip().startswith("["):
        try:
            a, b = json.loads(base), json.loads(extra)
            if isinstance(a, list) and isinstance(b, list):
                return json.dumps(a + b, sort_keys=True, ensure_ascii=False)   # as slim/settings.go writes it
        except ValueError:
            pass
    return base + ("|" if key in PIPE_LISTS else ",") + extra


def layers(server):
    """[(file name, secret)] in the order they apply."""
    names = [("common.env", False), (os.path.join("secrets", "common.env"), True)]
    if server:
        names += [(f"server-{server}.env", False), (os.path.join("secrets", f"server-{server}.env"), True)]
    return names


def load(environ, folder=None, report=True):
    """(values from the files, {file: [keys]}, {secret keys}) for the server in environ."""
    folder = folder or DIR
    server = (environ.get("RESKATE_SERVER") or "").strip()
    values, sources, secret = {}, {}, set()
    for name, is_secret in layers(server):
        try:
            with open(os.path.join(folder, name), encoding="utf-8-sig") as f:
                items = parse(f.read())
        except OSError:
            continue
        keys = []
        for key, value, append in items:
            if key in IDENTITY:
                if report:
                    print(f"[settings] {key} in {name} is ignored: it belongs to the container (per slot)", flush=True)
                continue
            if append:
                value = joined(key, values.get(key, environ.get(key, "")), value)
            values[key] = value
            keys.append(key)
            if is_secret:
                secret.add(key)
            else:
                secret.discard(key)
        if keys:
            sources[name] = keys
    return values, sources, secret


def digest(values):
    """A short hash of what the files set, to tell when they changed."""
    text = "".join(f"{k}={values[k]}\n" for k in sorted(values))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ---- settings the server takes while it runs ------------------------------------------------------
# key -> the console commands that set it now (ReSkate's README "Console and role commands"); the server
# saves them to its own file. Every other key needs a restart. slim/settings.go has the same keys.
def _onoff(cmd):
    return lambda v, e: [f"{cmd} {'on' if v.strip().lower() in ('1', 'true', 'yes', 'on') else 'off'}"]


def _plain(cmd, off_if_zero=False):
    return lambda v, e: [f"{cmd} {'off' if off_if_zero and v.strip() in ('0', '') else v.strip()}"]


def _clearable(cmd):
    return lambda v, e: [f"{cmd} {'off' if v.strip().lower() in ('off', 'none', '') else v.strip()}"]


def _list(clear, add, sep=","):
    return lambda v, e: [clear] + ([] if v.strip().lower() in ("off", "none", "") else
                                   [f"{add} {x.strip()}" for x in v.split(sep) if x.strip()])


def _kick_time(cmd):
    return lambda v, e: [f"{cmd} {'restart' if v.strip().lower() in ('0', 'restart') else v.strip()}"]


def _distances(v, e):
    keys = ("DISTANCE_FULL_RATE_RETURN", "DISTANCE_HALF_RATE_START", "DISTANCE_HALF_RATE_RETURN", "DISTANCE_LOW_RATE_START")
    values = [e.get(k, "").strip() for k in keys]
    return [f"distances {' '.join(values)}"] if all(values) else []


def _colors(v, e):
    badge, text = e.get("CHAT_COLOR", "").strip(), e.get("CHAT_TEXT_COLOR", "").strip()
    return [f"chat-color {badge} {text}".strip()] if badge else []


def _vote(name, field):
    if field == "_ENABLED":
        return lambda v, e: [f"votes {name} {'on' if v.strip().lower() in ('1', 'true', 'yes', 'on') else 'off'}"]
    if field == "_PERCENT":
        return lambda v, e: [f"votes {name} {v.strip()}"]
    word = {"_SECONDS": "seconds", "_COOLDOWN_SECONDS": "cooldown", "_MIN_PLAYERS": "min-players"}[field]
    return lambda v, e: [f"votes {name} {word} {v.strip()}"]


LIVE = {
    "SERVER_NAME": _plain("name"), "SERVER_PASSWORD": _clearable("password"), "WELCOME_MESSAGE": _clearable("welcome"),
    "LISTED": _onoff("listed"), "VOICE_CHAT": _onoff("voice"), "VOICE_RANGE": _plain("voice-range"),
    "OBJECT_PLACEMENT": _plain("placement"), "OBJECT_LIMIT": _plain("objects", True),
    "OBJECT_SCALING": _onoff("object-scaling"), "SYNC_EFFECTS": _onoff("effects"), "NOCLIP": _onoff("noclip"),
    "NO_BAIL": _onoff("nobail"), "BOOSTS": _onoff("boosts"), "ENFORCE_TUNING": _onoff("tuning"),
    "MAP_POOL": _list("map-pool clear", "map-pool add"), "MAP_ROTATION_MINUTES": _plain("rotation", True),
    "MAP_ROTATION_VOTE": _onoff("rotation vote"), "MAP_ROTATION_EXTEND_MINUTES": _plain("rotation stay", True),
    "MAP_ROTATION_ANNOUNCE": _onoff("rotation announce"), "MAP_LATEST_VERSIONS": _onoff("maps latest"),
    "MAP_LATEST_CHECK_MINUTES": _plain("maps latest every"), "WORLD_LAYER_SYNC": _onoff("layer-sync"),
    "VOTE_SECONDS": _plain("votes seconds"), "VOTE_COOLDOWN_SECONDS": _plain("votes cooldown"),
    "POLLS": _plain("votes polls"), "POLL_SECONDS": _plain("votes poll-seconds"),
    "VOTE_STARTER_YES": _onoff("votes starter-yes"),
    "ANNOUNCEMENTS": _list("announcements clear", "announcements add", "|"),
    "ANNOUNCEMENT_INTERVAL_MINUTES": _plain("announcements interval", True),
    "ANNOUNCE_THROWDOWNS": _onoff("announce-throwdowns"), "ACTIVITY_LOG": _onoff("activity-log"),
    "PARTIES": _onoff("parties"), "PARTY_SIZE": _plain("party-size"), "AFK_KICK_MINUTES": _plain("afk-kick", True),
    "KICK_MINUTES": _kick_time("kick-time"), "VOTE_KICK_MINUTES": _kick_time("votes kick-minutes"),
    "SPEED_CHECK": _plain("speed-check"), "BOARD_CHECK": _plain("board-check"), "SCORE_CHECK": _plain("score-check"),
    "BONE_SCALE_LIMIT": _plain("bone-scale", True), "BONE_REACH_LIMIT": _plain("bone-reach", True),
    "SEND_RATE": _plain("rate"), "CROWD_BUDGET": _plain("crowd", True), "GLOBAL_WORD_FILTER": _onoff("words global"),
    "CHAT_COLOR": _colors, "CHAT_TEXT_COLOR": _colors,
    "PARK_CONSTRUCTION": lambda v, e: [f"park construction {v.strip()}"],
    "PARK_HISTORIC": lambda v, e: [f"park historic {v.strip()}"],
    "PARK_FINANCIAL": lambda v, e: [f"park financial {v.strip()}"],
    "LAYERS": lambda v, e: [f"layers {' '.join(x.strip() for x in v.split(',') if x.strip())}"] if v.strip() else [],
    **{k: _distances for k in ("DISTANCE_FULL_RATE_RETURN", "DISTANCE_HALF_RATE_START", "DISTANCE_HALF_RATE_RETURN",
                               "DISTANCE_LOW_RATE_START")},
    **{prefix + field: _vote(name, field) for prefix, name in (("VOTE_MAP", "map"), ("VOTE_KICK", "kick"),
                                                                ("VOTE_TIME_OF_DAY", "tod"))
       for field in ("_ENABLED", "_PERCENT", "_SECONDS", "_COOLDOWN_SECONDS", "_MIN_PLAYERS")},
}
# Lists the env merges into what the server has (in-game additions stay), so a change only adds:
LIVE.update({
    "ADMINS": lambda v, e: [f"role {x.strip()} admin" for x in v.split(",") if x.strip()],
    "RESERVED": lambda v, e: [f"reserved add {x.strip()}" for x in v.split(",") if x.strip()],
    "SCORE_ALLOW": lambda v, e: [f"score-allow {x.strip().lower()}" for x in v.split(",") if x.strip()],
    "BANS": lambda v, e: [f"ban {x.partition(':')[0].strip()} {x.partition(':')[2].strip()}".strip()
                          for x in v.split(",") if x.strip()],
})


def live_commands(old, new):
    """(console commands that bring a running server from settings `old` to `new`, keys that need a restart)."""
    commands, restart = [], []
    for key in sorted(set(old) | set(new)):
        if old.get(key) == new.get(key):
            continue
        if key not in LIVE or key not in new:
            restart.append(key)
            continue
        lines = LIVE[key](new[key], new)
        if not lines:                     # nothing the console can say (e.g. one of the four distances alone)
            restart.append(key)
        for line in lines:
            if line not in commands:
                commands.append(line)
    return commands, restart


def restart_digest(values):
    """digest() of only the keys a running server cannot take (LIVE has the others)."""
    return digest({k: v for k, v in values.items() if k not in LIVE})


def apply(environ=os.environ, folder=None):
    """Put the files' values into environ and say where they came from. Returns the digest, or
    None when there is no settings folder."""
    folder = folder or DIR
    if not os.path.isdir(folder):
        return None
    values, sources, _ = load(environ, folder)
    for name, keys in sources.items():
        print(f"[settings] {name}: {', '.join(dict.fromkeys(keys))}", flush=True)
    environ.update(values)
    d = digest(values)
    print(f"[settings] digest {d}", flush=True)
    print(f"[settings] restart-digest {restart_digest(values)}", flush=True)
    return d
