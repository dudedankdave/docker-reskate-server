"""Checks a ReSkate plugin folder and packs it as a Thunderstore upload.

    python plugins/build.py plugins/tools          # writes plugins/dist/TOOLS-1.0.0.zip
    python plugins/build.py plugins/tools --check  # only the checks

The zip holds manifest.json, icon.png, README.md (and CHANGELOG.md) at its root beside the plugin's
reskate-plugin.json and its files, which is what Thunderstore and ReSkate's plugin loader expect.
The team (the "Owner" in Owner-Name) is chosen on Thunderstore's upload page, not in the zip.
"""
import argparse
import json
import pathlib
import re
import struct
import sys
import zipfile

# Extension/Plugins/plugin_manifest.cpp in ReSkate 2.1.2: what "permissions" may name.
PERMISSIONS = {
    "console", "storage", "overlay", "menu", "ui.screen", "input", "hud", "events.tricks", "events.skater",
    "skater.read", "skater.teleport", "skater.control", "skater.animation", "trainer", "camera",
    "world.raycast", "world.objects", "world.entities", "world.environment", "world.ai", "travel", "session.read",
    "session.skaters", "session.nametags", "net.session", "net.http", "voice.control", "chat.read",
    "chat.send", "chat.local", "server", "server.parks", "throwdowns", "replay", "music", "progression.read",
    "settings.read", "audio.ui", "audio", "outfit", "board", "mods.read", "events.physics", "game.speed", "files",
    "variants", "settings.write", "room", "capture",
}
ROOT_FILES = ("manifest.json", "icon.png", "README.md", "CHANGELOG.md", "reskate-plugin.json")
# A plugin never ships a program (plugin_manifest.cpp, check_package_files).
RUNNABLE = {".exe", ".dll", ".sys", ".scr", ".com", ".cpl", ".msi", ".msp", ".bat", ".cmd", ".ps1", ".psm1",
            ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".hta", ".lnk", ".pif", ".reg"}


def check(folder: pathlib.Path) -> list[str]:
    """What is wrong with the plugin in `folder`; empty when it can be uploaded."""
    problems = []
    try:
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        plugin = json.loads((folder / "reskate-plugin.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return [f"manifest: {e}"]
    if not re.fullmatch(r"[A-Za-z0-9_]{1,128}", manifest.get("name", "")):
        problems.append("manifest.json name: letters, digits and _ only")
    if not re.fullmatch(r"\d+\.\d+\.\d+", manifest.get("version_number", "")):
        problems.append("manifest.json version_number: major.minor.patch")
    if not 0 < len(manifest.get("description", "")) <= 250:
        problems.append("manifest.json description: 1 to 250 characters")
    if not isinstance(manifest.get("dependencies"), list) or "website_url" not in manifest:
        problems.append("manifest.json needs dependencies (a list) and website_url")
    if plugin.get("api") != 1:
        problems.append("reskate-plugin.json api: 1")
    entry = plugin.get("entry", "")
    if not entry.endswith((".lua", ".luau")) or not (folder / entry).is_file():
        problems.append(f"reskate-plugin.json entry: {entry!r} is not a Lua file in the folder")
    if plugin.get("side", "client") not in ("client", "server", "both"):
        problems.append("reskate-plugin.json side: client, server or both")
    for name in plugin.get("permissions", []):
        if name not in PERMISSIONS:
            problems.append(f"reskate-plugin.json: unknown permission {name!r}")
    try:
        head = (folder / "icon.png").read_bytes()[:24]
        if head[:8] != b"\x89PNG\r\n\x1a\n" or struct.unpack(">II", head[16:24]) != (256, 256):
            problems.append("icon.png: a 256x256 PNG")
    except OSError:
        problems.append("icon.png is missing")
    if not (folder / "README.md").is_file():
        problems.append("README.md is missing")
    changelog = folder / "CHANGELOG.md"
    if changelog.is_file() and f"## {manifest.get('version_number')}" not in changelog.read_text(encoding="utf-8"):
        problems.append(f"CHANGELOG.md has no section for {manifest.get('version_number')}")
    for path in files(folder):
        if path.suffix.lower() in RUNNABLE:
            problems.append(f"{path.relative_to(folder)}: a plugin never ships a program")
    return problems


def files(folder: pathlib.Path) -> list[pathlib.Path]:
    """The files the package holds: the root files and the plugin's folders."""
    picked = [folder / name for name in ROOT_FILES if (folder / name).is_file()]
    for sub in sorted(p for p in folder.iterdir() if p.is_dir() and p.name not in ("dist", "__pycache__")):
        picked += sorted(p for p in sub.rglob("*") if p.is_file())
    return picked


def build(folder: pathlib.Path, out: pathlib.Path) -> pathlib.Path:
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    out.mkdir(parents=True, exist_ok=True)
    target = out / f"{manifest['name']}-{manifest['version_number']}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for path in files(folder):
            info = zipfile.ZipInfo(path.relative_to(folder).as_posix(), date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, path.read_bytes())
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("folder", type=pathlib.Path)
    parser.add_argument("--check", action="store_true", help="only check, write nothing")
    parser.add_argument("--out", type=pathlib.Path, default=pathlib.Path(__file__).resolve().parent / "dist")
    args = parser.parse_args()
    problems = check(args.folder)
    for problem in problems:
        print(f"error: {problem}", file=sys.stderr)
    if problems:
        return 1
    if not args.check:
        print(build(args.folder, args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
