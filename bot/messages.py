"""Server settings edited from Discord: bot/bot.py's /setting-* and /message-* commands write them into the
settings files (/shared/settings/common.env for all servers, server-<n>.env for one; secrets in secrets/ beside
them; app/settings.py), where the settings module types what a running server can take into its console and the
leaderboard module picks up the Ranked/Unranked texts; the rest the rollover applies when the server is empty.
Only the documented settings can be edited here (not the container's identity: ports, Steam token ...); other
lines in the files are left as they are."""
import os
import re
import sys

sys.path[:0] = [os.path.dirname(os.path.abspath(__file__)), os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib")]
import settings  # noqa: E402  (app/settings.py, copied into the image)

DIR = os.environ.get("SETTINGS_DIR") or "/shared/settings"
KEYS = {
    "join": "WELCOME_MESSAGE",
    "ranked": "RANKED_MESSAGE",
    "unranked-mods": "UNRANKED_MESSAGE",
    "unranked-tuning": "UNRANKED_TUNING_MESSAGE",
    "unranked-speed": "UNRANKED_SPEED_MESSAGE",
}
MAX_BYTES = 200
MAX_VALUE = 2000
SECRET = re.compile(r"PASSWORD|TOKEN|SECRET|WEBHOOK|_KEY$|^KEY$", re.I)   # as the bridge hides them
# Documented settings (docs/ENVIRONMENT.md) beyond settings.LIVE, which a running server cannot take: they
# wait for the rollover to restart the server once nobody is on it. Not here: PORT, QUERY_PORT, STEAM_TOKEN,
# RESKATE_*, file locations and the bot's own token.
RESTART_ONLY = frozenset("""ANNOUNCE_KICKS COMMANDS DISCORD_APPROVAL_CHANNEL DISCORD_MENTION_IDS DISCORD_USERNAME FINGER_DISTANCE
GLOBAL_BANS LEADERBOARD LEADERBOARD_BOARDS LEADERBOARD_COMMANDS LEADERBOARD_DISCORD LEADERBOARD_DISCORD_INTERVALS
LEADERBOARD_INTERVAL LEADERBOARD_INTERVALS LEADERBOARD_LEAGUE LEADERBOARD_POINTS LEADERBOARD_PROPS_ANNOUNCE
LEADERBOARD_PROPS_PER_DAY LEADERBOARD_SCOPE LEADERBOARD_SIZE MAP MAP_ROTATION_VOTE_SECONDS MAX_PLAYERS MODS MODS_MAX_GB
MODS_STORE MODS_UPDATE PACK_MS PARK_LIMIT_OBJECTS PARK_LIMIT_PARKS PARK_MODS RANKED_MESSAGE ROLES_SCOPE TEAMS THREADS
UNRANKED_MESSAGE UNRANKED_SPEED_MESSAGE UNRANKED_TUNING_MESSAGE UPDATE_CHECK_MINUTES UPDATE_COUNTDOWN UPDATE_MODE
UPDATE_POLICY UPDATE_SCHEDULE USE_STEAM_RELAY VOTES_CUSTOM VOTE_EXTEND_MAP_ENABLED VOTE_EXTEND_MAP_PERCENT
VOTE_EXTEND_MAP_SECONDS WORDS_BLOCKED WORDS_FILTERED WORD_WARNINGS""".split())
TEXT_KEYS = frozenset(KEYS.values())


def editable():
    """Every key /setting-set takes, sorted."""
    return sorted((set(settings.LIVE) | RESTART_ONLY) - set(settings.IDENTITY))


def is_secret(key):
    return bool(SECRET.search(key))


def live(key):
    """True when a running server takes a change of `key` (the settings module types it)."""
    return key in settings.LIVE


def path(scope, secret=False):
    name = "common.env" if scope == "all" else f"server-{scope}.env"
    return os.path.join(DIR, "secrets", name) if secret else os.path.join(DIR, name)


def quote(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


def _read(file):
    try:
        with open(file, encoding="utf-8") as f:
            return f.read().splitlines()
    except OSError:
        return []


def _write(file, lines):
    os.makedirs(os.path.dirname(file), exist_ok=True)
    with open(file + ".tmp", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n" if lines else "")
    os.replace(file + ".tmp", file)


def _is(line, key):
    return line.strip().removeprefix("export ").lstrip().partition("=")[0].strip() == key


def set_key(scope, key, value):
    """Write `key` for "all" or a server number (the line is replaced); returns the key."""
    key = key.strip().upper()
    if key not in editable():
        raise ValueError(f"{key or 'That'} is not a setting that can be changed here.")
    value = value.strip()
    if not value or len(value) > MAX_VALUE or "\n" in value:
        raise ValueError(f"The value must be 1-{MAX_VALUE} characters on one line ('off' clears most settings).")
    if key in TEXT_KEYS and len(value.encode()) > MAX_BYTES:
        raise ValueError(f"A text is at most {MAX_BYTES} bytes.")
    file = path(scope, is_secret(key))
    lines = [ln for ln in _read(file) if not _is(ln, key)]
    lines.append(f"{key}={quote(value)}")
    _write(file, lines)
    if is_secret(key):
        os.chmod(file, 0o600)
    return key


def set_text(scope, name, text):
    """Write the text of `name` ("join", "ranked" ...) for "all" or a server number; "off" clears it in game."""
    return set_key(scope, KEYS[name], text)


def unset_key(scope, key):
    """Remove the line again: the server falls back to the shared value or the image's default."""
    key = key.strip().upper()
    file = path(scope, is_secret(key))
    lines = _read(file)
    kept = [ln for ln in lines if not _is(ln, key)]
    if len(kept) == len(lines):
        raise ValueError("Nothing set there.")
    _write(file, kept)
    return key


def unset(scope, name):
    return unset_key(scope, KEYS[name])


def current_keys(scope):
    """{key: value} the files for `scope` set (secrets masked)."""
    out = {}
    for secret in (False, True):
        for ln in _read(path(scope, secret)):
            key, sep, value = ln.strip().removeprefix("export ").lstrip().partition("=")
            if sep and key.strip() in editable():
                out[key.strip()] = "(secret)" if secret else value.strip().strip('"')
    return dict(sorted(out.items()))


def current(scope):
    """{name: text} the file for `scope` sets (raw, as written)."""
    out = {}
    for ln in _read(path(scope)):
        for name, key in KEYS.items():
            if _is(ln, key):
                out[name] = ln.partition("=")[2].strip().strip('"')
    return out
