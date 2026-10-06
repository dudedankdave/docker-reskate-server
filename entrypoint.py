"""Writes /data/ReSkateServer.json from environment variables, then starts the server.

Every setting from Server/README.txt has an env var (see .env). A variable that is
unset or empty leaves the value already in the file (or the server default) alone,
so changes made in-game/console survive restarts unless the env var pins them.
ADMINS / BANS / SCORE_ALLOW are merged with what the file already holds.

MODS / MODS_UPDATE install Thunderstore mods before the server starts, see mods.py.
DISCORD_WEBHOOK (+ DISCORD_MENTION_IDS, DISCORD_CONSOLE, DISCORD_USERNAME) are not server
settings: they start notifier.py, see there.
"""
import json
import os
import sys

CONFIG = "/data/ReSkateServer.json"


def env(name):
    value = os.environ.get(name)
    return None if value is None or value.strip() == "" else value.strip()


def as_bool(name, value):
    v = value.lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    sys.exit(f"{name}: expected true/false, got {value!r}")


def as_int(name, value):
    try:
        return int(value)
    except ValueError:
        sys.exit(f"{name}: expected a number, got {value!r}")


def as_list(value):
    return [item.strip() for item in value.split(",") if item.strip()]


def choice(*allowed):
    def check(name, value):
        if value.lower() not in allowed:
            sys.exit(f"{name}: expected one of {', '.join(allowed)}, got {value!r}")
        return value.lower()
    return check


def non_negative(name, value):
    n = as_int(name, value)
    if n < 0:
        sys.exit(f"{name}: expected 0 (off) or more minutes, got {value!r}")
    return n


def text(_name, value):
    return value


# PASSWORD / WELCOME: "off" (or "none") clears them, since empty means "leave alone".
def clearable(_name, value):
    return "" if value.lower() in ("off", "none") else value


os.makedirs("/data/Mods", exist_ok=True)
cfg = {}
if os.path.exists(CONFIG):
    with open(CONFIG, encoding="utf-8") as f:
        cfg = json.load(f)

SIMPLE = {
    "SERVER_NAME": ("name", text),
    "MAP": ("map", text),
    "MAP_ROTATION_MINUTES": ("map_rotation_minutes", non_negative),
    "MAX_PLAYERS": ("max_players", as_int),
    "SERVER_PASSWORD": ("password", clearable),
    "WELCOME_MESSAGE": ("welcome", clearable),
    "LISTED": ("listed", as_bool),
    "AUTO_UPDATE": ("auto_update", as_bool),
    "ANNOUNCE_THROWDOWNS": ("announce_throwdowns", as_bool),
    "PARTIES": ("parties", as_bool),
    "PARTY_SIZE": ("party_size", as_int),
    "SPEED_CHECK": ("speed_check", choice("off", "warn", "kick")),
    "SCORE_CHECK": ("score_check", choice("off", "warn", "kick")),
    "ACTIVITY_LOG": ("activity_log", as_bool),
    "PORT": ("port", as_int),
    "QUERY_PORT": ("query_port", as_int),
    "TPS": ("tps", choice("20", "30", "60", "120")),
    "VOICE_CHAT": ("voice_chat", as_bool),
    "VOICE_RANGE": ("voice_range", as_int),
    "OBJECT_PLACEMENT": ("object_placement", choice("everyone", "admins", "nobody")),
    "NOCLIP": ("noclip", as_bool),
    "NO_BAIL": ("no_bail", as_bool),
    "BOOSTS": ("boosts", as_bool),
    "ENFORCE_TUNING": ("enforce_tuning", as_bool),
    "WORLD_LAYER_SYNC": ("world_layer_sync", as_bool),
}
for var, (key, convert) in SIMPLE.items():
    value = env(var)
    if value is not None:
        cfg[key] = convert(var, value)
if "tps" in cfg:
    cfg["tps"] = int(cfg["tps"])

NESTED = {
    "distances": {
        "DISTANCE_FULL_RATE_RETURN": "full_rate_return",
        "DISTANCE_HALF_RATE_START": "half_rate_start",
        "DISTANCE_HALF_RATE_RETURN": "half_rate_return",
        "DISTANCE_LOW_RATE_START": "low_rate_start",
    },
}
for section, mapping in NESTED.items():
    for var, key in mapping.items():
        value = env(var)
        if value is not None:
            cfg.setdefault(section, {})[key] = as_int(var, value)

for var, key in {"PARK_CONSTRUCTION": "construction", "PARK_HISTORIC": "historic",
                 "PARK_FINANCIAL": "financial"}.items():
    value = env(var)
    if value is not None:
        cfg.setdefault("parks", {})[key] = value

votes = cfg.setdefault("votes", {})
for prefix, key in (("VOTE_MAP", "map"), ("VOTE_KICK", "kick"), ("VOTE_TIME_OF_DAY", "time_of_day")):
    if (value := env(prefix + "_ENABLED")) is not None:
        votes.setdefault(key, {})["enabled"] = as_bool(prefix + "_ENABLED", value)
    if (value := env(prefix + "_PERCENT")) is not None:
        votes.setdefault(key, {})["percent"] = as_int(prefix + "_PERCENT", value)
if (value := env("VOTE_SECONDS")) is not None:
    votes["seconds"] = as_int("VOTE_SECONDS", value)
if (value := env("VOTE_COOLDOWN_SECONDS")) is not None:
    votes["cooldown_seconds"] = as_int("VOTE_COOLDOWN_SECONDS", value)
if not votes:
    del cfg["votes"]

# LAYERS=key=on,other_key=off
if (value := env("LAYERS")) is not None:
    layers = cfg.setdefault("layers", {})
    for item in as_list(value):
        key, _, mode = item.partition("=")
        mode = mode.strip().lower()
        if mode not in ("on", "off", "default"):
            sys.exit(f"LAYERS: {item!r} must be key=on|off|default")
        if mode == "default":
            layers.pop(key.strip(), None)
        else:
            layers[key.strip()] = mode

# MAP_POOL=Map A,Map B: the maps players vote between and the rotation goes through.
# Pins the list (in-game map-pool changes are replaced on restart); "off" empties it = all maps.
if (value := env("MAP_POOL")) is not None:
    cfg["map_pool"] = [] if value.lower() in ("off", "none") else as_list(value)

# Lists are merged so in-game additions (admin add, ban) are kept.
if (value := env("ADMINS")) is not None:
    admins = [str(a) for a in cfg.get("admins", [])]
    cfg["admins"] = admins + [a for a in as_list(value) if a not in admins]
if (value := env("SCORE_ALLOW")) is not None:
    allowed = [s.lower() for s in cfg.get("score_allow", [])]
    cfg["score_allow"] = allowed + [s.lower() for s in as_list(value) if s.lower() not in allowed]
# BANS=76561198000000000,76561198000000001:Some Name
if (value := env("BANS")) is not None:
    bans = cfg.setdefault("bans", [])
    known = {str(b.get("id")) for b in bans}
    for item in as_list(value):
        sid, _, name = item.partition(":")
        if sid.strip() not in known:
            bans.append({"id": sid.strip(), "name": name.strip(), "added": 0})

tmp = CONFIG + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
os.replace(tmp, CONFIG)

# Thunderstore mods/maps (MODS): installed before the server starts, failures never block it.
if (value := env("MODS")) is not None:
    update = as_bool("MODS_UPDATE", env("MODS_UPDATE") or "true")
    sys.path.insert(0, "/app")
    try:
        import mods
        mods.install_all(value, update)
    except Exception as exc:  # the server must start even if this breaks
        print(f"[mods] disabled: {exc!r}", flush=True)

# Discord sidecar (console forwarding + update announcements). It is forked off before the
# server is exec'd, so the server keeps the console for `docker attach`.
if (hook := env("DISCORD_WEBHOOK")) is not None:
    if not hook.startswith(("https://", "http://")):
        sys.exit("DISCORD_WEBHOOK: expected a webhook URL starting with https://")
    for var in ("DISCORD_MENTION_IDS",):
        for item in as_list(env(var) or ""):
            if not item.isdigit():
                sys.exit(f"{var}: expected Discord user ids (digits), got {item!r}")
    if (value := env("DISCORD_CONSOLE")) is not None:
        as_bool("DISCORD_CONSOLE", value)
    if os.fork() == 0:
        try:
            os.setsid()
            devnull = os.open(os.devnull, os.O_RDWR)
            for fd in (0, 1, 2):
                os.dup2(devnull, fd)
            sys.path.insert(0, "/app")
            import notifier
            notifier.main()
        finally:
            os._exit(0)

args = ["/app/ReSkateServer", "--config", CONFIG] + sys.argv[1:]
os.chdir("/app")
os.execv(args[0], args)
