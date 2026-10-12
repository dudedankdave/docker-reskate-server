"""Prints README.md for Docker Hub, whose description is limited to 25,000 bytes.

Docker Hub cannot follow links into the repository, so relative links (docs/ENVIRONMENT.md,
example.env, slim/, ...) are made absolute GitHub links. If the result is too long, the last
sections (Building, then Troubleshooting) are left out and replaced by a link to the full README.
Exits 1 if it is still too long.
"""
import re
import sys

LIMIT = 25000
REPO = "https://github.com/dudedankdave/docker-reskate-server"

readme = open("README.md", encoding="utf-8").read()


def absolute(match):
    target = match.group(2)
    if re.match(r"[a-z]+:|#|/", target):
        return match.group(0)
    kind = "tree" if target.endswith("/") else "blob"
    return f"{match.group(1)}({REPO}/{kind}/main/{target})"


hub = re.sub(r"(\]\s?)\(([^)\s]+)\)", absolute, readme)
for heading in ("Building", "Troubleshooting"):
    if len(hub.encode()) <= LIMIT:
        break
    # cut the section up to the next "## " heading (or the end) and its entry in the contents list
    hub = re.sub(rf"(?ms)^## {heading}\n.*?(?=^## |\Z)", "", hub)
    hub = re.sub(rf" · \[{heading}\]\(#{heading.lower()}\)", "", hub)
    hub = hub.rstrip("\n") + f"\n\n{heading} is described in the [full README]({REPO}#readme).\n"
size = len(hub.encode())
if size > LIMIT:
    sys.exit(f"Docker Hub README is {size} bytes, the limit is {LIMIT}")
sys.stdout.write(hub)
