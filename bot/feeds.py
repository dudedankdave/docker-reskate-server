"""Discord feeds from the server logs, replacing the webhooks one scope at a time.

bot.json: "servers": {"1": {"public": id, "chat": id, "admin": id, "log": id, "console": id}, ...}
  public   joins, leaves, finished throwdowns, map changes
  chat     in-game chat
  admin    kicks, bans, anti-cheat, commands typed in game, warnings and errors (+ who used the bot's console)
  log      every console line but chat
  console  admin channel whose messages are typed into the console (needs "console_chat": true and the
           Message Content intent; only members with an admin role)
A scope with no channel is skipped. Lines are collected for a few seconds and posted as one message.
"""
import re

import discord

import rollover
from status import STAMP, UP, MAP

LINE = rollover.LINE
CHAT = re.compile(r"^\[chat\] (.*)$")
JOIN = re.compile(r"^(.+) joined \(\d+(?:, admin)?\), \d+/\d+ players")
LEAVE = re.compile(r"^(.+?) left \(.*\)(?: \[.*\])?$")
THROW = re.compile(r"^\[throwdown\] .+ has finished: ")
ADMIN = re.compile(r"^\[(?:kick|ban|anticheat|command|words|spam)\]|\b(?:kicked|banned|warning|error)\b", re.I)
LIMIT = 1900


def classify(text):
    """The scopes a console line (without its time stamp) belongs to."""
    m = LINE.match(text)
    body = m.group(1) if m else text
    if CHAT.match(body):
        return {"chat"}
    scopes = {"log"}
    if JOIN.match(body) or LEAVE.match(body) or THROW.match(body) or MAP.match(body):
        scopes.add("public")
    if ADMIN.search(body) or JOIN.match(body) or LEAVE.match(body):
        scopes.add("admin")
    return scopes


def show(text):
    m = LINE.match(text)
    body = m.group(1) if m else text
    chat = CHAT.match(body)
    return (chat.group(1) if chat else body)


class Feeds:
    def __init__(self, bot):
        self.bot = bot
        self.servers = bot.cfg.get("servers") or {}
        self.last = {}          # container id -> (started, (sec, nsec)) of the last line seen

    def read(self, info):
        """New (scope, text) pairs from one container since the last call."""
        key, started = info["Id"], info["State"].get("StartedAt", "")
        seen = self.last.get(key)
        first = seen is None or seen[0] != started
        since = None if first else f"{seen[1][0]}.{seen[1][1]:09d}"
        pos = (0, 0) if first else seen[1]
        out = []
        import time
        from datetime import datetime, timezone
        for line in rollover.logs(info, since=since, timestamps=True):
            m = STAMP.match(line)
            if not m:
                continue
            at = (int(datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()),
                  int((m.group(2) or "0").ljust(9, "0")[:9]))
            if at <= pos:
                continue
            pos = max(pos, at)
            if not first:               # a freshly seen container: remember where it is, replay nothing
                out.append(m.group(3))
        self.last[key] = (started, pos if pos != (0, 0) else (int(time.time()), 0))
        return out

    def collect(self):
        """{(server, scope): [text, ...]} for every running container with new lines."""
        found = self.bot.board.gather()
        posts = {}
        for number, infos in found.items():
            cfg = self.servers.get(number) or {}
            if not any(cfg.get(s) for s in ("public", "chat", "admin", "log")):
                continue
            for info in infos:
                if not info["State"].get("Running"):
                    continue
                for text in self.read(info):
                    for scope in classify(text):
                        if cfg.get(scope):
                            posts.setdefault((number, scope), []).append(show(text))
        return posts

    async def post(self, number, scope, lines):
        channel_id = int(self.servers[number][scope])
        channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
        role = self.bot.board.cfg["servers"].get(number, {}).get("role", f"Server {number}")
        text, chunks = "", []
        for line in lines:
            line = discord.utils.escape_markdown(line)[:400]
            if len(text) + len(line) + 1 > LIMIT:
                chunks.append(text)
                text = ""
            text += line + "\n"
        if text:
            chunks.append(text)
        for chunk in chunks[:5]:
            await channel.send(f"**{role}**\n{chunk}" if scope in ("admin", "log") else chunk,
                               allowed_mentions=discord.AllowedMentions.none())
