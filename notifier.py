"""Discord webhook sidecar, started by entrypoint.py when DISCORD_WEBHOOK is set.

Does two things, both through the same webhook:
  * forwards the server console (the lines the server also writes to
    /data/ReSkateServer.log) as batched code blocks; DISCORD_CONSOLE=false turns this off
  * posts "UPDATE AVAILABLE" once per new ReSkate GitHub release, mentioning the
    Discord user ids in DISCORD_MENTION_IDS (only that message can ping anyone)

It runs in a forked child, so the server keeps the console. Nothing here may disturb the
server: every failure is swallowed and written to /data/DiscordWebhook.log instead.
"""
import json
import os
import re
import subprocess
import time

LOG = "/data/ReSkateServer.log"
STATE = "/data/.discord-update-notified"
MODS_STATE = "/data/.discord-mods-notified"
NOTES = "/data/DiscordWebhook.log"
REPO = "Dingo-Shenanigans/ReSkate"
HUB = "dudedankdave/reskate-server"
CHECK_EVERY = 3 * 3600
FLUSH_AFTER = 3          # seconds a console line may wait to be batched
MAX_PENDING = 300        # lines kept while Discord is unreachable
DATE = re.compile(r"^\[\d{4}-\d{2}-\d{2} (\d{2}:\d{2}:\d{2})\]")


def note(message):
    try:
        if os.path.exists(NOTES) and os.path.getsize(NOTES) > 50_000:
            os.truncate(NOTES, 0)
        with open(NOTES, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except OSError:
        pass


def curl(args, payload=None, timeout=20):
    """Returns (http_code, body); (0, error text) when curl itself failed."""
    cmd = ["curl", "-sS", "-m", str(timeout), "-w", "\n%{http_code}", *args]
    try:
        run = subprocess.run(cmd, input=payload, capture_output=True, timeout=timeout + 5)
    except (OSError, subprocess.SubprocessError) as e:
        return 0, str(e)
    out = run.stdout.decode("utf-8", "replace")
    body, _, code = out.rpartition("\n")
    if run.returncode != 0:
        return 0, run.stderr.decode("utf-8", "replace").strip()
    return int(code) if code.isdigit() else 0, body


def post(url, payload):
    data = json.dumps(payload).encode("utf-8")
    for _ in range(4):
        code, body = curl(["-H", "Content-Type: application/json", "--data-binary", "@-", url], data)
        if 200 <= code < 300:
            return True
        if code == 429:
            try:
                wait = float(json.loads(body).get("retry_after", 2))
            except (ValueError, AttributeError):
                wait = 2
            time.sleep(min(max(wait, 1), 30))
            continue
        note(f"webhook post failed: HTTP {code} {body[:200]}")
        return False
    note("webhook post failed: still rate limited after retries")
    return False


def username():
    name = os.environ.get("DISCORD_USERNAME") or os.environ.get("SERVER_NAME") or ""
    # Discord rejects webhook names containing these words (server names often hold a discord.gg link)
    name = re.sub(r"(?i)\S*(discord|clyde)\S*", "", name)
    name = re.sub(r"\s+", " ", name)
    name = re.sub(r"(\s*\|\s*)+$", "", name).strip(" |-")
    return (name or "ReSkate server")[:80]


def send_console(url, name, lines):
    chunk = []
    size = 0
    for line in lines + [None]:
        if line is not None and size + len(line) + 1 <= 1850:
            chunk.append(line)
            size += len(line) + 1
            continue
        if chunk:
            post(url, {"username": name, "content": "```\n" + "\n".join(chunk) + "\n```",
                       "allowed_mentions": {"parse": []}})
        chunk, size = ([line], len(line) + 1) if line is not None else ([], 0)


def clean(line):
    line = DATE.sub(r"[\1]", line.rstrip("\r"))
    return line.replace("```", "'''")[:500]


def read_new(state):
    try:
        st = os.stat(LOG)
    except FileNotFoundError:
        return []
    if state["ino"] != st.st_ino or st.st_size < state["pos"]:
        state["ino"], state["pos"] = st.st_ino, 0      # log was replaced or truncated
    if st.st_size == state["pos"]:
        return []
    with open(LOG, "rb") as f:
        f.seek(state["pos"])
        data = f.read(st.st_size - state["pos"])
    end = data.rfind(b"\n")
    if end < 0:
        return []                                       # wait for the rest of the line
    state["pos"] += end + 1
    return [clean(line) for line in data[:end].decode("utf-8", "replace").splitlines() if line.strip()]


def console_loop(url, name):
    try:
        st = os.stat(LOG)
        state = {"ino": st.st_ino, "pos": st.st_size}   # only what the server writes from now on
    except FileNotFoundError:
        state = {"ino": None, "pos": 0}
    pending, first = [], 0.0
    while True:
        try:
            new = read_new(state)
            if new and not pending:
                first = time.time()
            pending += new
            if len(pending) > MAX_PENDING:
                pending = [f"... {len(pending) - MAX_PENDING} lines skipped"] + pending[-MAX_PENDING:]
            if pending and (time.time() - first >= FLUSH_AFTER or sum(map(len, pending)) > 1700):
                batch, pending = pending, []
                send_console(url, name, batch)
        except Exception as e:  # never let the sidecar die
            note(f"console loop: {e!r}")
        time.sleep(1)


def numbers(version):
    return tuple(int(n) for n in re.findall(r"\d+", version)[:4])


def read_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def check_once(url, name, mentions, running):
    code, body = curl(["-H", "User-Agent: reskate-server-image", "-H", "Accept: application/vnd.github+json",
                       f"https://api.github.com/repos/{REPO}/releases/latest"])
    if code != 200:
        note(f"release check failed: HTTP {code} {body[:200]}")
        return
    latest = json.loads(body)["tag_name"].lstrip("v")
    if numbers(latest) <= numbers(running):
        return
    published = curl(["-o", "/dev/null", f"https://hub.docker.com/v2/repositories/{HUB}/tags/{latest}"])[0] == 200
    state = read_state()
    if state.get("version") == latest and (state.get("published") or not published):
        return                                          # already announced, nothing new to say
    tags = " ".join(f"<@{i}>" for i in mentions)
    if published:
        how = ("Update with `docker compose pull && docker compose up -d`. "
               "The restart kicks everyone and changes the join code.")
    else:
        how = f"`{HUB}:{latest}` is not on Docker Hub yet, the image has to be built first."
    if state.get("version") == latest:                  # earlier message said "not published yet"
        text = f"{tags} **Image available**: `{HUB}:{latest}` is on Docker Hub now. {how}"
    else:
        text = (f"{tags} **UPDATE AVAILABLE**: ReSkate **{latest}** is out, this server runs **{running}**. "
                f"Players on the new version can't join until the server is updated.\n{how}\n"
                f"https://github.com/{REPO}/releases/tag/v{latest}")
    payload = {"username": name, "content": text.strip(),
               "allowed_mentions": {"parse": [], "users": list(mentions)}}
    if post(url, payload):
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump({"version": latest, "published": published}, f)


def post_mod_events(url, name):
    """Announce the installs/updates mods.py did during this start, then forget them."""
    import mods
    try:
        with open(mods.EVENTS, encoding="utf-8") as f:
            events = [json.loads(line) for line in f if line.strip()]
        os.remove(mods.EVENTS)
    except (OSError, ValueError):
        return
    for e in events:
        label = f"{e['owner']}-{e['name']}"
        maps = f" Maps: {', '.join(e['maps'])}." if e.get("maps") else ""
        if e.get("from"):
            text = f"**MOD UPDATED**: {label} {e['from']} to **{e['to']}**.{maps}"
        else:
            text = f"**MOD INSTALLED**: {label} **{e['to']}**.{maps}"
        post(url, {"username": name, "content": text, "allowed_mentions": {"parse": []}})


def check_mods(url, name, mentions):
    """Announce (once per version) newer Thunderstore versions of the unpinned MODS entries."""
    import mods
    spec = os.environ.get("MODS", "")
    update = os.environ.get("MODS_UPDATE", "true").strip().lower() in ("1", "true", "yes", "on")
    try:
        with open(MODS_STATE, encoding="utf-8") as f:
            seen = json.load(f)
    except (OSError, ValueError):
        seen = {}
    tags = " ".join(f"<@{i}>" for i in mentions)
    for owner, pkg, latest, have in mods.pending_updates(spec):
        key = f"{owner}-{pkg}"
        if seen.get(key) == latest:
            continue
        how = ("Restart the server to install it. The restart kicks everyone and changes the join code."
               if update else "`MODS_UPDATE` is false, so it will not be installed automatically.")
        text = (f"{tags} **MOD UPDATE AVAILABLE**: {key} **{latest}** is out, this server has **{have}**. {how}\n"
                f"https://thunderstore.io/c/reskate/p/{owner}/{pkg}/")
        if post(url, {"username": name, "content": text.strip(),
                      "allowed_mentions": {"parse": [], "users": list(mentions)}}):
            seen[key] = latest
            with open(MODS_STATE, "w", encoding="utf-8") as f:
                json.dump(seen, f)


def mods_loop(url, name, mentions):
    time.sleep(5)
    try:
        post_mod_events(url, name)
    except Exception as e:
        note(f"mod events: {e!r}")
    time.sleep(85)
    while True:
        try:
            check_mods(url, name, mentions)
        except Exception as e:
            note(f"mod update check: {e!r}")
        time.sleep(CHECK_EVERY)


def update_loop(url, name, mentions, running):
    time.sleep(60)
    while True:
        try:
            check_once(url, name, mentions, running)
        except Exception as e:
            note(f"update check: {e!r}")
        time.sleep(CHECK_EVERY)


def main():
    import threading
    url = os.environ["DISCORD_WEBHOOK"].strip()
    name = username()
    mentions = [i.strip() for i in os.environ.get("DISCORD_MENTION_IDS", "").split(",") if i.strip()]
    running = os.environ.get("RESKATE_IMAGE_VERSION", "")
    if os.environ.get("MODS", "").strip():
        import threading
        threading.Thread(target=mods_loop, args=(url, name, mentions), daemon=True).start()
    if re.fullmatch(r"\d+(\.\d+)+", running):
        threading.Thread(target=update_loop, args=(url, name, mentions, running), daemon=True).start()
    else:
        note(f"update check off: image version {running!r} is not a release number")
    if os.environ.get("DISCORD_CONSOLE", "true").strip().lower() in ("1", "true", "yes", "on"):
        console_loop(url, name)
    else:
        while True:
            time.sleep(3600)
