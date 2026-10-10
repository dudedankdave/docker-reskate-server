"""Prints README.md for Docker Hub, whose description is limited to 25,000 bytes.

The environment variable tables (### Per server up to ### Data) are replaced by a link to
them on GitHub; everything else is unchanged. Exits 1 if the result is still too long.
"""
import sys

LIMIT = 25000
REPO = "https://github.com/dudedankdave/docker-reskate-server"

readme = open("README.md", encoding="utf-8").read()
start, end = readme.index("### Per server\n"), readme.index("### Data\n")
link = (f"**All environment variables** (per server, access, gameplay, anti-cheat, voice, network, "
        f"voting, announcements, parks and layers, mods, updates) are listed in the "
        f"[README on GitHub]({REPO}#per-server) and in [`example.env`]({REPO}/blob/main/example.env).\n\n\n")
hub = readme[:start] + link + readme[end:]
size = len(hub.encode())
if size > LIMIT:
    sys.exit(f"Docker Hub README is {size} bytes, the limit is {LIMIT}")
sys.stdout.write(hub)
