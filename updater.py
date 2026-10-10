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
import subprocess

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


def rmtree(path):
    """shutil is not in the image's python3-minimal, so rm does it."""
    subprocess.run(["rm", "-rf", "--", path], check=False)


def copy_tree(src, dst):
    os.makedirs(dst, exist_ok=True)
    for entry in os.listdir(src):
        if entry in OURS or entry == VERSION_FILE:
            continue
        s, d = os.path.join(src, entry), os.path.join(dst, entry)
        if os.path.islink(s):
            if os.path.lexists(d):
                rmtree(d)
            os.symlink(os.readlink(s), d)
        elif os.path.isdir(s):
            copy_tree(s, d)
        else:
            subprocess.run(["cp", "-p", "--", s, d], check=True)


def prepare(image_version, say):
    """Make sure /data/server holds a runnable server; returns the folder to run it from."""
    have = version_of(DIR) if os.path.exists(os.path.join(DIR, "ReSkateServer")) else ""
    if have and numbers(have) >= numbers(image_version):
        if numbers(have) > numbers(image_version):
            say(f"[update] running ReSkate {have} from {DIR} (installed by the updater; the image has {image_version})")
        return DIR
    rmtree(STAGING)
    copy_tree(IMAGE, STAGING)
    write_version(STAGING, image_version)
    swap_in(STAGING)
    rmtree(OLD)
    say(f"[update] {DIR} set up from the image, ReSkate {image_version}" + (f" (was {have})" if have else ""))
    return DIR


def swap_in(staged):
    """staged -> /data/server, the previous one kept as /data/server.old for a rollback."""
    rmtree(OLD)
    if os.path.exists(DIR):
        os.rename(DIR, OLD)
    os.rename(staged, DIR)


def rollback():
    if not os.path.exists(OLD):
        return False
    rmtree(STAGING)
    os.rename(DIR, STAGING)
    os.rename(OLD, DIR)
    rmtree(STAGING)
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
    rmtree(STAGING)
    try:
        curl(["-o", tgz, asset_url(info)], 600)
        if info.get("size") and os.path.getsize(tgz) != int(info["size"]):
            raise RuntimeError(f"download is {os.path.getsize(tgz)} bytes, launcher.json says {info['size']}")
        if sha256(tgz) != info["sha256"].lower():
            raise RuntimeError("download does not match its SHA-256 in launcher.json")
        copy_tree(DIR, STAGING)
        unpacked = STAGING + ".unpack"                        # tarfile is not in python3-minimal either
        rmtree(unpacked)
        os.makedirs(unpacked)
        run = subprocess.run(["tar", "-xzf", tgz, "-C", unpacked, "--no-same-owner", "--no-same-permissions"],
                             capture_output=True)
        if run.returncode != 0:
            raise RuntimeError("unpacking failed: " + run.stderr.decode("utf-8", "replace").strip())
        tops = os.listdir(unpacked)                           # ReSkateServer-Linux-x.y.z/<path>
        if len(tops) != 1 or not os.path.isdir(os.path.join(unpacked, tops[0])):
            raise RuntimeError(f"unexpected release layout: {tops}")
        root = os.path.join(unpacked, tops[0])
        for here, dirs, files in os.walk(root):
            rel = os.path.relpath(here, root)
            dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(here, d))
                       and not (rel == "." and d in KEEP)]
            os.makedirs(os.path.join(STAGING, rel), exist_ok=True)
            for f in files:
                s = os.path.join(here, f)
                if os.path.islink(s) or not os.path.isfile(s) or (rel == "." and f in KEEP):
                    continue
                d = os.path.normpath(os.path.join(STAGING, rel, f))
                if os.path.lexists(d):
                    rmtree(d)
                os.chmod(s, 0o755 if os.stat(s).st_mode & 0o111 else 0o644)
                os.rename(s, d)
        rmtree(unpacked)
        exe = os.path.join(STAGING, "ReSkateServer")
        if info.get("exe_sha256") and sha256(exe) != info["exe_sha256"].lower():
            raise RuntimeError("unpacked ReSkateServer does not match exe_sha256 in launcher.json")
        write_version(STAGING, info["version"])
    except Exception:
        rmtree(STAGING)
        rmtree(STAGING + ".unpack")
        raise
    finally:
        try:
            os.remove(tgz)
        except OSError:
            pass
    return STAGING
