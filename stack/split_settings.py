"""Writes today's server containers' env vars out as settings files (RESKATE_MODE=split-settings):

    common.env               keys every server has with the same value
    server-<n>.env           the rest, per server
    secrets/common.env       secret keys (passwords, tokens, webhook URLs) every server shares
    secrets/server-<n>.env   the other secret keys

PORT, QUERY_PORT, STEAM_TOKEN and RESKATE_SERVER stay in the containers (they are per slot), and the
image's own variables are left out. Secret values are written to the files only, never printed.
Servers are the running containers named container_prefix<n>-..., numbered by their reskate.server
label, RESKATE_SERVER or name; with two slots running, the newest counts.

    docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v reskate-shared:/shared \\
      dudedankdave/reskate-server split-settings [--write] [--force] [--out /shared/settings]

Without --write it only says which keys would go where. Existing files are kept unless --force.
Settings files override the container env, so the containers keep working as they are; the rollover
leaves the keys the files set out of the containers it creates from then on."""
import argparse
import os
import re
import sys

sys.path[:0] = [os.path.dirname(os.path.abspath(__file__)), os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib")]
import rollover  # noqa: E402
import settings  # noqa: E402

IMAGE_KEYS = {"PATH", "LD_LIBRARY_PATH", "HOME", "SteamAppId", "RESKATE_IMAGE_VERSION", "RESKATE_IMAGE_REVISION",
              "HOSTNAME", "TERM", "LANG", "PYTHONUNBUFFERED"}
SECRET = re.compile(r"PASSWORD|TOKEN|SECRET|WEBHOOK|_KEY$|^KEY$", re.I)


def quote(value):
    """value as an env-file value that settings.parse reads back unchanged."""
    if value == "" or re.fullmatch(r"[^\s#'\"\\]+(?:[ \t]+[^\s#'\"\\]+)*", value) and "\n" not in value:
        return value
    if "'" not in value and "\n" not in value:
        return f"'{value}'"
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t") + '"'


def number_of(info, prefix):
    labels = info["Config"].get("Labels") or {}
    env = rollover.env_of(info)
    if labels.get("reskate.server"):
        return labels["reskate.server"]
    if env.get("RESKATE_SERVER"):
        return env["RESKATE_SERVER"]
    m = re.match(re.escape(prefix) + r"(\d+)", info["Name"].lstrip("/"))
    return m.group(1) if m else None


def servers(prefix):
    """{number: env} of the newest running container of each server."""
    found = {}
    for c in rollover.docker("GET", "/containers/json"):
        if not any(n.lstrip("/").startswith(prefix) for n in c.get("Names") or []):
            continue
        info = rollover.docker("GET", f"/containers/{c['Id']}/json")
        number = number_of(info, prefix)
        if number is None:
            print(f"{info['Name'].lstrip('/')}: no server number (label reskate.server, RESKATE_SERVER or name), skipped")
            continue
        started = info["State"].get("StartedAt", "")
        if number not in found or started > found[number][0]:
            found[number] = (started, rollover.env_of(info))
    return {n: env for n, (_, env) in sorted(found.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0)}


def split(envs):
    """{file name: {key: value}} from {number: env}."""
    keep = set(settings.IDENTITY) | IMAGE_KEYS
    envs = {n: {k: v for k, v in env.items() if k not in keep} for n, env in envs.items()}
    common = {}
    if len(envs) > 1:
        first = next(iter(envs.values()))
        common = {k: v for k, v in first.items() if all(env.get(k) == v for env in envs.values())}
    files = {"common.env": {}, os.path.join("secrets", "common.env"): {}}
    for k, v in common.items():
        files[os.path.join("secrets", "common.env") if SECRET.search(k) else "common.env"][k] = v
    for n, env in envs.items():
        own = {k: v for k, v in env.items() if k not in common}
        files[f"server-{n}.env"] = {k: v for k, v in own.items() if not SECRET.search(k)}
        files[os.path.join("secrets", f"server-{n}.env")] = {k: v for k, v in own.items() if SECRET.search(k)}
    return {name: values for name, values in files.items() if values}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=settings.DIR)
    ap.add_argument("--prefix", default="reskate-server-")
    ap.add_argument("--write", action="store_true", help="write the files (default: only show what would go where)")
    ap.add_argument("--force", action="store_true", help="replace files that are there already")
    args = ap.parse_args()
    envs = servers(args.prefix)
    if not envs:
        sys.exit(f"no running containers named {args.prefix}<n>...")
    print(f"servers: {', '.join(envs)}")
    for name, values in split(envs).items():
        path = os.path.join(args.out, name)
        print(f"{name}: {', '.join(sorted(values))}")
        if not args.write:
            continue
        if os.path.exists(path) and not args.force:
            print(f"  {path} is there already, kept (--force replaces it)")
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        head = "# Written by split-settings from the running containers. Overrides their env vars.\n"
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            f.write(head + "".join(f"{k}={quote(v)}\n" for k, v in sorted(values.items())))
        os.replace(path + ".tmp", path)
        try:                                   # the servers run as uid 1000; secrets readable by them only
            os.chown(os.path.dirname(path), 1000, 1000)
            os.chown(path, 1000, 1000)
            if name.startswith("secrets"):
                os.chmod(path, 0o600)
        except OSError as e:
            print(f"  could not hand {path} to uid 1000: {e}")
        print(f"  wrote {path}")
    if not args.write:
        print("nothing written (--write writes the files)")


if __name__ == "__main__":
    main()
