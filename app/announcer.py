"""Kick and ban announcements in chat, started by entrypoint.py unless ANNOUNCE_KICKS=false.

ReSkate tells only the admin who kicks or bans someone; the other players just see them gone.
This reads the lines the server writes to /data/ReSkateServer.log and, for each admin kick or
ban, types `say <name> was kicked.` / `say <name> was banned.` into the server console, which
the server sends to everyone in chat as "Server".

It runs as a thread beside supervisor.py and types through Supervisor.send(), the same way the
notifier does its countdowns. Nothing here may disturb the server: every failure is swallowed.
"""
import os
import re
import threading
import time

LOG = "/data/ReSkateServer.log"
DATE = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ")
# The server's replies to `kick` / `ban` (Server/server_commands.cpp). Vote kicks are not
# matched: the server already says those in chat. A tagged line ("[chat] Server: ...", our own
# `say`) never matches, so nothing is announced twice.
KICKED = re.compile(r"^([^\[].*) was kicked until the server restarts\.$")
BANNED = re.compile(r"^([^\[].*) was banned\.$")


def announcement(line):
    """The chat line for one server log line, or None."""
    text = DATE.sub("", line.rstrip("\r\n"), count=1)
    if text == line.rstrip("\r\n"):
        return None                                     # not a log line
    for pattern, what in ((KICKED, "kicked"), (BANNED, "banned")):
        m = pattern.match(text)
        if m:
            return f"{m.group(1).strip()} was {what}."
    return None


def follow(send):
    # Start at the end: kicks from before this start were announced (or not) back then.
    try:
        st = os.stat(LOG)
        state = {"ino": st.st_ino, "pos": st.st_size}
    except FileNotFoundError:
        state = {"ino": None, "pos": 0}
    while True:
        time.sleep(1)
        try:
            st = os.stat(LOG)
        except FileNotFoundError:
            continue
        if state["ino"] != st.st_ino or st.st_size < state["pos"]:
            state["ino"], state["pos"] = st.st_ino, 0     # log was replaced or truncated
        if st.st_size == state["pos"]:
            continue
        try:
            with open(LOG, "rb") as f:
                f.seek(state["pos"])
                data = f.read(st.st_size - state["pos"])
        except OSError:
            continue
        end = data.rfind(b"\n")
        if end < 0:
            continue                                    # wait for the rest of the line
        state["pos"] += end + 1
        for line in data[:end].decode("utf-8", "replace").splitlines():
            text = announcement(line)
            if text:
                send(f"say {text}")                     # False while the server restarts: skip


def start(send):
    def run():
        try:
            follow(send)
        except Exception:  # never take the server down with us
            pass
    threading.Thread(target=run, daemon=True).start()
