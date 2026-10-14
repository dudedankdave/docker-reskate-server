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

Shared store (MODS_STORE, default `shared` = the volume mounted at /mods, or a path for a store
of this server's own): every package version is downloaded once into
<store>/packages/<Owner>-<Name>-<version> and each server only gets a symlink
/data/Mods/<Name> to the version it wants. A version is never changed after it is written, so a
server still running (or draining players) on an older map keeps its files while another one
already moved on, and a lock per version makes servers starting together wait for one download
instead of repeating it. `python3 /app/mods.py gc [days]` removes versions no server has
started with for that long (never the newest of a package).
"""
import json
import os
import re
import subprocess
import sys
import time

try:
    import fcntl
except ImportError:  # no locking: fine while only one server installs
    fcntl = None

MODS_DIR = "/data/Mods"
TMP_DIR = "/data/.mods-tmp"          # same volume as Mods, so renames are atomic
MARKER = ".thunderstore.json"
EVENTS = "/data/.mods-events"        # install/update events, posted to Discord by notifier.py
BASE = os.environ.get("THUNDERSTORE_URL", "https://thunderstore.io").rstrip("/")

def max_gb():
    """MODS_MAX_GB: largest download and unpacked size of one package in GiB, 0 = no limit."""
    raw = os.environ.get("MODS_MAX_GB", "").strip()
    try:
        value = float(raw) if raw else 8.0
        if value < 0:
            raise ValueError
        return value
    except ValueError:
        print(f"[mods] MODS_MAX_GB={raw!r} is not a number of GiB, using 8", flush=True)
        return 8.0


MAX_GB = max_gb()
MAX_UNPACKED = int(MAX_GB * 1024 ** 3)   # 0 = no limit
LIMIT = f"{MAX_GB:g} GiB (MODS_MAX_GB)"
ID = re.compile(r"^([A-Za-z0-9_]+)-([A-Za-z0-9_]+)(?:-(\d+\.\d+\.\d+))?$")
URL = re.compile(r"/(?:p|package)/(?:download/)?([A-Za-z0-9_]+)/([A-Za-z0-9_]+)(?:/(\d+\.\d+\.\d+))?")


SHARED = "/mods"                     # where the shared volume is mounted


def store_setting():
    """MODS_STORE: `shared` (default) = the volume at /mods, used only while one is mounted there;
    an absolute path = a store at that path for this server, used as it is."""
    raw = os.environ.get("MODS_STORE", "").strip()
    if raw.lower() in ("", "shared"):
        return SHARED, True
    path = os.path.normpath(raw)
    if path.startswith("/") and path != "/" and not (path + "/").startswith(MODS_DIR + "/"):
        return path, False
    print(f"[mods] MODS_STORE={raw!r} is neither 'shared' nor an absolute path outside {MODS_DIR}, using shared",
          flush=True)
    return SHARED, True


STORE, NEEDS_MOUNT = store_setting()
PACKAGES = STORE + "/packages"
STORE_TMP = STORE + "/.tmp"
LOCK_WAIT = 4000                     # seconds to wait for another server's download of the same version


def shared():
    """True when packages go into the store: always for a path, for `shared` only while a volume
    is mounted at /mods (else each server keeps its own copies in /data/Mods)."""
    return not NEEDS_MOUNT or os.path.ismount(STORE)


class Lock:
    """An exclusive lock on a file in the store (a no-op without fcntl)."""

    def __init__(self, name):
        os.makedirs(STORE + "/.locks", exist_ok=True)
        self.path = f"{STORE}/.locks/{name}.lock"

    def __enter__(self):
        self.file = open(self.path, "a")
        if fcntl:
            deadline = time.time() + LOCK_WAIT
            while True:
                try:
                    fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.time() > deadline:
                        self.file.close()
                        raise RuntimeError("another server has been installing this for too long")
                    time.sleep(2)
        return self

    def __exit__(self, *exc):
        self.file.close()                # closing releases the lock


def rmtree(path):
    """Recursive delete via rm: the image's minimal Python lacks the usual helper module."""
    subprocess.run(["rm", "-rf", path], check=False)


def record_event(owner, name, old, new, maps):
    """Hand an install/update over to the Discord sidecar (only when a webhook is set)."""
    if not (os.environ.get("DISCORD_WEBHOOK_ADMIN") or os.environ.get("DISCORD_WEBHOOK")):
        return
    try:
        with open(EVENTS, "a", encoding="utf-8") as f:
            f.write(json.dumps({"owner": owner, "name": name, "from": old, "to": new, "maps": maps}) + "\n")
    except OSError:
        pass


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
    if MAX_UNPACKED and m and int(m.group(1).replace(",", "")) > MAX_UNPACKED:
        raise RuntimeError(f"zip unpacks to more than {LIMIT}")


def unpack(owner, name, version, url, work):
    """Download the package and unpack it into the empty folder `work`, with the marker written."""
    archive = work + ".zip"
    log(f"{owner}-{name} {version}: downloading")
    cap = ["--max-filesize", str(MAX_UNPACKED)] if MAX_UNPACKED else []
    run = subprocess.run(["curl", "-fsSL", "--retry", "3", "--retry-delay", "5", "-m", "3600", *cap, "-o", archive, url],
                         capture_output=True)
    if run.returncode == 63:
        raise RuntimeError(f"download is larger than {LIMIT}")
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


def stored(owner, name, version):
    return f"{PACKAGES}/{owner}-{name}-{version}"


def clean_store_tmp():
    """Leftovers of installs that died half way (nothing else uses these folders after an hour)."""
    try:
        for entry in os.scandir(STORE_TMP):
            if time.time() - entry.stat().st_mtime > 3600 * 2:
                rmtree(entry.path)
    except OSError:
        pass


def fill_store(owner, name, version, url, adopt=None):
    """Make sure the shared store holds this version, downloading it once however many servers ask.
    `adopt` is a folder this installer wrote earlier with exactly this version: copied, not downloaded."""
    final = stored(owner, name, version)
    with Lock(f"{owner}-{name}-{version}"):
        if os.path.exists(os.path.join(final, MARKER)):      # another server finished while we waited
            return final
        os.makedirs(PACKAGES, exist_ok=True)
        os.makedirs(STORE_TMP, exist_ok=True)
        clean_store_tmp()
        work = f"{STORE_TMP}/{owner}-{name}-{version}.{os.getpid()}"
        rmtree(work)
        if adopt:
            run = subprocess.run(["cp", "-a", adopt, work], capture_output=True)
            if run.returncode != 0:
                rmtree(work)
                raise RuntimeError("copying into the shared store failed: " + run.stderr.decode(errors="replace").strip())
        else:
            os.makedirs(work)
            unpack(owner, name, version, url, work)
        rmtree(final)                                         # a folder without marker is a dead leftover
        os.rename(work, final)                                # complete or absent, never half
    return final


def touch(folder):
    """Mark a store folder as in use right now (what gc goes by)."""
    try:
        os.utime(folder)
    except OSError:
        pass


def link(target, final):
    """Point /data/Mods/<name> at a store folder; an earlier symlink is swapped atomically, a folder
    this installer made earlier (it has the marker) is moved aside and removed."""
    os.makedirs(MODS_DIR, exist_ok=True)
    tmp = target + ".link"
    if os.path.lexists(tmp):
        os.remove(tmp)
    os.symlink(final, tmp)
    old = None
    if os.path.isdir(target) and not os.path.islink(target):
        old = target + ".old"
        rmtree(old)
        os.rename(target, old)
    os.replace(tmp, target)
    if old:
        rmtree(old)
    touch(final)


def install(owner, name, version, url, old=None):
    target = os.path.join(MODS_DIR, name)
    if shared():
        link(target, fill_store(owner, name, version, url))
    else:
        work = os.path.join(TMP_DIR, name)
        rmtree(TMP_DIR)
        os.makedirs(work)
        unpack(owner, name, version, url, work)
        backup = target + ".old"
        if os.path.isdir(target):
            rmtree(backup)
            os.rename(target, backup)
        os.makedirs(MODS_DIR, exist_ok=True)
        os.rename(work, target)
        rmtree(backup)
        rmtree(TMP_DIR)
    maps = maps_in(target)
    log(f"{owner}-{name} {version}: installed" + (f", maps: {', '.join(maps)} (use as MAP)" if maps else ""))
    record_event(owner, name, old, version, maps)


def maps_in(target):
    """The display names of the maps a mod adds (its reskate-levels.json), [] for none."""
    try:
        with open(os.path.join(target, "reskate-levels.json"), encoding="utf-8-sig") as f:
            return [lv["displayName"] for lv in json.load(f).get("levels", []) if "displayName" in lv]
    except (OSError, ValueError, KeyError, AttributeError):
        return []


def log_maps(owner, name):
    """One line per map of an installed mod with the version in place, for the status cards:
    [mods] map Skate3Improved: 333-Skate_3_Improved 1.0.8"""
    target = os.path.join(MODS_DIR, name)
    have = read_manifest(target).get("version_number") if os.path.isdir(target) else None
    if have:
        for m in maps_in(target):
            log(f"map {m}: {owner}-{name} {have}")


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
        elif shared():
            if os.path.islink(target):
                touch(os.path.realpath(target))
            else:                                             # this server's own copy: share it
                link(target, fill_store(owner, name, want, m["download_url"], adopt=target))
                log(f"{label} {want}: moved into the shared store")
        log(f"{label} {want}: up to date")
    elif have is not None and marker is None:
        log(f"{label}: {MODS_DIR}/{name} ({have}) was not installed by MODS, leaving it alone")
    elif have is not None and dependency:
        log(f"{label}: needs {want}, {have} is installed, leaving it")
    elif have is not None and version is None and version_key(want) < version_key(have):
        log(f"{label}: {have} installed, Thunderstore reports older {want} (stale?), leaving it")
    elif have is not None and version is None and not update:
        log(f"{label}: {have} installed, newer {want} available (MODS_UPDATE=false)")
    else:
        install(owner, name, want, m["download_url"], have)
    log_maps(owner, name)
    for dep in m.get("dependencies", []):
        d = ID.match(dep)
        if d:
            ensure(d.group(1), d.group(2), d.group(3), update, True, seen)


def pending_updates(spec):
    """Unpinned MODS entries whose installed version is older than the latest on Thunderstore."""
    out = []
    for token in re.split(r"[\s,]+", spec.strip()):
        p = parse(token) if token else None
        if not p or p[2]:                      # invalid or pinned: nothing to announce
            continue
        try:
            latest = meta(p[0], p[1])["version_number"]
        except Exception as e:
            log(f"{token}: update check failed: {e}")
            continue
        have = read_manifest(os.path.join(MODS_DIR, p[1])).get("version_number")
        if have and version_key(latest) > version_key(have):
            out.append((p[0], p[1], latest, have))
    return out


def install_all(spec, update=True):
    seen = set()
    if spec.strip() and not shared():
        log(f"no volume mounted at {SHARED}: maps are kept in this server's {MODS_DIR}. Mount the shared "
            f"volume there (e.g. reskate-mods:{SHARED}) to download each map once for all servers, "
            f"or set MODS_STORE to a path.")
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


def gc(days):
    """Remove store folders nobody started with for `days` days, except the newest of each package.
    A running server keeps using the version it started with, so keep `days` above how long a
    server may run without a restart."""
    newest = {}
    for entry in os.scandir(PACKAGES) if os.path.isdir(PACKAGES) else []:
        m = read_marker(entry.path)
        if m:
            key = (m["owner"], m["name"])
            if key not in newest or version_key(m["version"]) > version_key(newest[key][0]):
                newest[key] = (m["version"], entry.path)
    for entry in os.scandir(PACKAGES) if os.path.isdir(PACKAGES) else []:
        m = read_marker(entry.path)
        if not m or newest[(m["owner"], m["name"])][1] == entry.path:
            continue
        age = (time.time() - entry.stat().st_mtime) / 86400
        if age >= days:
            with Lock(f"{m['owner']}-{m['name']}-{m['version']}"):
                rmtree(entry.path)
            log(f"removed {entry.name} (unused for {age:.0f} days)")


if __name__ == "__main__":  # docker exec <container> python3 /app/mods.py [gc [days]]
    if sys.argv[1:2] == ["gc"]:
        gc(float(sys.argv[2]) if len(sys.argv) > 2 else 30)
        sys.exit(0)
    install_all(os.environ.get("MODS", ""),
                os.environ.get("MODS_UPDATE", "true").strip().lower() in ("1", "true", "yes", "on"))
