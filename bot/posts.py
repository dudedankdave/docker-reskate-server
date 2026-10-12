"""Two things the bot posts by itself, each in its own channel (bot.json):

  leaderboards_channel  one message per board (All Time, this Month, this Week, every running Event),
                        edited in place when the points change (checked every LEADERBOARD_CHECK seconds)
  updates_channel       "ReSkate X is out." once per official ReSkate release (the launcher.json the
                        servers update from); the first run only remembers the current version

The message ids and the last seen release are kept in the bot's state file.
"""
import asyncio
import json
import os
import time

import discord

import leaderboard as lb
import rollover

CHECK = max(30, int(os.environ.get("LEADERBOARD_CHECK", "300")))    # seconds between two looks at the database
RELEASE_CHECK = 600
RELEASES = "https://github.com/Dingo-Shenanigans/ReSkate/releases"
BLURPLE, GREEN = 0x5865F2, 0x2ECC71


def board_embed(name, rows):
    body = "\n".join(f"{i}. {r['name']} - {r['points']:,} pts" + (f" ({r['wins']} wins)" if r["wins"] else "")
                     for i, r in enumerate(rows, 1)) or "Nobody has points yet."
    embed = discord.Embed(title=f"🏆 {name}", description=f"```\n{body}\n```"[:4000], color=BLURPLE)
    embed.set_footer(text="/top, /rank, /player in Discord · /top, /rank in game")
    return embed


def current_boards(read_db):
    """[(board id, name, top rows)] for the boards worth showing now."""
    def get(cur):
        now = time.time()
        out, seen = [], set()
        for spec in ("general", "month", "week"):
            b = lb.Store.find(cur, spec, now)
            if b and b["id"] not in seen:
                seen.add(b["id"])
                out.append((b["id"], b["name"], [dict(r) for r in lb.Store.top(cur, b["id"], 10)]))
        for b in cur.execute("SELECT id, name FROM boards WHERE kind = 'event' AND closed_at IS NULL ORDER BY created_at").fetchall():
            out.append((b["id"], b["name"], [dict(r) for r in lb.Store.top(cur, b["id"], 10)]))
        return out
    return read_db(get)


async def channel_of(bot, key):
    cid = bot.cfg.get(key)
    if not cid:
        return None
    return bot.get_channel(int(cid)) or await bot.fetch_channel(int(cid))


async def run_leaderboards(bot, read_db):
    await bot.wait_until_ready()
    shown = {}            # board id -> what was last posted
    while True:
        try:
            channel = await channel_of(bot, "leaderboards_channel")
            boards = await asyncio.to_thread(current_boards, read_db)
            ids = bot.state.setdefault("boards", {})
            for board_id, name, rows in boards or []:
                signature = json.dumps([name, rows], sort_keys=True)
                if shown.get(board_id) == signature:
                    continue
                embed = board_embed(name, rows)
                msg = None
                if ids.get(board_id):
                    try:
                        msg = await channel.fetch_message(int(ids[board_id]))
                    except discord.NotFound:
                        msg = None
                if msg:
                    await msg.edit(embed=embed)
                else:
                    ids[board_id] = str((await channel.send(embed=embed)).id)
                    bot.save()
                shown[board_id] = signature
        except Exception as e:
            rollover.say(f"leaderboard post failed: {e!r}")
        await asyncio.sleep(CHECK)


def release_notes(version):
    """The release's text from GitHub, or "" (best effort)."""
    try:
        data = json.loads(rollover.fetch(f"https://api.github.com/repos/Dingo-Shenanigans/ReSkate/releases/tags/{version}", 20))
        return (data.get("body") or "").strip()
    except Exception:
        return ""


async def run_updates(bot):
    await bot.wait_until_ready()
    while True:
        try:
            channel = await channel_of(bot, "updates_channel")
            version = await asyncio.to_thread(rollover.latest_release)
            known = bot.state.get("release")
            if known is None:
                bot.state["release"] = version
                bot.save()
            elif version != known and rollover.numbers(version) > rollover.numbers(known):
                notes = await asyncio.to_thread(release_notes, version)
                embed = discord.Embed(title=f"ReSkate {version} is out", url=f"{RELEASES}/tag/{version}",
                                      description=notes[:1500] or "A new ReSkate release is available.", color=GREEN)
                await channel.send(embed=embed)
                bot.state["release"] = version
                bot.save()
        except Exception as e:
            rollover.say(f"release check failed: {e!r}")
        await asyncio.sleep(RELEASE_CHECK)


def start(bot, read_db):
    if bot.cfg.get("leaderboards_channel"):
        bot.loop.create_task(run_leaderboards(bot, read_db))
    if bot.cfg.get("updates_channel"):
        bot.loop.create_task(run_updates(bot))
