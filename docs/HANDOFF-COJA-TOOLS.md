# Handoff: COJA tools plugin, next steps with the game folder

For a new chat that runs on the PC with ReSkate installed. Written 2026-10-27 at plugin version **0.2.4**.

## Where things are

- Repo: `dudedankdave/docker-reskate-server`, branch **`claude/wizardly-ride-jr16e7`** (not merged; `main` still has
  1.0.5 from PR #90). Local checkout: `C:\Home\Github\Privat\docker-reskate-server`. Pull the branch first.
- Plugin: `plugins/tools` (package `COCOJAMBO-TOOLS`). Build the zip: `python plugins/build.py plugins/tools` gives
  `plugins/dist/TOOLS-<version>.zip`. Tests: `pip install lupa`, then `python -m unittest discover -s tests`
  (plugin tests: `tests/test_tools_plugin.py`, against a stand-in of ReSkate's API). Lint: `ruff check .`.
- **Read first:** `docs/COJA-TOOLS-API-NOTES.md`: everything learned about ReSkate's plugin API (screens, cursor, pad,
  input blocking, holds, teleports and headings, the throwdown kit, flags on the map, web requests, what gets a
  plugin stopped). `plugins/tools/README.md` and `CHANGELOG.md` say what the plugin does.
- ReSkate's source: public at github.com/Dingo-Shenanigans/ReSkate (`Extension/Plugins`, `Extension/Throwdowns`,
  `contrib/throwdown-kit.md`). Its `contrib/plugins/` (with SmokeyDawg58's plugin) is gitignored, not public.
- Versions below 1.0.0 are pre-releases; bump `manifest.json` `version_number` and `plugin/version.lua` together
  (a test checks), and add a `## <version>` section to `CHANGELOG.md` (the build checks).

## The test server

- `reskate-server-9-2.1.2-r143` ("COCOJAMBOS YARD 9 - Test", port 12490) on the Portainer host (endpoint 3), data
  volume `reskate-test_reskate-test-data` at `/data`. It runs with `UPDATE_MODE=auto`, so ReSkate runs from
  `/data/server` and reads plugins from `/data/server/Plugins`, which is now a link to `/data/Plugins` (the old
  1.0.0 folder is in `/data/server-Plugins-1.0.0.bak`).
- Deploy: unzip the zip into `/data/Plugins/COCOJAMBO-TOOLS/` (owner uid 1000). The server reloads it within 2 s
  ("[Plugins] Plugin COCOJAMBO-TOOLS reloading"). Players install the same zip into their own ReSkate by hand.
- The plugin's server copy stores its data in `/data/data/plugins/COCOJAMBO-TOOLS/storage.json`: keys
  `tracks:<map key>` (the tracks), `best:<map key>:<track id>` (challenge top 10), `hud:*`. The Skate 3 map's key is
  **`dingolevel_reskate_skate3improved`** (the map's level, `reskate.level.current().name`'s last part, lower case;
  before 0.2.7 it was `bam`, skate.'s base world, which every modded map shares).
- Settings: `/shared/settings/server-9.env` (TOOLS_HUD=true, LEADERBOARD=true; PLUGINS_REQUIRED does not list the
  plugin, it is not on Thunderstore).
- Diagnostics: players' games report what the game did to the server log as `[coco race] diag <player>: ...`
  (`/data/ReSkateServer.log`). That is how every API question so far was answered; keep using it.

## What works now (0.2.4, all tried in game up to 0.2.1 unless noted)

- HUD: corner chip, F4 stats card, notes from the server.
- COJA menu (F5): our own panel without a game panel (`ui.screen(title, fn, {panel = false})`), mouse, keyboard and
  controller work; the skater is held while it is open. Tabs: HOME (profile, stats from the hub
  reskate.cocojambo.net), RACES (setup and challenges), ADMIN (Track Studio, staff), SETTINGS.
- Races: tracks of neon gates saved on the server per map, built in Track Studio (F7 or F5 > ADMIN); line-up
  teleport facing through the start gate (fixed and confirmed), hold until GO, the game's own countdown (0.2.2, not
  yet tried), our standings/time/position bar, results. Challenges: solo timed runs with a top 10.
- 0.2.3/0.2.4 (not yet tried in game): challenges as throwdown flags on the map; races for Everyone / My crew /
  Invited players; gates without the ground line.

## The open job: Skate 3's races (University Race 2 ...) in our races

The user showed SmokeyDawg58's **Deathrace** mode (from his `SmokeyDawg58-ReSkate_Throwdown` plugin, not on
Thunderstore, installed on this PC): a start pad ("HOLD Y TO START"), a setup page (race, skaters, time limit), and
the native "race" HUD (DEATHRACE header, checkpoint bar, stopwatch, TIME LEFT, "CHECKPOINT 7/12 34 M"). Its races
(University Race 2, Industrial Race 2 ...) are **Skate 3's own races, ported**, so they are not his to license
(credit him for the port all the same).

1. **Find the route data on this PC.** Look in the game's ReSkate folder for the plugin
   `Plugins/SmokeyDawg58-ReSkate_Throwdown` and its data folder: Deathrace routes are JSON files with
   `"format": "ReSkate Deathrace route", "version": 1` and `"map"`, `"settings"`, saved per map
   (`routes/<map>/<name>.json`, per ReSkate's plugin tests). Find where the Skate 3 races are: shipped in the plugin
   as route files, or generated from the Skate 3 map's own race challenges (then the plugin's Lua shows how it reads
   them). Read the plugin's Lua for the route format: checkpoints (position, direction, size), start, time limit.
   Also note how it places its start pad (a world entity blueprint? `reskate.entities.set`) and its HUD document.
2. **Write a converter** (a Python script in `plugins/tools/`, with a test) from Deathrace routes to our track format,
   then get them into the server's storage under `tracks:<map key>`. Do not edit `storage.json` by hand while the server
   runs: the plugin keeps its storage in memory and writes it back. Add an import to the plugin's server copy
   instead, e.g. a console command `coco import <map> <base64 json>` (the server's tools can type it, like
   `coco player`), which checks each track with `clean_track` and saves it as Track Studio does.
   Our track (as stored): `{"id", "name", "author", "circuit": bool, "challenge": bool, "gates": [{"p": [x, y, z]
   (middle of the gate's bottom edge, metres, y up), "n": [nx, nz] (the way through, level, unit), "w" width 2-30,
   "h" height 1.5-12, "heading"}]}`, 2-40 gates (3+ for a circuit), name up to 32 characters, at most 20 tracks per
   map. Ids are `"<ms>-<n>"`; a new id is fine. A heading `h` faces `(sin h, cos h)`, so `n` from a heading is
   `[sin h, cos h]` and back `h = atan2(nx, nz)`.
3. **Native race HUD.** Switch our race HUD to the kit's native `hud` document with `theme = "race"` (header, board,
   timers, markers ...; see `contrib/throwdown-kit.md` and how SmokeyDawg58's Deathrace fills it). Ask the user
   whether the native timer and checkpoint bar replace our top bar or sit beside it; they asked to keep our own
   standings, time and position cards.

## Other open items

- Hub: HOME matches players by name on the hub's public boards (top 50, no Steam IDs). A per-player endpoint by
  Steam ID on the hub (its code is on branch `claude/public-page`, not merged) would fix both.
- Feature requests to ReSkate's devs: a plugin API for a race-style map point (the map's POI registry is only used by
  ReSkate's C++), and party membership for plugins (for party-only races).
- `app/updater.py`: an existing `/data/server` never gets the `Plugins` link (suggested as a separate task).
- When done: merge the branch to `main` (PR), so the server image gets the plugin and the renumbered history.
