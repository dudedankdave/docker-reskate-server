"""The ReSkate Discord bot (phase 1): live status cards and read-only leaderboard commands.

* Status cards: the same cards as status.py, but as a message of the bot itself, edited in place
  (BOT_STATUS_CHANNEL). The message id is remembered in BOT_STATE.
* Slash commands: /servers, /players, /top, /rank, /boards, /player, /stats.
  They read the shared leaderboard database (LEADERBOARD_FILE, default /shared/leaderboard.db)
  and the server logs; nothing here changes a server or the database.

DISCORD_TOKEN comes from the environment only. Without it the bot prints the cards and exits.
Settings: bot.json (see bot.example.json) or the upper-case env vars named in load_config().
"""
import asyncio
import json
import os
import sqlite3
import sys
import time
from contextlib import closing

import discord
from discord import app_commands

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import leaderboard as lb  # noqa: E402  (app/leaderboard.py, copied into the image)
import rollover  # noqa: E402
import status  # noqa: E402
import console  # noqa: E402  (bot/console.py: the console, through the Docker socket)
import feeds  # noqa: E402  (bot/feeds.py: log feeds)
import coach  # noqa: E402  (bot/coach.py: the /coach dialog)
import posts  # noqa: E402  (bot/posts.py: leaderboard messages and release news)

CONFIG = os.environ.get("BOT_CONFIG", "/config/bot.json")
STATE = os.environ.get("BOT_STATE", "/state/bot.json")
DB = os.environ.get("LEADERBOARD_FILE", "/shared/leaderboard.db")
INTERVAL = max(15, int(os.environ.get("STATUS_INTERVAL", "60")))
say = rollover.say


def load_config():
    cfg = {}
    try:
        with open(CONFIG, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        pass
    for key in ("status_channel", "guild"):
        if os.environ.get("BOT_" + key.upper()):
            cfg[key] = os.environ["BOT_" + key.upper()]
    return cfg


def db_path(league="ranked"):
    """The Ranked database (leaderboard.db) or the Unranked one beside it (leaderboard-unranked.db)."""
    return DB if league != "unranked" else os.path.join(os.path.dirname(DB), "leaderboard-unranked.db")


def read_db(fn, league="ranked"):
    """fn(cursor) on the leaderboard file of a league, opened read-only. None if it can't be read."""
    path = db_path(league)
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            return fn(db.cursor())
    except sqlite3.Error as e:
        say(f"cannot read {path}: {e}")
        return None


def table(rows):
    if not rows:
        return "Nobody has points yet."
    lines = [f"{i}. {r['name']} - {r['points']:,} pts ({r['wins']} wins, {r['played']} played)" for i, r in enumerate(rows, 1)]
    return "```\n" + "\n".join(lines) + "\n```"


class Bot(discord.Client):
    def __init__(self, cfg):
        intents = discord.Intents.default()
        intents.message_content = bool(cfg.get("console_chat"))   # privileged: enable it in the developer portal too
        super().__init__(intents=intents)
        self.cfg = cfg
        self.feeds = feeds.Feeds(self)
        self.feed_failed = {}
        self.tree = app_commands.CommandTree(self)
        with open(rollover.CONFIG, encoding="utf-8") as f:
            self.board = status.Board(json.load(f))
        self.state = {}
        try:
            with open(STATE, encoding="utf-8") as f:
                self.state = json.load(f)
        except (OSError, ValueError):
            pass
        add_commands(self)
        add_admin_commands(self)
        coach.add(self)

    async def setup_hook(self):
        try:
            if self.cfg.get("guild"):
                guild = discord.Object(int(self.cfg["guild"]))
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
            else:
                await self.tree.sync()
        except discord.HTTPException as e:   # e.g. 403: invited without the applications.commands scope
            say(f"could not register the slash commands ({e}); invite the bot with the applications.commands scope. Carrying on without them")
        if self.cfg.get("status_channel"):
            self.loop.create_task(self.cards())
        if self.cfg.get("servers"):
            self.loop.create_task(self.run_feeds())
        posts.start(self, read_db)

    async def on_ready(self):
        say(f"logged in as {self.user}")

    def save(self):
        try:
            os.makedirs(os.path.dirname(STATE) or ".", exist_ok=True)
            with open(STATE + ".tmp", "w", encoding="utf-8") as f:
                json.dump(self.state, f)
            os.replace(STATE + ".tmp", STATE)
        except OSError as e:
            say(f"could not save {STATE} ({e}); a restart would post a new status message")

    async def payload(self):
        message = await asyncio.to_thread(self.board.message)
        summary = dict(message["embeds"][-1], description=f"Updated <t:{int(time.time())}:R>")
        return [discord.Embed.from_dict(e) for e in message["embeds"][:-1] + [summary]]

    async def run_feeds(self):
        await self.wait_until_ready()
        while True:
            try:
                batches = await asyncio.to_thread(self.feeds.collect)
            except Exception as e:
                say(f"feeds: could not read the logs: {e!r}")
                batches = {}
            for (number, scope), lines in batches.items():   # one channel failing must not stop the others
                try:
                    await self.feeds.post(number, scope, lines)
                except Exception as e:
                    self.feed_error(number, scope, e)
            await asyncio.sleep(5)

    def feed_error(self, number, scope, error):
        """Log a failing channel at most every 10 minutes."""
        key, now = (number, scope), time.time()
        if now - self.feed_failed.get(key, 0) >= 600:
            self.feed_failed[key] = now
            say(f"feeds: server {number} {scope} channel failed ({error!r}); retrying silently for 10 min")

    def is_admin(self, member):
        roles = {str(r) for r in self.cfg.get("admin_roles") or []}
        return bool(roles & {str(r.id) for r in getattr(member, "roles", [])})

    async def running(self, number):
        """The running container of a server, or None."""
        found = await asyncio.to_thread(self.board.gather)
        return next((i for i in found.get(number, []) if i["State"].get("Running")), None)

    async def audit(self, number, who, line):
        """Tell the server's admin channel who typed what into the console."""
        channel = (self.cfg.get("servers", {}).get(number) or {}).get("admin")
        if channel:
            try:
                ch = self.get_channel(int(channel)) or await self.fetch_channel(int(channel))
                await ch.send(f"🛠 **{who}** typed into the console of server {number}: `{line[:300]}`",
                              allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException as e:
                say(f"audit post failed: {e}")

    async def on_message(self, message):
        if message.author.bot or not self.cfg.get("console_chat"):
            return
        for number, cfg in (self.cfg.get("servers") or {}).items():
            if cfg.get("console") and str(message.channel.id) == str(cfg["console"]):
                if not self.is_admin(message.author):
                    return
                why = console.refused(message.content)
                if why:
                    return await message.reply(f"Not sent: {why}.", mention_author=False)
                info = await self.running(number)
                if not info:
                    return await message.reply("That server is not running.", mention_author=False)
                try:
                    await asyncio.to_thread(console.send, info, message.content)
                    await message.add_reaction("✅")
                    await self.audit(number, message.author.display_name, message.content)
                except (OSError, RuntimeError) as e:
                    await message.reply(f"Could not type into the console: {e}", mention_author=False)

    async def cards(self):
        await self.wait_until_ready()
        channel = self.get_channel(int(self.cfg["status_channel"])) or await self.fetch_channel(int(self.cfg["status_channel"]))
        msg = None
        while True:
            try:
                embeds = await self.payload()
                if msg is None and self.state.get("message"):
                    try:
                        msg = await channel.fetch_message(int(self.state["message"]))
                    except discord.NotFound:
                        say("status message is gone, posting a new one")
                if msg is None:
                    msg = await channel.send(embeds=embeds)
                    self.state["message"] = str(msg.id)
                    self.save()
                else:
                    await msg.edit(embeds=embeds)
            except Exception as e:
                say(f"status cards failed: {e!r}")
                msg = None if isinstance(e, discord.NotFound) else msg
            await asyncio.sleep(INTERVAL)


def add_commands(bot):
    tree = bot.tree

    def server_choices(current):
        return [app_commands.Choice(name=f"{n} - {s.get('role', '')}", value=n)
                for n, s in bot.board.cfg["servers"].items() if current.lower() in f"{n} {s.get('role', '')}".lower()][:25]

    async def servers_ac(_i, current: str):
        return server_choices(current)

    @tree.command(name="servers", description="Status of all servers")
    async def servers(i: discord.Interaction):
        await i.response.defer()
        await i.followup.send(embeds=await bot.payload())

    @tree.command(name="players", description="Who is on a server (numbers; names are in the join/leave feed)")
    @app_commands.describe(server="Server number")
    @app_commands.autocomplete(server=servers_ac)
    async def players(i: discord.Interaction, server: str = ""):
        await i.response.defer()
        found = await asyncio.to_thread(bot.board.gather)
        lines = []
        for n, infos in found.items():
            if server and n != server:
                continue
            role = bot.board.cfg["servers"][n].get("role", f"Server {n}")
            running = [x for x in infos if x["State"].get("Running")]
            if not running:
                lines.append(f"**{role}**: offline")
                continue
            s = await asyncio.to_thread(bot.board.follow, running[0])
            lines.append(f"**{role}**: {s.players}/{s.max or '?'} on {s.map or '?'}")
        await i.followup.send("\n".join(lines) or "Unknown server.")

    leagues = app_commands.choices(league=[app_commands.Choice(name="Ranked", value="ranked"),
                                           app_commands.Choice(name="Unranked", value="unranked")])

    def board_for(cur, spec):
        return lb.Store.find(cur, spec or "", time.time())

    @tree.command(name="top", description="Leaderboard (all time by default)")
    @app_commands.describe(board="general, week, month, year, event, a week number, or add a mode: week skate", league="Ranked (default) or Unranked")
    @leagues
    async def top(i: discord.Interaction, board: str = "", league: str = "ranked"):
        def get(cur):
            b = board_for(cur, board)
            return (b, lb.Store.top(cur, b["id"], 10)) if b else None
        result = await asyncio.to_thread(read_db, get, league)
        if result is None:
            return await i.response.send_message("No such board, or the leaderboard is not available.", ephemeral=True)
        b, rows = result
        await i.response.send_message(f"**{b['name']}** ({league.title()})\n{table(rows)}")

    @tree.command(name="rank", description="Your or another player's rank")
    @app_commands.describe(player="Name (part of it) or SteamID", board="Board, as for /top", league="Ranked (default) or Unranked")
    @leagues
    async def rank(i: discord.Interaction, player: str, board: str = "", league: str = "ranked"):
        def get(cur):
            b = board_for(cur, board)
            p = find_player(cur, player)
            if not b or not p:
                return None
            return b, p, lb.Store.rank(cur, b["id"], p["steam_id"])
        result = await asyncio.to_thread(read_db, get, league)
        if result is None:
            return await i.response.send_message("No such player or board.", ephemeral=True)
        b, p, r = result
        if not r:
            return await i.response.send_message(f"{p['name']} has no points on {b['name']} yet.")
        place, total, row = r
        await i.response.send_message(f"**{p['name']}** is #{place} of {total} on {b['name']}: {row['points']:,} pts, {row['wins']} wins, {row['played']} played.")

    def find_player(cur, text):
        text = text.strip()
        return (cur.execute("SELECT * FROM players WHERE steam_id = ?", (text,)).fetchone()
                or cur.execute("SELECT * FROM players WHERE lower(name) = lower(?)", (text,)).fetchone()
                or cur.execute("SELECT * FROM players WHERE lower(name) LIKE '%' || lower(?) || '%' ORDER BY name LIMIT 1", (text,)).fetchone())

    @tree.command(name="boards", description="All boards")
    @app_commands.describe(league="Ranked (default) or Unranked")
    @leagues
    async def boards(i: discord.Interaction, league: str = "ranked"):
        def get(cur):
            return cur.execute("SELECT b.name, b.closed_at, COUNT(s.steam_id) n FROM boards b LEFT JOIN scores s "
                               "ON s.board = b.id AND s.points > 0 WHERE b.kind != 'mode' GROUP BY b.id "
                               "ORDER BY b.closed_at IS NOT NULL, b.created_at DESC LIMIT 25").fetchall()
        rows = await asyncio.to_thread(read_db, get, league)
        if rows is None:
            return await i.response.send_message("The leaderboard is not available.", ephemeral=True)
        await i.response.send_message("\n".join(f"{'🔒' if r['closed_at'] else '🟢'} **{r['name']}**: {r['n']} players" for r in rows) or "No boards yet.")

    @tree.command(name="player", description="A player's profile")
    @app_commands.describe(player="Name (part of it) or SteamID", league="Ranked (default) or Unranked")
    @leagues
    async def player(i: discord.Interaction, player: str, league: str = "ranked"):
        def get(cur):
            p = find_player(cur, player)
            if not p:
                return None
            ranks = []
            for b in cur.execute("SELECT * FROM boards WHERE kind IN ('general','year','month','week','event') "
                                 "ORDER BY closed_at IS NOT NULL, created_at DESC LIMIT 8").fetchall():
                r = lb.Store.rank(cur, b["id"], p["steam_id"])
                if r:
                    ranks.append((b["name"], r))
            recent = cur.execute("SELECT at, points, reason FROM ledger WHERE steam_id = ? AND board = 'general' "
                                 "ORDER BY id DESC LIMIT 5", (p["steam_id"],)).fetchall()
            return p, ranks, recent
        result = await asyncio.to_thread(read_db, get, league)
        if result is None:
            return await i.response.send_message("No such player.", ephemeral=True)
        p, ranks, recent = result
        e = discord.Embed(title=p["name"], color=status.BLURPLE)
        e.add_field(name="Ranks", value="\n".join(f"{n}: #{r[0]} of {r[1]} - {r[2]['points']:,} pts" for n, r in ranks) or "–", inline=False)
        e.add_field(name="Latest points", value="\n".join(f"<t:{int(r['at'])}:d> {r['points']:+d} {r['reason']}" for r in recent) or "–", inline=False)
        await i.response.send_message(embed=e)

    @tree.command(name="stats", description="Leaderboard totals")
    @app_commands.describe(league="Ranked (default) or Unranked")
    @leagues
    async def stats(i: discord.Interaction, league: str = "ranked"):
        def get(cur):
            now = time.time()
            return (cur.execute("SELECT COUNT(*) FROM players").fetchone()[0],
                    cur.execute("SELECT COALESCE(SUM(points),0) FROM ledger WHERE board='general'").fetchone()[0],
                    cur.execute("SELECT COUNT(*) FROM ledger WHERE board='general' AND at > ?", (now - 86400,)).fetchone()[0],
                    cur.execute("SELECT COUNT(DISTINCT steam_id) FROM ledger WHERE board='general' AND at > ?", (now - 7 * 86400,)).fetchone()[0])
        result = await asyncio.to_thread(read_db, get, league)
        if result is None:
            return await i.response.send_message("The leaderboard is not available.", ephemeral=True)
        players_n, points, today, week = result
        await i.response.send_message(f"**{players_n}** players known · **{points:,}** points handed out · **{today}** awards in the last 24 h · **{week}** players scored in the last 7 days")


def add_admin_commands(bot):
    tree = bot.tree

    async def servers_ac(_i, current: str):
        return [app_commands.Choice(name=f"{n} - {s.get('role', '')}", value=n)
                for n, s in bot.board.cfg["servers"].items() if current.lower() in f"{n} {s.get('role', '')}".lower()][:25]

    async def run(i, server, line):
        """Send one console line for an admin; answers the interaction."""
        if not bot.is_admin(i.user):
            return await i.response.send_message("Admins only.", ephemeral=True)
        if server not in bot.board.cfg["servers"]:
            return await i.response.send_message("Unknown server.", ephemeral=True)
        await i.response.defer(ephemeral=True)
        await i.followup.send(await coach.send_line(bot, i, server, line, i.user.display_name), ephemeral=True)

    guard = app_commands.default_permissions(manage_guild=True)

    @tree.command(name="say", description="Admin: say something in the server's chat")
    @guard
    @app_commands.autocomplete(server=servers_ac)
    async def say_cmd(i: discord.Interaction, server: str, text: str):
        await run(i, server, f"say {text}")

    @tree.command(name="announce", description="Admin: show an announcement to everyone on the server")
    @guard
    @app_commands.autocomplete(server=servers_ac)
    async def announce(i: discord.Interaction, server: str, text: str):
        await run(i, server, f"announce {text}")

    @tree.command(name="msg", description="Admin: private message to a player")
    @guard
    @app_commands.autocomplete(server=servers_ac)
    async def msg(i: discord.Interaction, server: str, player: str, text: str):
        await run(i, server, f"msg {player} {text}")

    @tree.command(name="kick", description="Admin: kick a player (until the server restarts)")
    @guard
    @app_commands.autocomplete(server=servers_ac)
    async def kick(i: discord.Interaction, server: str, player: str):
        await run(i, server, f"kick {player}")

    @tree.command(name="console", description="Admin: type any console command (not quit/stop/restart/update)")
    @guard
    @app_commands.autocomplete(server=servers_ac)
    async def console_cmd(i: discord.Interaction, server: str, command: str):
        await run(i, server, command)


def main():
    cfg = load_config()
    token = os.environ.get("DISCORD_TOKEN", "").strip()
    if not token:
        with open(rollover.CONFIG, encoding="utf-8") as f:
            print(json.dumps(status.Board(json.load(f)).message(), indent=2, ensure_ascii=False))
        say("no DISCORD_TOKEN: printed the cards, not starting the bot")
        return
    Bot(cfg).run(token, log_handler=None)


if __name__ == "__main__":
    main()
