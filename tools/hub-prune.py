"""Deletes the tags of dudedankdave/reskate-server on Docker Hub that only clutter the tag list.

Every build pushes <ver>-r<n> and <ver>-r<n>-slim besides the moving tags, so each merge to main
that changes the image adds two tags. Only the newest revision of each ReSkate version is kept
(the rollover's image_updates looks for the newest one), the older ones go, and so does a
<ver>-rc tag once <ver> is out. Moving tags (<ver>, <major>.<minor>, latest, slim) are never touched.

  python3 tools/hub-prune.py [--dry-run] [repo]

Needs DOCKERHUB_USERNAME and DOCKERHUB_TOKEN (an access token allowed to delete) unless --dry-run.
"""
import json
import os
import re
import sys
import urllib.request

REVISION = re.compile(r"(\d+(?:\.\d+)+)-r(\d+)(-slim)?")
RC = re.compile(r"(\d+(?:\.\d+)+)-rc")


def stale(tags):
    """The tags to delete: older revisions of each version (and flavour), and -rc tags of released versions."""
    newest = {}
    for t in tags:
        if m := REVISION.fullmatch(t):
            key = (m.group(1), m.group(3))
            newest[key] = max(newest.get(key, 0), int(m.group(2)))
    out = []
    for t in tags:
        if m := REVISION.fullmatch(t):
            if int(m.group(2)) < newest[(m.group(1), m.group(3))]:
                out.append(t)
        elif (m := RC.fullmatch(t)) and m.group(1) in tags:
            out.append(t)
    return sorted(out)


def request(url, method="GET", data=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"JWT {token}"
    req = urllib.request.Request(url, method=method, headers=headers,
                                 data=json.dumps(data).encode() if data is not None else None)
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read()
    return json.loads(body) if body else None


def hub_tags(repo):
    url, names = f"https://hub.docker.com/v2/repositories/{repo}/tags?page_size=100", set()
    while url:
        page = request(url)
        names.update(t["name"] for t in page.get("results") or [])
        url = page.get("next")
    return names


def main(argv):
    dry = "--dry-run" in argv
    args = [a for a in argv if a != "--dry-run"]
    repo = args[0] if args else "dudedankdave/reskate-server"
    doomed = stale(hub_tags(repo))
    if not doomed:
        print("nothing to prune")
        return 0
    if dry:
        print("would delete:", " ".join(doomed))
        return 0
    token = request("https://hub.docker.com/v2/users/login", "POST",
                    {"username": os.environ["DOCKERHUB_USERNAME"], "password": os.environ["DOCKERHUB_TOKEN"]})["token"]
    failed = 0
    for tag in doomed:
        try:
            request(f"https://hub.docker.com/v2/repositories/{repo}/tags/{tag}/", "DELETE", token=token)
            print(f"deleted {repo}:{tag}")
        except Exception as e:  # one tag that cannot go must not stop the others
            print(f"::warning::cannot delete {repo}:{tag}: {e}")
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
