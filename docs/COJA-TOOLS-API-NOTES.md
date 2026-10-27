# COJA tools: what ReSkate's plugin API does (notes)

What the COJA tools plugin (`plugins/tools`) learned about ReSkate 2.1.2's plugin API, from ReSkate's source
(github.com/Dingo-Shenanigans/ReSkate: `Extension/Plugins`, `contrib/throwdown-kit.md`) and from test rounds on the
test server. Each point says where it is in that source, so it can be checked again after a ReSkate update.

## Our own screen: the COJA menu (F5)

- `reskate.ui.screen(title, build, {panel = false})` is a screen the game draws **nothing** for (dock 3 in
  `plugin_ui.h`). The plugin draws it in its own draw handler. While it is open (`reskate.ui.open(title)`), the input
  is the plugin's: the game's own controls stop, and the mouse cursor shows. Esc closes it and fires
  `screen_closed(title)` (`UI/Overlay/plugin_overlay.cpp`, `draw_plugin_screen`).
- Without `{panel = false}` the game draws a full screen of its own (dimmed, with the title and "Esc closes it") over
  everything a plugin draws, and builds it from the widgets its function is handed. A plugin's drawing is then hidden.
- A plugin's drawing over plain gameplay (the F4 card, the race HUD) is never covered, but gets no cursor.

## Mouse

- `reskate.input.cursor()` gives `{position, left, right, middle, wheel, over_screen}` **only while the plugin's
  screen is open**, else nil (`plugin_api_world.cpp`, `input_cursor`). `position` is a vector in window pixels, not a
  table. `left` is the button held down, so a click is its edge.
- `over_screen` says whether a *game panel* is under the cursor. A screen without a panel has none, so it is
  always false there: do not read it as "the cursor is off the screen".
- `reskate.input.mouse()` is raw movement since the last read (`{x, y, wheel}` in counts), not a position.
- `reskate.input.capture(on)` only works while a plugin's camera is shown.

## Controller and keyboard

- `reskate.input.pad_bits()` is the pad as XInput bits (d-pad up 0x1, down 0x2, left 0x4, right 0x8, LB 0x100,
  RB 0x200, A 0x1000, B 0x2000). It is **nil while a plugin screen is open** (the screen has the pad). Then
  `reskate.input.gamepad().buttons` still names the buttons: `a b x y up down left right lb rb start back
  left_stick right_stick`.
- `reskate.input.key(k)` takes a key name or a Windows virtual-key code, and reads the keyboard directly
  (GetAsyncKeyState), whatever the game or a screen does with it.
- `reskate.input.game_buttons(hidden, held, pause)` keeps XInput buttons from the game **while asked**: call it every
  tick. `reskate.input.game_keys(keys)` keeps keys from the game until the next call. Escape and the mouse buttons
  always reach the game; a list holding one is refused whole.
- `reskate.input.hide{left_trigger, right_trigger, right_stick, left_stick_click, right_stick_click = true}` takes
  named flags, not a list. The left stick cannot be hidden (only its x while grinding).
- `reskate.skater.hold(on)` holds the skater where it is, but needs the game's stock physics first:
  `reskate.physics.competition(true)`.

## Teleports and headings

- A heading `h` (radians) faces `(sin h, cos h)` in x and z: the skater's +Z (`plugin_game_runtime.cpp`). To face
  along a direction `(nx, nz)`: `h = atan2(nx, nz)`.
- `reskate.skater.teleport(position, heading)` is refused with "the host has turned teleporting off" when the server
  does not allow noclip at that moment, unless the mode's match asks for teleports (`match.teleports`) **and** its
  HUD is active (`hud.active`) or it holds the flag (`throwdown_kit.cpp`, `allows_teleports`). A race that leaves the
  flag at the start must publish `hud = {active = true}`.
- `reskate.server.move(player, position)` (the server copy) takes no heading.

## The throwdown kit

- `setup_row` sends a choice's value **from 0**, while the setup document's `value` is from 1.
- `match.input_lock = true` holds the racers on the grid (no skating input).
- `flags` show on the map as native throwdown flags (map point, bubble, map card with up to 3 stats, Enroll card):
  the plugin's only way onto the in-game map. Native challenges can only be read (`reskate.progression`).
- `reskate.entities.set(id, blueprint, transform)` places a world object from one of the game's blueprints;
  `reskate.entities.blueprints(prefix)` lists the loaded ones. The ADMIN tab logs the ones that look like markers.

## Native countdown

- `hud.countdown = {remaining_ms, total_ms, sampled_ms, ticking = true}` (with `hud.active`) is the game's own
  3-2-1-GO: it shows the digits for the last 3 s of the clock and GO for under a second once the match plays
  (`native_hud_selection.h`, `HudCountdownLifecycle`); `hud.intro = {title, rules, until_ms}` is its card before.

## One flag registration for all modes

- The game has a single throwdown flag registration (`native_flag_session.cpp`, `kit::flag::begin`): clicking any
  mode's card begins it, and `begin` refuses while one is open (waiting, placing, a placed flag, cleanup). One left
  open blocks every card on the Throwdowns page. `reskate.throwdowns.flag.state()` gives `{busy, owned, waiting,
  placing, mine, mode, game}`, `mode` only when it is the plugin's; `flag.leave()` lets it go.

## Flags on the map

- A `flags` entry shows on *this* player's map only when its `leader` is another player here (a peer): the game then
  mirrors it as a native flag (map point, bubble, card with up to 3 stats) and its Enroll comes back as `join`. A
  flag led by this player (or by nobody) is sent to the other players instead, up to 8, keyed by `game`
  (`throwdown_relay.cpp`, `maintain_custom_flags`). So a plugin showing fixed places (our challenges) publishes its
  own, led by another online player; alone on the server there is no such flag.
- Plugins see no parties: players have only `id, name, me, host, server`, and the server's `parties` command is not
  theirs to run (a plugin's console command only takes the lines meant for it).

## Holding racers before GO

- `match.input_lock` blocks the game's input only while no native challenge panel owns it
  (`owned_menu_countdown_capture(input_lock, challenge_panel_owns_input())`, `input_capture.cpp`); with the game's
  countdown shown (0.2.2) racers could move again. The races hold the skater as well (`reskate.skater.hold(true)`
  under `physics.competition(true)`) and let go at GO. The game's countdown plays no sound for a plugin's race: the
  plugin beeps (`audio.play_ui("countdown")` on 3, 2, 1, `"go"` at GO).

## Map markers and the maps' own races

- The race point the Skate 3 map shows at the top of University is a native skate. race challenge in the map's level
  data (built with ReSkate Studio from a template level); the game registers it on the map itself. Plugins can
  neither create such a point (the map's POI registry is only used by ReSkate's C++: party markers, waypoints) nor
  read a challenge's position or route (`reskate.progression` has id, type, neighborhood and attempts only).
- The maps' archives are Oodle-compressed (CAS block codec 0x11, Kraken header 8C 06): only readable on Windows with
  the game's Oodle DLL, so a native race's route can only be exported there (ReSkate Studio).

## Web requests

- `reskate.http.request({url, method, body, content_type}, function(status, body, error))` needs the `net.http`
  permission and the host in the manifest's `"domains"`; HTTPS only. It exists only in the game: a plugin's server
  copy has no web requests.
- The hub's public JSON (reskate.cocojambo.net/status/api/: `board?league=`, `teams`, `servers`) has no Steam IDs:
  players are matched by name, and boards list their top 50.

## Time and memory: what gets a plugin stopped

ReSkate stops a plugin (all its binds, its menu and its drawing are gone until the game is restarted) when one call
into it runs over 50 ms, when it averages over 2 ms a tick for 3 seconds in a row, when it uses over 64 MB, or after
more than 5 errors in 10 s (`plugin_vm.h`, `Limits`). So big jobs (thousands of blueprints) are spread over ticks,
and what is measured every frame (`d:measure`) is cached.

## Which map

- `reskate.session.world().map` is skate.'s base world (`bam`, `grom`, `ftue`, `stadium_1`, `stadium_2`, `mpr`;
  `world_layer_types.h`): every modded map (Skate 3 Improved, Skate 2 ...) loads on top of `bam`, so it cannot tell
  them apart. `reskate.level.current()` (`{name}`, no permission) is the level being played: the map's own level
  asset in the game (`client_tick.cpp`: the live mod level) and the map the server runs on the server
  (`server_plugins.cpp`: `map_level()`). Its last path part, lower case, is the key that both share.

## Modules

- `require` caches each module per plugin: requiring one twice returns the same table.
