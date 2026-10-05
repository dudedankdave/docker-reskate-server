"""Installs the Thunderstore mods/maps listed in MODS into /data/Mods before the server starts.

MODS is a comma/space/newline separated list of
    Owner-Name                  latest version, kept up to date on every start
    Owner-Name-1.2.3            pinned to exactly that version
    https://thunderstore.io/c/reskate/p/Owner/Name/   (a package page URL, optionally .../1.2.3/)
Dependencies from a package's manifest.json are installed too (at the version they ask for).

Rules: only folders this installer created (marker .thunderstore.json) are ever replaced; a
folder you copied in by hand is left alone (and adopted, without a download, when its
manifest.json already has the wanted version). A failure is logged and never stops the
server from starting. The default image has no Python HTTPS or zip support, so this shells
out to curl and unzip.
"""
import json
import os
import re
import subprocess

MODS_DIR = "/data/Mods"
TMP_DIR = "/data/.mods-tmp"          # same volume as Mods, so renames are atomic
MARKER = ".thunderstore.json"
BASE = os.environ.get("THUNDERSTORE_URL", "https://thunderstore.io").rstrip("/")
MAX_UNPACKED = 8 * 1024 ** 3        # refuse zips that unpack to more than 8 GiB
ID = re.compile(r"^([A-Za-z0-9_]+)-([A-Za-z0-9_]+)(?:-(\d+\.\d+\.\d+))?$")
URL = re.compile(r"/(?:p|package)/(?:download/)?([A-Za-z0-9_]+)/([A-Za-z0-9_]+)(?:/(\d+\.\d+\.\d+))?")


def rmtree(path):
    """Recursive delete via rm: the image's minimal Python lacks the usual helper module."""
    subprocess.run(["rm", "-rf", path], check=False)


def log(message):
    print(f"[mods] {message}", flush=True)


def parse(token):
    m = (URL.search(token) if token.startswith(("http://", "https://")) else ID.match(token))
    return m.groups() if m else None


def get_json(url):
    run = subprocess.run(["curl", "-fsSL", "--retry", "3", "--retry-delay", "2", "-m", "30", url],
                         capture_output=True)
    if run.returncode != 0:
        raise RuntimeError(run.stderr.decode(errors="replace").strip() or f"curl exit {run.returncode}")
    return json.loads(run.stdout)


def meta(owner, name, version=None):
    url = f"{BASE}/api/experimental/package/{owner}/{name}/" + (f"{version}/" if version else "")
    data = get_json(url)
    return data["latest"] if not version and "latest" in data else data


def read_manifest(folder):
    try:
        with open(os.path.join(folder, "manifest.json"), encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def read_marker(folder):
    try:
        with open(os.path.join(folder, MARKER), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def version_key(v):
    return tuple(int(n) for n in re.findall(r"\d+", v or "")[:4])


def check_zip(path):
    """Refuse path traversal, symlinks/special files and zip bombs before anything is extracted."""
    names = subprocess.run(["unzip", "-Z1", path], capture_output=True)
    if names.returncode != 0:
        raise RuntimeError("not a valid zip file")
    for entry in names.stdout.decode("utf-8", "replace").splitlines():
        if entry.startswith("/") or ".." in entry.replace("\\", "/").split("/"):
            raise RuntimeError(f"unsafe path in zip: {entry!r}")
    listing = subprocess.run(["unzip", "-Z", path], capture_output=True).stdout.decode("utf-8", "replace")
    for line in listing.splitlines():
        if line[:1] in ("l", "c", "b", "p", "s"):
            raise RuntimeError("zip contains a link or special file")
    total = subprocess.run(["unzip", "-Zt", path], capture_output=True).stdout.decode()
    m = re.search(r"([\d,]+) bytes uncompressed", total)
    if m and int(m.group(1).replace(",", "")) > MAX_UNPACKED:
        raise RuntimeError("zip unpacks to more than 8 GiB")


def install(owner, name, version, url):
    target = os.path.join(MODS_DIR, name)
    work = os.path.join(TMP_DIR, name)
    rmtree(TMP_DIR)
    os.makedirs(work)
    archive = os.path.join(TMP_DIR, f"{name}-{version}.zip")
    log(f"{owner}-{name} {version}: downloading")
    run = subprocess.run(["curl", "-fsSL", "--retry", "3", "--retry-delay", "5", "-m", "3600", "-o", archive, url],
                         capture_output=True)
    if run.returncode != 0:
        raise RuntimeError("download failed: " + (run.stderr.decode(errors="replace").strip() or f"curl exit {run.returncode}"))
    check_zip(archive)
    run = subprocess.run(["unzip", "-q", archive, "-d", work], capture_output=True)
    if run.returncode != 0:
        raise RuntimeError("unzip failed: " + run.stderr.decode(errors="replace").strip())
    os.remove(archive)
    if not os.path.exists(os.path.join(work, "manifest.json")):
        raise RuntimeError("package has no manifest.json")
    with open(os.path.join(work, MARKER), "w", encoding="utf-8") as f:
        json.dump({"owner": owner, "name": name, "version": version}, f)
    backup = target + ".old"
    if os.path.isdir(target):
        rmtree(backup)
        os.rename(target, backup)
    os.makedirs(MODS_DIR, exist_ok=True)
    os.rename(work, target)
    rmtree(backup)
    rmtree(TMP_DIR)
    maps = []
    try:
        with open(os.path.join(target, "reskate-levels.json"), encoding="utf-8-sig") as f:
            maps = [lv["displayName"] for lv in json.load(f).get("levels", []) if "displayName" in lv]
    except (OSError, ValueError, KeyError):
        pass
    log(f"{owner}-{name} {version}: installed" + (f", maps: {', '.join(maps)} (use as MAP)" if maps else ""))


def ensure(owner, name, version, update, dependency=False, seen=None):
    seen = seen if seen is not None else set()
    if (owner, name) in seen:
        return
    seen.add((owner, name))
    label = f"{owner}-{name}"
    m = meta(owner, name, version)
    want = m["version_number"]
    target = os.path.join(MODS_DIR, name)
    have = read_manifest(target).get("version_number") if os.path.isdir(target) else None
    marker = read_marker(target) if os.path.isdir(target) else None
    if have == want:
        if marker is None:
            with open(os.path.join(target, MARKER), "w", encoding="utf-8") as f:
                json.dump({"owner": owner, "name": name, "version": want}, f)
        log(f"{label} {want}: up to date")
    elif have is not None and marker is None:
        log(f"{label}: {MODS_DIR}/{name} ({have}) was not installed by MODS, leaving it alone")
    elif have is not None and dependency:
        log(f"{label}: needs {want}, {have} is installed, leaving it")
    elif have is not None and version is None and not update:
        log(f"{label}: {have} installed, newer {want} available (MODS_UPDATE=false)")
    else:
        install(owner, name, want, m["download_url"])
    for dep in m.get("dependencies", []):
        d = ID.match(dep)
        if d:
            ensure(d.group(1), d.group(2), d.group(3), update, True, seen)


def install_all(spec, update=True):
    seen = set()
    for token in re.split(r"[\s,]+", spec.strip()):
        if not token:
            continue
        p = parse(token)
        if not p:
            log(f"{token}: not a valid package (use Owner-Name, Owner-Name-1.2.3 or a Thunderstore URL)")
            continue
        try:
            ensure(p[0], p[1], p[2], update, False, seen)
        except Exception as e:  # one bad package must not stop the others, or the server
            rmtree(TMP_DIR)
            log(f"{token}: {e}")


if __name__ == "__main__":  # docker exec <container> python3 /app/mods.py
    install_all(os.environ.get("MODS", ""),
                os.environ.get("MODS_UPDATE", "true").strip().lower() in ("1", "true", "yes", "on"))
