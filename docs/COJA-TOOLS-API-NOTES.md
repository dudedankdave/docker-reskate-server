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

## Web requests

- `reskate.http.request({url, method, body, content_type}, function(status, body, error))` needs the `net.http`
  permission and the host in the manifest's `"domains"`; HTTPS only. It exists only in the game: a plugin's server
  copy has no web requests.
- The hub's public JSON (reskate.cocojambo.net/status/api/: `board?league=`, `teams`, `servers`) has no Steam IDs:
  players are matched by name, and boards list their top 50.

## Modules

- `require` caches each module per plugin: requiring one twice returns the same table.
