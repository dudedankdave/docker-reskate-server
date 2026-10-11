"""Prints README.md for Docker Hub, whose description is limited to 25,000 bytes.

Docker Hub cannot follow links into the repository, so relative links (docs/ENVIRONMENT.md,
example.env, slim/, ...) are made absolute GitHub links; everything else is unchanged.
Exits 1 if the result is too long.
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
size = len(hub.encode())
if size > LIMIT:
    sys.exit(f"Docker Hub README is {size} bytes, the limit is {LIMIT}")
sys.stdout.write(hub)
