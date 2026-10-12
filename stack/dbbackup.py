"""Keeps a consistent copy of the leaderboard databases: every BACKUP_INTERVAL minutes (default 60)
it copies /shared/*.db with SQLite's online backup into /backup as <name>-<weekday><hour>.db
(so at most 7*24 files per database are kept, the oldest are overwritten). It never writes to /shared."""
import glob
import os
import sqlite3
import time

SRC, DST = "/shared", "/backup"
EVERY = max(1, int(os.environ.get("BACKUP_INTERVAL", "60"))) * 60

while True:
    for path in glob.glob(os.path.join(SRC, "*.db")):
        name = os.path.basename(path)[:-3]
        out = os.path.join(DST, time.strftime(name + "-%a%H.db", time.gmtime()))
        try:
            src = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
            dst = sqlite3.connect(out + ".tmp")
            src.backup(dst)
            dst.close()
            src.close()
            os.replace(out + ".tmp", out)
            print("backed up", path, "->", out, flush=True)
        except Exception as e:
            print("backup of", path, "failed:", e, flush=True)
    time.sleep(EVERY)
