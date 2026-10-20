"""Discord feeds from the server logs, replacing the webhooks one scope at a time.

bot.json: "servers": {"1": {"public": id, "chat": id, "admin": id, "log": id, "console": id}, ...}
  public   joins, leaves, finished throwdowns, map changes, server up (cleaned up like the public webhook:
           no Steam IDs, no leave reasons)
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
from status import UP, MAP

LINE = rollover.LINE
CHAT = re.compile(r"^\[chat\] (.*)$")
JOIN = re.compile(r"^(.+) joined \(\d+(?:, admin)?\), (\d+/\d+) players")
LEAVE = re.compile(r"^(.+?) left \(.*\)(?: \[.*\])?$")
THROW = re.compile(r"^\[throwdown\] .+ has finished: ")
ADMIN = re.compile(r"^\[(?:kick|ban|anticheat|command|words|spam)\]|\b(?:kicked|banned|warning|error)\b", re.I)
EVENT = re.compile(r"^\[event\] [a-z-]+ \{")   # the image's own messages, see rollover/bridge.py
LIMIT = 1900
RAW = "\x00"                                    # marks a line that is posted as it is (already Markdown)


def classify(text):
    """The scopes a console line (without its time stamp) belongs to."""
    m = LINE.match(text)
    body = m.group(1) if m else text
    if EVENT.match(body):
        return set()               # not a console line; the bot takes them from the bridge later
    if CHAT.match(body):
        return {"chat"}
    scopes = {"log"}
    if JOIN.match(body) or LEAVE.match(body) or THROW.match(body) or MAP.match(body) or UP.match(body):
        scopes.add("public")
    if ADMIN.search(body) or JOIN.match(body) or LEAVE.match(body):
        scopes.add("admin")
    return scopes


def show(text, scope=""):
    """A line as the scope posts it. public is for players, like the public webhook: a join reads
    "Name joined, 3/100 players", a leave "Name left" (no Steam IDs, admin flag or leave reason)."""
    m = LINE.match(text)
    body = m.group(1) if m else text
    chat = CHAT.match(body)
    if chat:
        return chat.group(1)
    if scope == "public":
        if j := JOIN.match(body):
            return f"{j.group(1)} joined, {j.group(2)} players"
        if left := LEAVE.match(body):
            return f"{left.group(1)} left"
        if up := UP.match(body):
            return f"Up on {up.group(1)} for {up.group(2)} players."
    return body


class Feeds:
    def __init__(self, bot):
        self.bot = bot
        self.servers = bot.cfg.get("servers") or {}
        self.scopes = getattr(bot, "jobs", {}).get("scopes", ("public", "chat", "admin", "log"))   # the bot's role, see roles.py

    def collect(self):
        """{(server, scope): [text, ...]} of what came in since the last call (console lines, and through the
        bridge also the image's own messages: admin and updates-admin to admin, public to public)."""
        posts = {}
        for number, _slot, kind, scopes, text, ping in self.bot.backend.lines():
            cfg = self.servers.get(number) or {}
            if not any(cfg.get(s) for s in self.scopes):
                continue
            if kind == "message":
                scope = {"admin": "admin", "updates-admin": "admin", "public": "public"}.get((scopes or [""])[0])
                if scope in self.scopes and cfg.get(scope):
                    posts.setdefault((number, scope), []).append(RAW + text)   # the image's own Markdown
                continue
            if kind != "console":
                continue
            for scope in classify(text):
                if scope in self.scopes and cfg.get(scope):
                    posts.setdefault((number, scope), []).append(show(text, scope))
        return posts

    async def post(self, number, scope, lines):
        channel_id = int(self.servers[number][scope])
        channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
        role = self.bot.backend.servers().get(number, {}).get("role", f"Server {number}")
        text, chunks = "", []
        for line in lines:
            line = line[1:][:1500] if line.startswith(RAW) else discord.utils.escape_markdown(line)[:400]
            if len(text) + len(line) + 1 > LIMIT:
                chunks.append(text)
                text = ""
            text += line + "\n"
        if text:
            chunks.append(text)
        for chunk in chunks[:5]:
            await channel.send(f"**{role}**\n{chunk}" if scope in ("admin", "log") else chunk,
                               allowed_mentions=discord.AllowedMentions.none())
