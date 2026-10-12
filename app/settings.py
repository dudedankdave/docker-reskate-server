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
    return d
