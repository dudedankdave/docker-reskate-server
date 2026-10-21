"""Entry point of the tools image (dudedankdave/reskate-tools): one helper per container, chosen by
RESKATE_MODE or the first argument (the rest goes to the helper).

  bridge          servers <-> everything else (the only one with the Docker socket besides the rollover)
  rollover        new ReSkate releases, map updates, settings and image revisions, on empty servers
  bot             the Discord bot
  dashboard       the password-protected web page to watch servers, rollover, bridge and bots
  dockerproxy     a filtering Docker socket for the dashboard (only the calls it makes, game servers only)
  leaderboard     the leaderboard of the servers that hand it over (/shared/modules.json)
  webhooks        their Discord webhooks
  settings        their settings changes, typed into the running servers; roles reload
  announcer       their kick and ban announcements
  backup          hourly backups of the leaderboard databases
  split-settings  today's server containers' env out as settings files
  status          the old status cards through a webhook (the bot has them now)"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MODES = {
    "bridge": "bridge.py", "rollover": "rollover.py", "bot": "bot.py", "dashboard": "dashboard.py", "dockerproxy": "dockerproxy.py",
    "leaderboard": "mod_leaderboard.py",
    "webhooks": "mod_webhooks.py", "settings": "mod_settings.py", "announcer": "mod_announcer.py",
    "backup": "dbbackup.py", "split-settings": "split_settings.py", "status": "status.py",
}


def main():
    args = sys.argv[1:]
    if args and args[0].strip().lower() in MODES:
        mode, args = args[0].strip().lower(), args[1:]
    else:
        mode = os.environ.get("RESKATE_MODE", "").strip().lower()
    if mode not in MODES:
        sys.exit(f"RESKATE_MODE (or the first argument): one of {', '.join(MODES)}; got {mode!r}\n\n{__doc__}")
    os.execv(sys.executable, [sys.executable, "-I", os.path.join(HERE, MODES[mode]), *args])


if __name__ == "__main__":
    main()
