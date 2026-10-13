"""The texts players see, edited from Discord: bot/bot.py's /message-* commands write them into the settings
files (/shared/settings/common.env for all servers, server-<n>.env for one; app/settings.py), where the settings
module types the join message into the running server and the leaderboard module picks up the Ranked/Unranked
texts. Only these keys can be edited here; other lines in the files are left as they are."""
import os

DIR = os.environ.get("SETTINGS_DIR") or "/shared/settings"
KEYS = {
    "join": "WELCOME_MESSAGE",
    "ranked": "RANKED_MESSAGE",
    "unranked-mods": "UNRANKED_MESSAGE",
    "unranked-tuning": "UNRANKED_TUNING_MESSAGE",
    "unranked-speed": "UNRANKED_SPEED_MESSAGE",
}
MAX_BYTES = 200


def path(scope):
    return os.path.join(DIR, "common.env" if scope == "all" else f"server-{scope}.env")


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


def set_text(scope, name, text):
    """Write the text of `name` ("join", "ranked" ...) for "all" or a server number; "off" clears it in game."""
    key = KEYS[name]
    text = text.strip()
    if not text or len(text.encode()) > MAX_BYTES:
        raise ValueError(f"The text must be 1-{MAX_BYTES} bytes ('off' switches it off).")
    file = path(scope)
    lines = [ln for ln in _read(file) if not _is(ln, key)]
    lines.append(f"{key}={quote(text)}")
    _write(file, lines)
    return key


def unset(scope, name):
    """Remove the line again: the server falls back to the shared text or the image's default."""
    key = KEYS[name]
    file = path(scope)
    lines = _read(file)
    kept = [ln for ln in lines if not _is(ln, key)]
    if len(kept) == len(lines):
        raise ValueError("Nothing set there.")
    _write(file, kept)
    return key


def current(scope):
    """{name: text} the file for `scope` sets (raw, as written)."""
    out = {}
    for ln in _read(path(scope)):
        for name, key in KEYS.items():
            if _is(ln, key):
                out[name] = ln.partition("=")[2].strip().strip('"')
    return out
