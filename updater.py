"""ReSkate server files in /data/server, and installing new ReSkate releases into it.

UPDATE_MODE=pinned runs the server baked into the image (/app): the image tag is the version.
UPDATE_MODE=auto runs it from /data/server instead, so a release installed here survives the
container being recreated. /data/server is filled from the image on first start and again
whenever the image holds a newer version than the one there.

New releases are read from the release's launcher.json (the file ReSkate's own updater uses):
the Linux tarball is downloaded, checked against its SHA-256, unpacked next to /data/server and
its ReSkateServer checked against exe_sha256 before anything is swapped. supervisor.py swaps.
The server's own updater stays off (--no-update): it would replace /app, which is lost on recreate.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile

IMAGE = "/app"
DIR = "/data/server"
STAGING = "/data/server.new"
OLD = "/data/server.old"
VERSION_FILE = ".version"
REPO = "Dingo-Shenanigans/ReSkate"
LAUNCHER = f"https://github.com/{REPO}/releases/latest/download/launcher.json"
OURS = {"entrypoint.py", "healthcheck.py", "notifier.py", "mods.py", "updater.py", "supervisor.py",
        "reskate", "__pycache__"}
KEEP = {"ReSkateServer.json", "ReSkateServer.log", "Mods", "world-layers.json"}   # /data links, never replaced


def numbers(version):
    return tuple(int(n) for n in re.findall(r"\d+", version or "")[:4])


def version_of(path):
    try:
        with open(os.path.join(path, VERSION_FILE), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def write_version(path, version):
    with open(os.path.join(path, VERSION_FILE), "w", encoding="utf-8") as f:
        f.write(version + "\n")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def copy_tree(src, dst):
    os.makedirs(dst, exist_ok=True)
    for entry in os.listdir(src):
        if entry in OURS or entry == VERSION_FILE:
            continue
        s, d = os.path.join(src, entry), os.path.join(dst, entry)
        if os.path.islink(s):
            if os.path.lexists(d):
                os.remove(d) if not os.path.isdir(d) or os.path.islink(d) else shutil.rmtree(d)
            os.symlink(os.readlink(s), d)
        elif os.path.isdir(s):
            copy_tree(s, d)
        else:
            shutil.copy2(s, d)


def prepare(image_version, say):
    """Make sure /data/server holds a runnable server; returns the folder to run it from."""
    have = version_of(DIR) if os.path.exists(os.path.join(DIR, "ReSkateServer")) else ""
    if have and numbers(have) >= numbers(image_version):
        if numbers(have) > numbers(image_version):
            say(f"[update] running ReSkate {have} from {DIR} (installed by the updater; the image has {image_version})")
        return DIR
    shutil.rmtree(STAGING, ignore_errors=True)
    copy_tree(IMAGE, STAGING)
    write_version(STAGING, image_version)
    swap_in(STAGING)
    shutil.rmtree(OLD, ignore_errors=True)
    say(f"[update] {DIR} set up from the image, ReSkate {image_version}" + (f" (was {have})" if have else ""))
    return DIR


def swap_in(staged):
    """staged -> /data/server, the previous one kept as /data/server.old for a rollback."""
    shutil.rmtree(OLD, ignore_errors=True)
    if os.path.exists(DIR):
        os.rename(DIR, OLD)
    os.rename(staged, DIR)


def rollback():
    if not os.path.exists(OLD):
        return False
    shutil.rmtree(STAGING, ignore_errors=True)
    os.rename(DIR, STAGING)
    os.rename(OLD, DIR)
    shutil.rmtree(STAGING, ignore_errors=True)
    return True


def curl(args, timeout):
    run = subprocess.run(["curl", "-fsSL", "-m", str(timeout), "-A", "reskate-server-image", *args],
                         capture_output=True, timeout=timeout + 5)
    if run.returncode != 0:
        raise RuntimeError(run.stderr.decode("utf-8", "replace").strip() or f"curl exit {run.returncode}")
    return run.stdout


def latest():
    """The newest ReSkate release's Linux server: {"version", "url", "sha256", "size", "exe_sha256"}."""
    info = json.loads(curl([LAUNCHER], 30).decode("utf-8-sig"))["server_linux"]
    if not re.fullmatch(r"\d+(\.\d+)+", info.get("version", "")):
        raise RuntimeError(f"launcher.json: unexpected server_linux version {info.get('version')!r}")
    return info


def asset_url(info):
    url = info["url"]
    if url.startswith("asset:"):
        url = f"https://github.com/{REPO}/releases/download/v{info['version']}/{url[6:]}"
    if not url.startswith("https://"):
        raise RuntimeError(f"launcher.json: refusing download URL {url!r}")
    return url


def stage(info):
    """Download, verify and unpack a release into /data/server.new (a copy of /data/server with
    the release unpacked over it). Raises with a readable message on any problem."""
    tgz = DIR + ".download.tar.gz"
    shutil.rmtree(STAGING, ignore_errors=True)
    try:
        curl(["-o", tgz, asset_url(info)], 600)
        if info.get("size") and os.path.getsize(tgz) != int(info["size"]):
            raise RuntimeError(f"download is {os.path.getsize(tgz)} bytes, launcher.json says {info['size']}")
        if sha256(tgz) != info["sha256"].lower():
            raise RuntimeError("download does not match its SHA-256 in launcher.json")
        copy_tree(DIR, STAGING)
        with tarfile.open(tgz, "r:gz") as tar:
            for m in tar.getmembers():
                parts = m.name.split("/", 1)                 # ReSkateServer-Linux-x.y.z/<path>
                if len(parts) < 2 or not parts[1] or parts[1].split("/")[0] in KEEP:
                    continue
                if not (m.isfile() or m.isdir()) or ".." in parts[1].split("/") or parts[1].startswith("/"):
                    continue
                m.name = parts[1]
                tar.extract(m, STAGING, set_attrs=False, filter="data")
                if m.isfile():
                    os.chmod(os.path.join(STAGING, m.name), 0o755 if m.mode & 0o111 else 0o644)
        exe = os.path.join(STAGING, "ReSkateServer")
        if info.get("exe_sha256") and sha256(exe) != info["exe_sha256"].lower():
            raise RuntimeError("unpacked ReSkateServer does not match exe_sha256 in launcher.json")
        write_version(STAGING, info["version"])
    except Exception:
        shutil.rmtree(STAGING, ignore_errors=True)
        raise
    finally:
        try:
            os.remove(tgz)
        except OSError:
            pass
    return STAGING
