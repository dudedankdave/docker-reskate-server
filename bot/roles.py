"""One codebase, three Discord bots: which jobs an instance does is set by its role (BOT_ROLE or "role" in bot.json).

  stats  (Coach Frank)  status cards, leaderboard posts, release news, the read-only commands (/servers /players /top ...)
  chat   (Vee)          the public and chat feeds: joins, leaves, map changes, in-game chat
  admin  (Meat Man)     the admin and log feeds, the console (/say /kick /coach /setting-* ...), console channels, audit posts
  all    (default)      everything in one bot, as before the split

Every instance has its own DISCORD_TOKEN and its own BOT_STATE file. They share bot.json (channel ids, admin roles).
"""
import os

ROLES = {
    "stats": {"stats": True, "console": False, "scopes": ()},
    "chat": {"stats": False, "console": False, "scopes": ("public", "chat")},
    "admin": {"stats": False, "console": True, "scopes": ("admin", "log")},
    "all": {"stats": True, "console": True, "scopes": ("public", "chat", "admin", "log")},
}


def role_of(cfg):
    """The role's name; an unknown one is an error, so a typo does not start a bot that does nothing."""
    name = (os.environ.get("BOT_ROLE") or cfg.get("role") or "all").strip().lower()
    if name not in ROLES:
        raise SystemExit(f"BOT_ROLE {name!r} is not one of {', '.join(ROLES)}")
    return name


def jobs(cfg):
    return ROLES[role_of(cfg)]
