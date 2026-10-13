"""Typing into a server's console and following its log, through the Docker socket.

The game containers keep stdin open (OpenStdin, with or without a TTY) and the image's supervisor reads
it, so what the Docker attach API writes lands in the console exactly like `docker attach`. The bot runs on the host, so
the plain unix socket is enough (a WebSocket upgrade is not needed). Only the write side is used;
the output is read with the log API (rollover.logs)."""
import socket

import rollover

# These stop or restart the server (or install an update). Restarts are only allowed on an empty
# server, so the bot's own restart command, not the console, is the way to do them (phase 3).
BLOCKED = {"quit", "exit", "stop", "restart", "shutdown", "update", "reload"}


def refused(line):
    """Why this line must not be sent, or None."""
    word = (line.strip().split(None, 1) or [""])[0].lower().lstrip("/")
    if not line.strip() or "\n" in line or "\r" in line:
        return "one non-empty line only"
    if word in BLOCKED:
        return f"`{word}` stops or restarts the server; restarts are only done on an empty server"
    return None


# Lines that only talk or ask; everything else (map, votes, kick, ban, roles, settings ...) can disturb players.
HARMLESS = {"say", "announce", "msg", "poll", "players", "help", "status", "version", "who", "list"}
DEFAULT_CUTOFF = 3     # players: the restart cutoff (David, 2026-10-12): up to 3 may be disturbed, more not


def disruptive(line):
    """True when the line is more than talking (it changes the game for the players on the server)."""
    word = (line.strip().split(None, 1) or [""])[0].lower().lstrip("/")
    return word not in HARMLESS


def needs_confirmation(line, players, cutoff=DEFAULT_CUTOFF):
    """The warning to show before typing `line` on a server with `players` on, or None."""
    if players is not None and players > cutoff and disruptive(line):
        return (f"**{players} players** are on this server (more than {cutoff}). "
                f"`{line[:150]}` can disturb them. Send it anyway?")
    return None


def send(info, line, timeout=10):
    """Type `line` into the container's console. Raises OSError/RuntimeError when it can't."""
    if rollover.DOCKER_API:
        raise RuntimeError("console input needs the Docker socket, not DOCKER_API")
    if not info["Config"].get("OpenStdin"):    # with or without a TTY: attach writes to the server's stdin either way
        raise RuntimeError("the container keeps no stdin open, so there is no console to type into")
    path = f"/v1.41/containers/{info['Id']}/attach?stream=1&stdin=1&stdout=0&stderr=0"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(rollover.SOCKET)
        sock.sendall((f"POST {path} HTTP/1.1\r\nHost: docker\r\nConnection: Upgrade\r\nUpgrade: tcp\r\n"
                      "Content-Length: 0\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(4096)
            if not chunk:
                break
            head += chunk
        status = head.split(b"\r\n", 1)[0].decode("latin-1")
        if " 101 " not in status and " 200 " not in status:
            raise RuntimeError(f"docker attach: {status or 'no answer'}")
        sock.sendall(line.encode("utf-8") + b"\n")
    finally:
        sock.close()
