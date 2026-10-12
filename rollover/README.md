# Rollover: a second server only while it's needed

A ReSkate release usually changes what clients and servers can join (2.0.3 changed the protocol),
and restarting a full server kicks everybody. `rollover.py` runs next to the servers and handles
each new release per server:

- **Nobody on:** the server is replaced in place (same slot, same Steam token, so the same Steam ID).
- **Players on:** a copy on the new version starts in the server's other slot. Players who updated
  their game join the copy; the old server keeps its players until it has been empty for
  `drain_grace_minutes`, then it is stopped (not removed, so it can be started again by hand).
- **Only a few players on:** with fewer than `copy_min_players` (default `1`, so any player counts)
  no copy starts yet. The server is replaced once it is empty, or a copy starts after
  `copy_wait_minutes` (default `60`) if it never empties. Both can also be set per server.

So most of the time each server runs one container; two only while the old one still has players.

**Map and mod updates** work the same way. Every `mod_check_minutes` (default `30`) the rollover asks
each server, with the image's own `mods.py`, whether an unpinned `MODS` entry has a newer version on
Thunderstore. If so, an empty server is restarted (mods install on start); a busy one gets a copy in
the other slot (same ReSkate version and image, named `<name>-m<MMDDHHMM>`) and the old one drains as
above. Like `copy_min_players` / `copy_wait_minutes` for releases, `MODUPDATE_MIN_PLAYERS` (default `1`)
and `MODUPDATE_WAIT` (minutes, default `60`) hold a map update back while only a few players are on;
set them as env vars on the rollover container, or as `modupdate_min_players` / `modupdate_wait` in
`servers.json` (globally or per server). An env var on the container wins over the file's top-level
value; a per-server value wins over both. `mod_updates: false` (globally or per
server) turns this off; servers with `MODS_UPDATE=false` or pinned entries are never rolled for mods.

**Shared map store:** every container the rollover creates (copies, in-place replacements, map-update copies) gets the volume `MODS_VOLUME` at `/mods` (default `reskate-mods`; env var, or `mods_volume` in `servers.json`, globally or per server; `off` leaves it out). Servers share one download of each map version (see the main README); a server started without `/mods` can crash-loop on current images. A copy keeps its predecessor's map version available there until `mods.py gc` removes it. The existing servers need the same mount added once, the next time they are recreated.

**Repair:** a server that is up to date but runs without the shared map volume is recreated (named `<name>-r<MMDDHHMM>`, same image and settings, plus `/mods`) once nobody at all is on it. It never waits for a cutoff and never kicks anyone; `repair_mods: false` (globally or per server) turns it off.

**Waiting for the image:** a ReSkate release is announced before its Docker Hub image is built. The rollover waits up to `IMAGE_WAIT_MINUTES` (`image_wait_minutes`, default `60`) for `<image>:<version>` to exist before it updates or starts anything for that release; after that it goes on with the current image and the release installed into the volume. Map updates don't wait.

## Slots

Each server has two slots with their own ports, Steam token and `/data` volume (`servers.example.json`).
The two slots of a server sit next to each other: server N uses 124N0/124N1 (A) and 124N2/124N3 (B).
The copy is a clone of the running container (env, restart policy, network, Discord webhooks) with
the slot's `PORT`, `QUERY_PORT`, `STEAM_TOKEN`, volume and a `SERVER_NAME` from `name_format`
(`{v}` = version without dots, `{n}` = server number, `{role}`). It is named `<container_prefix><server>-<version>`.

- **Tokens** are never stored in the config: the slot's token is taken from a container (running or
  stopped) that used that slot, or from `STEAM_TOKEN_<server><slot>` (`STEAM_TOKEN_1B`) on the rollover
  container. A Steam token can only be signed in once, so the two slots need different tokens.
- **Image:** `dudedankdave/reskate-server:<version>` when Docker Hub has it. Otherwise the current
  image, with the release installed into the slot's volume first by the image's own updater
  (`UPDATE_MODE=auto`, `UPDATE_POLICY=ask`, so the copy never installs a later release on its own).
  A new release therefore needs no new image.
- **Players** are counted from the server log since it last came up (joins, leaves, `[network]`
  lines); the server's A2S query reports 0. If the log can't be read the server counts as busy.
- **Protected containers** (`protect`) are never stopped or replaced; a server whose other slot is
  taken by one waits.

## Running it

```sh
cd rollover && docker compose up -d --build     # starts in DRY_RUN: only logs what it would do
docker logs -f reskate-rollover
```

`DRY_RUN=false` lets it act. `DISCORD_WEBHOOK` posts what it does. Without the socket it can also use
Portainer: `DOCKER_API=https://portainer.example/api/endpoints/3/docker` (with an `X-API-Key` added by a
proxy). `PRETEND_LATEST=2.0.9 python3 rollover.py --once` shows what a release would trigger.

## Status cards in Discord

`status.py` keeps one message in a Discord channel up to date: a card per server with online/offline,
players, map, version and join code, and the total at the bottom. It posts the message once through a webhook
and then only edits it (every minute when something changed, at least every 5 minutes), so the channel
never fills up. Players come from the server log like above; a server with no running container shows
as offline, one that is still coming up as starting, and an older version still draining its players
is listed on its server's card.

```sh
cd rollover && docker compose up -d --build status    # STATUS_WEBHOOK set in docker-compose.yml
docker logs -f reskate-status
```

Make the webhook in the channel the cards should go to (channel settings, Integrations, Webhooks); its
name and avatar are what the message shows. Ideally a channel where only the webhook can post, so the
cards stay at the top. If the message is deleted, a new one is posted. Without `STATUS_WEBHOOK` it only
logs the message it would post.

| Variable | Default | |
| --- | --- | --- |
| `STATUS_WEBHOOK` | | Webhook URL of the channel. |
| `STATUS_INTERVAL` | `60` | Seconds between checks. |
| `STATUS_REFRESH_MINUTES` | `5` | Edit at least this often, so "Updated" shows the board is alive. |
| `STATUS_SPACER` | `assets/status-spacer.png` on `dev` | Wide transparent image on every card so they are all the same size; empty = none. |
| `STATUS_JOIN_CODE` | `true` | Show the join code on the cards. |
| `STATUS_MESSAGE_ID` | | Edit this message instead of the remembered one (e.g. after losing the volume). |
| `STATUS_STATE` | `/state/status.json` | Where the message id is kept. |
