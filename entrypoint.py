"""Writes /data/ReSkateServer.json from environment variables, then starts the server.

Every setting from Server/README.txt has an env var (see .env). A variable that is
unset or empty leaves the value already in the file (or the server default) alone,
so changes made in-game/console survive restarts unless the env var pins them.
ADMINS / RESERVED / SCORE_ALLOW / BANS are merged with what the file already holds.

ReSkate 1.1.7 put the settings into sections ("server", "access", "maps", ...) and renamed
several. A file from an older version is moved to that layout here first (as the server
itself would), so the env vars below always land in one place. Bans live in data/bans.json.

MODS / MODS_UPDATE install Thunderstore mods before the server starts, see mods.py.
DISCORD_WEBHOOK_ADMIN / DISCORD_WEBHOOK (older name) / DISCORD_WEBHOOK_USER (+ DISCORD_MENTION_IDS, DISCORD_CONSOLE, DISCORD_USERNAME) are not server
settings: they start notifier.py, see there.
"""
import json
import os
import re
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


# ReSkate only accepts 1-64 ASCII letters, digits, spaces and - _ / [ ] ( ) as a name and
# refuses to start otherwise. Clean a name it would reject (and say so) instead of crash-looping.
NAME_OK = re.compile(r"[A-Za-z0-9 _/\[\]()-]{1,64}")
ACCENTS = {ord(k): v for k, v in zip(
    "äöüÄÖÜéèêëÉÈÊËáàâãåÁÀÂÃÅíìîïÍÌÎÏóòôõøÓÒÔÕØúùûÚÙÛýÿÝçÇñÑ",
    "aouAOUeeeeEEEEaaaaaAAAAAiiiiIIIIoooooOOOOOuuuUUUyyYcCnN")}
ACCENTS.update({ord("ß"): "ss", ord("æ"): "ae", ord("Æ"): "AE", ord("œ"): "oe", ord("Œ"): "OE"})


def clean_name(value):
    s = value.translate(ACCENTS)
    s = re.sub(r"['\u2019.#%]", "", s)              # joins the letters around it: COCOJAMBO'S -> COCOJAMBOS
    s = re.sub(r"[^A-Za-z0-9 _/\[\]()-]", "-", s)    # | : , ... become a dash
    s = re.sub(r"\s*-(?:\s*-)+\s*", " - ", s)       # no runs of dashes
    s = re.sub(r"\s+", " ", s).strip(" -")
    return s[:64].rstrip(" -") or "ReSkate server"


def server_name(name, value):
    if NAME_OK.fullmatch(value):
        return value
    clean = clean_name(value)
    print(f'[config] {name} {value!r} is not accepted by ReSkate (1-64 letters, numbers, spaces and - _ / [ ] ( ) only), '
          f'using {clean!r} instead', flush=True)
    return clean


# Maps the server knows without a mod (ReSkate's own list); anything else must come from an installed mod.
BUILTIN_MAPS = ["San Vansterdam", "Isle of Grom", "Super Ultra Mega Resort", "Stadium 1"]


def warn_unknown_maps(cfg):
    """The server refuses to start on an unknown map. Say which names are available before it does."""
    provided = {}
    try:
        folders = sorted(os.listdir("/data/Mods"))
    except OSError:
        folders = []
    for folder in folders:
        try:
            with open(f"/data/Mods/{folder}/reskate-levels.json", encoding="utf-8-sig") as f:
                for level in json.load(f).get("levels", []):
                    if level.get("displayName"):
                        provided[level["displayName"]] = folder
        except (OSError, ValueError, AttributeError):
            pass
    known = {m.lower() for m in BUILTIN_MAPS} | {m.lower() for m in provided}
    maps = cfg.get("maps", {})
    wanted = [("MAP", maps["map"])] if isinstance(maps.get("map"), str) and maps["map"] else []
    wanted += [("MAP_POOL", str(m)) for m in (maps.get("pool") or [])]
    for var, name in wanted:
        if name.lower() not in known:
            print(f'[maps] WARNING: {var} "{name}" is not a built-in map and no installed mod provides it, '
                  f'the server will refuse to start (Config problem). Installed mod maps: '
                  f'{", ".join(sorted(provided)) or "none"}. Built-in maps: {", ".join(BUILTIN_MAPS)}.', flush=True)


def int_range(lo, hi):
    def check(name, value):
        n = as_int(name, value)
        if not lo <= n <= hi:
            sys.exit(f"{name}: expected a number from {lo} to {hi}, got {value!r}")
        return n
    return check


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

BANS_FILE = os.path.join(os.path.dirname(CONFIG), "data", "bans.json")

# section -> (key, key before 1.1.7)
LAYOUT = {
    "server": [("name", "name"), ("password", "password"), ("welcome_message", "welcome"),
               ("listed", "listed"), ("max_players", "max_players"), ("port", "port"),
               ("query_port", "query_port"), ("steam_token", "steam_token"),
               ("auto_update", "auto_update"), ("activity_log", "activity_log")],
    "access": [("admins", "admins"), ("reserved_players_slots", "reserved"),
               ("use_global_bans", "global_bans")],
    "maps": [("map", "map"), ("pool", "map_pool"), ("rotation_minutes", "map_rotation_minutes"),
             ("parks", "parks"), ("world_layer_sync", "world_layer_sync"), ("layers", "layers")],
    "players": [("allow_boosts", "boosts"), ("allow_no_bail", "no_bail"), ("allow_noclip", "noclip"),
                ("allow_parties", "parties"), ("party_size", "party_size"),
                ("allow_voice_chat", "voice_chat"), ("voice_range", "voice_range"),
                ("object_placement", "object_placement"), ("object_limit", "object_limit"),
                ("announce_throwdowns", "announce_throwdowns")],
    "anti_cheat": [("speed_hack", "speed_check"), ("modified_scoring", "score_check"),
                   ("allowed_scoring_mods", "score_allow"), ("enforce_tuning", "enforce_tuning"),
                   ("bone_scale_limit", "bone_scale_limit")],
    "network": [("send_rate", "send_rate"), ("crowd_budget", "crowd_budget"), ("distances", "distances")],
}
REMOVED = {"tps": "TPS", "reserved_slots": "RESERVED_SLOTS"}  # gone in 1.1.7


def load_bans():
    try:
        with open(BANS_FILE, encoding="utf-8") as f:
            bans = json.load(f)
        return bans if isinstance(bans, list) else []
    except (OSError, ValueError):
        return []


def merge_bans(bans, new):
    known = {str(b.get("id")) for b in bans if isinstance(b, dict)}
    for b in new:
        if isinstance(b, dict) and str(b.get("id")) not in known:
            known.add(str(b.get("id")))
            bans.append(b)
    return bans


def migrate(cfg):
    """Moves pre-1.1.7 top-level settings into their sections (a value already in a section wins)."""
    moved = False
    for section, keys in LAYOUT.items():
        for key, old in keys:
            if old in cfg:  # no old name is also a section name
                cfg.setdefault(section, {}).setdefault(key, cfg.pop(old))
                moved = True
    for key in REMOVED:
        moved |= cfg.pop(key, None) is not None
    if "bans" in cfg:
        write_bans(merge_bans(load_bans(), cfg.pop("bans") or []))
        moved = True
    if moved:
        print("[config] moved the settings to the sectioned layout of ReSkate 1.1.7+", flush=True)
    return cfg


def write_bans(bans):
    os.makedirs(os.path.dirname(BANS_FILE), exist_ok=True)
    tmp = BANS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(bans, f, indent=2)
        f.write("\n")
    os.replace(tmp, BANS_FILE)


migrate(cfg)


def setting(section, key, value):
    cfg.setdefault(section, {})[key] = value


def get(section, key, default=None):
    return cfg.get(section, {}).get(key, default)


SIMPLE = {
    "SERVER_NAME": ("server", "name", server_name),
    "SERVER_PASSWORD": ("server", "password", clearable),
    "WELCOME_MESSAGE": ("server", "welcome_message", clearable),
    "LISTED": ("server", "listed", as_bool),
    "MAX_PLAYERS": ("server", "max_players", int_range(1, 249)),
    "PORT": ("server", "port", as_int),
    "QUERY_PORT": ("server", "query_port", as_int),
    "STEAM_TOKEN": ("server", "steam_token", clearable),
    "AUTO_UPDATE": ("server", "auto_update", as_bool),
    "ACTIVITY_LOG": ("server", "activity_log", as_bool),
    "GLOBAL_BANS": ("access", "use_global_bans", as_bool),
    "MAP": ("maps", "map", text),
    "MAP_ROTATION_MINUTES": ("maps", "rotation_minutes", non_negative),
    "WORLD_LAYER_SYNC": ("maps", "world_layer_sync", as_bool),
    "BOOSTS": ("players", "allow_boosts", as_bool),
    "NO_BAIL": ("players", "allow_no_bail", as_bool),
    "NOCLIP": ("players", "allow_noclip", as_bool),
    "PARTIES": ("players", "allow_parties", as_bool),
    "PARTY_SIZE": ("players", "party_size", as_int),
    "VOICE_CHAT": ("players", "allow_voice_chat", as_bool),
    "VOICE_RANGE": ("players", "voice_range", int_range(50, 1000)),
    "OBJECT_PLACEMENT": ("players", "object_placement", choice("everyone", "admins", "nobody")),
    "OBJECT_LIMIT": ("players", "object_limit", int_range(0, 1024)),
    "ANNOUNCE_THROWDOWNS": ("players", "announce_throwdowns", as_bool),
    "SPEED_CHECK": ("anti_cheat", "speed_hack", choice("off", "warn", "kick")),
    "SCORE_CHECK": ("anti_cheat", "modified_scoring", choice("off", "warn", "kick")),
    "ENFORCE_TUNING": ("anti_cheat", "enforce_tuning", as_bool),
    "BONE_SCALE_LIMIT": ("anti_cheat", "bone_scale_limit", int_range(0, 8)),
    "USE_STEAM_RELAY": ("network", "use_steam_relay", as_bool),
    "SEND_RATE": ("network", "send_rate", int_range(128, 16384)),
    "CROWD_BUDGET": ("network", "crowd_budget", int_range(0, 1000000)),
    "PACK_MS": ("network", "pack_ms", int_range(0, 50)),
    "FINGER_DISTANCE": ("network", "finger_distance", int_range(0, 100000)),
    "STEAM_DEBUG": ("network", "steam_debug", as_bool),
}
for var, (section, key, convert) in SIMPLE.items():
    value = env(var)
    if value is not None:
        setting(section, key, convert(var, value))
for key, var in REMOVED.items():
    if env(var) is not None:
        print(f"[config] {var} is ignored: ReSkate 1.1.7 removed the {key!r} setting", flush=True)

NESTED = {
    ("network", "distances"): {
        "DISTANCE_FULL_RATE_RETURN": "full_rate_return",
        "DISTANCE_HALF_RATE_START": "half_rate_start",
        "DISTANCE_HALF_RATE_RETURN": "half_rate_return",
        "DISTANCE_LOW_RATE_START": "low_rate_start",
    },
}
for (section, sub), mapping in NESTED.items():
    for var, key in mapping.items():
        value = env(var)
        if value is not None:
            cfg.setdefault(section, {}).setdefault(sub, {})[key] = as_int(var, value)

for var, key in {"PARK_CONSTRUCTION": "construction", "PARK_HISTORIC": "historic",
                 "PARK_FINANCIAL": "financial"}.items():
    value = env(var)
    if value is not None:
        cfg.setdefault("maps", {}).setdefault("parks", {})[key] = value

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
    layers = cfg.setdefault("maps", {}).setdefault("layers", {})
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
    setting("maps", "pool", [] if value.lower() in ("off", "none") else as_list(value))

# Lists are merged so in-game additions (admin add, ban) are kept.
def merge_list(section, key, value, lower=False):
    norm = (lambda x: str(x).lower()) if lower else str
    have = [norm(a) for a in get(section, key, [])]
    setting(section, key, have + [a for a in map(norm, as_list(value)) if a not in have])


if (value := env("ADMINS")) is not None:
    merge_list("access", "admins", value)
if (value := env("RESERVED")) is not None:
    merge_list("access", "reserved_players_slots", value)
if (value := env("SCORE_ALLOW")) is not None:
    merge_list("anti_cheat", "allowed_scoring_mods", value, lower=True)
# BANS=76561198000000000,76561198000000001:Some Name  (written to data/bans.json)
if (value := env("BANS")) is not None:
    new = []
    for item in as_list(value):
        sid, _, name = item.partition(":")
        new.append({"id": sid.strip(), "name": name.strip(), "added": 0})
    bans = load_bans()
    count = len(bans)
    if len(merge_bans(bans, new)) != count or not os.path.exists(BANS_FILE):
        write_bans(bans)

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

warn_unknown_maps(cfg)

# Discord sidecar (console forwarding + update announcements). It is forked off before the
# server is exec'd, so the server keeps the console for `docker attach`.
hooks = [(v, env(v)) for v in ("DISCORD_WEBHOOK", "DISCORD_WEBHOOK_ADMIN", "DISCORD_WEBHOOK_USER")]
hooks = [(v, h) for v, h in hooks if h is not None]
if hooks:
    for var, hook in hooks:
        if not hook.startswith(("https://", "http://")):
            sys.exit(f"{var}: expected a webhook URL starting with https://")
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

# ReSkate 1.1.4+ can replace its own binary. In a container that drifts from the image tag and is
# lost when the container is recreated, so it stays off unless AUTO_UPDATE=true is set explicitly.
auto = (env("AUTO_UPDATE") or "").lower() in ("1", "true", "yes", "on")
no_update = [] if auto or "--no-update" in sys.argv[1:] else ["--no-update"]
args = ["/app/ReSkateServer", "--config", CONFIG] + no_update + sys.argv[1:]
os.chdir("/app")
os.execv(args[0], args)
