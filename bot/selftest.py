"""Check the bot's rights in every channel of bot.json once it is logged in, and log one line per channel:

  selftest: server 1 admin #admin-1: OK
  selftest: server 2 log 1558...: cannot see the channel (403 Forbidden)
  selftest: leaderboards #leaderboards: missing Embed Links, Read Message History

So `docker logs reskate-bot | grep selftest` tells what to fix in Discord. It only reads; nothing is posted.
"""
import discord

import rollover

say = rollover.say

POST = ("view_channel", "send_messages", "embed_links", "read_message_history")
NEEDS = {   # what each kind of channel needs; everything posts, status/leaderboards edit their old messages
    "console": ("view_channel", "send_messages", "read_message_history", "add_reactions"),
}
NAMES = {"view_channel": "View Channel", "send_messages": "Send Messages", "embed_links": "Embed Links",
         "read_message_history": "Read Message History", "add_reactions": "Add Reactions"}


def channels(cfg):
    """(label, kind, channel id) for every channel set in bot.json."""
    found = [(key.replace("_channel", ""), key, cfg[key])
             for key in ("status_channel", "leaderboards_channel", "updates_channel") if cfg.get(key)]
    for number, scopes in (cfg.get("servers") or {}).items():
        for scope, cid in (scopes or {}).items():
            if cid:
                found.append((f"server {number} {scope}", scope, cid))
    return found


async def check(bot, cid, kind):
    """(channel name, problem or None)."""
    try:
        channel = bot.get_channel(int(cid)) or await bot.fetch_channel(int(cid))
    except ValueError:
        return str(cid), "not a channel id"
    except discord.HTTPException as e:   # 403 no View Channel, 404 wrong id
        return str(cid), f"cannot see the channel ({e.status} {e.text or e.response.reason})"
    name = f"#{getattr(channel, 'name', cid)}"
    guild = getattr(channel, "guild", None)
    me = guild and (guild.me or guild.get_member(bot.user.id))
    if not me:
        return name, "not a server channel, or the bot is not a member there"
    perms = channel.permissions_for(me)
    missing = [NAMES[p] for p in NEEDS.get(kind, POST) if not getattr(perms, p)]
    if kind == "console" and not bot.intents.message_content:
        missing.append('"console_chat": true in bot.json and the Message Content intent')
    return name, ("missing " + ", ".join(missing)) if missing else None


async def run(bot):
    found = channels(bot.cfg)
    if not found:
        return say("selftest: no channels in bot.json")
    bad = 0
    for label, kind, cid in found:
        try:
            name, problem = await check(bot, cid, kind)
        except Exception as e:
            name, problem = str(cid), f"check failed ({e!r})"
        bad += bool(problem)
        say(f"selftest: {label} {name}: {problem or 'OK'}")
    say(f"selftest: {len(found) - bad} of {len(found)} channels OK" + (", fix the rest in Discord and restart the bot" if bad else ""))
