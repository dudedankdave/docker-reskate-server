# Changelog

Versions below 1.0.0 are pre-releases.

## 0.2.15

- The COCOJAMBO logo is larger: it heads the F4 panel and sits bigger on the points chip.

## 0.2.14

- Fixed: the plugin could stop at game startup (plugin/main.lua ran longer than 50 ms, leaving F-keys dead and F5
  unresponsive). The COJA menu (hud/menu), its tabs (HOME, RACES, ADMIN, SETTINGS) and their dependencies
  (hud/hub, hud/home) are now initialized lazily on the first game tick or when F5 is pressed, cutting the
  synchronous load time well below ReSkate's 50 ms limit.

## 0.2.13

- Fixed (0.2.12): setting up a Throwdown race failed again. The game goes through a setup step (hidden for a flag
  placed from F5) before placing a flag, and 0.2.12 let go of any registration of ours not placing after 5 s, which
  took that step away. Only a placed flag waiting with no race of ours on it is let go now.
- Controller: **LB+RB+B** leaves a race, stops a challenge or closes the results, as F10 does; **LB+RB+A** (already
  there) runs the challenge you stand at, as F8 does. The prompts and F5 > SETTINGS say so.
- Rolling start for challenges: staff switch it on per challenge in F5 > ADMIN > Track Studio. Skating through its
  start gate then runs it at once (no line-up, no countdown, the clock from the gate); F8 still runs it with the grid.

## 0.2.12

- Fixed: after quitting a race, a new custom Throwdown race could not be set up. A setup page closed without its
  Back (Esc, a quit) stayed counted as open, so the registration it left was never let go; it is now counted closed
  as soon as the game has no registration. The race's Quit also lets the flag go, and opening the card while the
  results show closes them instead of refusing.
- F5 > RACES: "Place the race flag" lets go of a registration left open and tries again; a new "Throwdowns stuck?
  Free the flag" button lets go of it by hand.
- The server's log gets every change of the flag state and every Throwdowns event (a line already seen was skipped).
- The race still runs on the choices made in F5 > RACES: ReSkate only relabels the setup page in an English game
  (it looks for "Spot Battle", "Players" and "Timer").

## 0.2.11

- Fixed: our card could open the stock Spot Battle setup page (Skater, rounds, round timer) instead of ours (track,
  gates, grid time, laps, who can join). The game builds the page as the card is clicked, before the plugin hears
  `open`; the setup document is now always published, so it is there in time (the game only shows it on the page).
- A game tells the server's log its map key again once the server answers (the first line, sent while loading,
  could be lost).

## 0.2.10

- Fixed: the Throwdowns cards (ours and other modes', like Deathrace) could stop opening. The game has one flag
  registration for every mode and opens no card while one is open; one of ours could stay open after a race. The
  plugin now lets go of a registration of ours that nothing of ours uses (no race hosted or joined, no setup page, no
  placing) after 5 s, and logs every change of the game's flag state to the server's log.

## 0.2.9

- A fair start for races: everyone stands the same distance behind the start gate, spread evenly across it (a second
  row only when the gate is too narrow for everyone 1.8 m apart, each row spread evenly too), and the clock starts
  for everyone at GO.
- Server challenges are unchanged: run alone, their clock starts when you go through the first gate.

## 0.2.8

- Racers are held on the grid again until GO: since the game shows the countdown, the throwdown's input lock alone no
  longer kept them, so the skater is held too (`reskate.skater.hold`) and let go at GO (or when the race goes).
- The countdown has sound again: a beep on 3, 2 and 1 and the GO sound (the game's countdown plays none for a
  plugin's race).

## 0.2.7

- Tracks are kept per map: the map's own level (`reskate.level.current()`), e.g. `dingolevel_reskate_skate3improved`.
  Until now they were kept per skate. base world, which is `bam` for every modded map, so a server rotating maps
  would have shown every map's tracks on all of them. A server playing a map of its own takes what was kept under its
  base world (tracks and challenge times) as that map's, once.
- The bundled Skate 3 races are for Skate 3 Improved's level only.
- The server's log says which map key the server and each game use (`[coco race] map key ...`).

## 0.2.6

- The plugin can bring race tracks itself: `deathrace_tracks.py <races.json> --bundle` writes Skate 3's races into
  `plugin/tracks/bundled.lua` (by map), and the plugin's server copy adds each to its map once when it starts; a
  track staff delete or rename stays so. A `coco import` line is too long for the bridge's console (300 characters;
  a race is about 1.2 KB), so this is the way onto the servers.
- It brings Skate 3's ten races (Downtown 1-3, Industrial 1-3, University 1-4) for the Skate 3 map, from
  333-Skate_3_Improved 1.1.8's `data/races.json`.

## 0.2.5

- Skate 3's ten races (Downtown, Industrial, University) as race tracks: `coco import <map> <base64 json>` on the
  server's console saves tracks made elsewhere, and `deathrace_tracks.py` turns the Skate 3 Improved map's race data
  (its Deathraces' `data/races.json`, taken from Skate 3) into those lines: a start line in front of the original
  start slots, then the original checkpoints.

## 0.2.4

- Private races: **Who can join** (F5 > RACES, and the Throwdowns setup page) is Everyone, **My crew** or **Invited
  players**. A crew race shows only to the host's crew (the crew tag the server's leaderboard knows); an invite-only
  race's flag goes only to the players ticked in F5 > RACES. The host turns everyone else away, and the flag's card
  says who it is for.
- Gates are two posts and a top bar: the line along the ground is gone (world lines are drawn over everything, so it
  showed through the floor), and the posts start at the ground.

## 0.2.3

- Challenges show on the in-game map as throwdown flags: a map point with its bubble and a card naming the challenge,
  its record and its gates. **Enroll** there puts you on its start and runs it. The game only shows a flag led by
  another player on the server, so they show once someone else is on (alone, use F5 > RACES or the yellow beacons).

## 0.2.2

- Races and challenges start with the game's own countdown: while the racers line up, its intro card names the
  track and the race (laps, grid time); then its 3-2-1 and GO. Our five start lights and GO are gone; our bar
  (position, lap, gate, time), the standings and the results stay.

## 0.2.1

- Fixed: switching to the ADMIN tab could get the plugin stopped (its F-keys and the menu gone until the game is
  restarted). The tab read all of the game's blueprints at once while drawing; ReSkate stops a plugin that takes
  over 50 ms in one call. The search is now a button on ADMIN (**Log the game's marker blueprints**) and reads a
  few hundred a tick.
- The menu measures each text once and fits long texts by halving, instead of measuring every frame letter by letter
  (ReSkate also stops a plugin that averages over 2 ms a tick for 3 s).

## 0.2.0

- The COJA menu (F5) is redone with four tabs:
  - **HOME**: your profile in the COCOJAMBO look: the logo, your name, role and crew, your points, rank, time played
    and week here, and from the hub (reskate.cocojambo.net) your place on the ranked and unranked All Time boards,
    your crew and the servers.
  - **RACES**: the race setup, then the challenges.
  - **ADMIN** (staff): Track Studio.
  - **SETTINGS**: the points chip, its side, the notes and the HUD's size, and the controls.
- The game asks the hub at most once a minute while HOME is open (`net.http`, only to reskate.cocojambo.net).
- The ADMIN tab logs the game's blueprints that look like challenge markers, for showing events with skate.'s own
  marker (`world.entities`).
- The controls are documented in the README; what we learned about ReSkate's API in docs/COJA-TOOLS-API-NOTES.md.

## 0.1.13

- The mouse works in the COJA menu: the game reports `over_screen = false` for a screen without a panel (it means
  "over a game panel"), which 0.1.12 took for the cursor being off the screen.
- Line-up teleports are allowed on a server without noclip too: a race publishes an active HUD, which the game
  requires (with `match.teleports`) once the flag is left.

## 0.1.12

- The mouse works in the COJA menu: the cursor's position is a vector, which 0.1.11 did not read.
- The d-pad works in it again: under the menu's screen the pad comes through `reskate.input.gamepad()`'s named
  buttons, not `pad_bits`.
- The skater is held while it is open: the game's stock physics are switched on first, as a hold needs (not during
  a race, which has its own).

## 0.1.11

Built against ReSkate's own plugin API source (Dingo-Shenanigans/ReSkate) instead of guesses:

- The COJA menu opens as a screen without a panel (`reskate.ui.screen(title, build, {panel = false})`): the game
  draws nothing over it, the input is the plugin's, the mouse cursor shows and works, and Esc closes it.
- While it is open the skater is held (`reskate.skater.hold`), the controller's buttons are kept from the game
  every tick, and the right stick and triggers with them.
- Racers on the grid, and challenge runners on the start, face along the start gate: the heading comes from the
  gate's direction (the game's heading h faces (sin h, cos h)), not from where the admin looked when placing it.
- The menu closes when a race or a challenge begins, and when the race flag is being placed.

## 0.1.10

- While the COJA menu is open the game gets none of the controller's buttons, and neither the arrows, Enter, Space
  nor Tab, renewed every tick: the d-pad works the menu without moving the skater. The sticks and triggers are
  asked to wait too.
- Escape is no longer in that list (it always reaches the game, and with it the whole list was refused).
- The mouse is left out for now: the game gives a cursor only while a plugin's camera is shown.

## 0.1.9

- The COJA menu (F5) is our own panel in the HUD's look again, drawn over the game like the F4 card: no game
  screen, which covered it. The **STATS** tab shows the F4 card itself.
- Used with the keyboard (arrows, Enter or Space, Tab for the next tab, Esc closes) or a controller (d-pad, A,
  LB/RB for the tabs, B closes); with the mouse too where the game gives a cursor.
- While it is open the editor's keys and combos wait, and the game is asked to keep the menu's keys and buttons
  from the skater; what it answers goes to the server log.

## 0.1.8

- The COJA menu (F5) is built with the game's own widgets on its own screen: the game draws that screen over
  anything a plugin draws, so 0.1.7's drawn menu sat hidden under it, and its mouse reading came back empty.
- **STATS** shows the stats card's lines: points, rank, time played, team, the board's top 10, the server's rules
  and links. F4 keeps the card itself.
- F5 closes the menu again; Esc closes it as the game's screens do.

## 0.1.7

- The COJA menu (F5) is a screen of its own in the HUD's look, used with the mouse (F5, Esc or X close it):
  - **STATS**: your stats card, as on F4.
  - **RACE**: track, laps, grid time and racer count, then place the flag; start or call off your race.
  - **CHALLENGES**: each challenge with its record and distance; **Run** puts you on its start and runs it.
  - **TRACK STUDIO** (staff): new, edit, delete and challenge on/off; buttons for every gate edit, circuit/sprint and save.
- The race tabs leave Insert > PLUGINS > COJA tools, which keeps the dashboard, controls and HUD settings.
- What the game makes of the menu's screen and cursor goes to the server log (`[coco race] diag`).

## 0.1.6

- The keyboard keys of Track Studio and challenges (F6, F8, F9, 1-7) work again: they are read by their key codes.
- A racer the game could not put on the grid is moved there by the server, and held until the lights go out.
- Race setup also picks the track by its name when the game sends it, and placing the flag confirms the track in a toast.
- Players' games report teleports, keys and the setup page's choices to the server log (`[coco race] diag`).
- **F5** opens the COJA menu, in Insert > PLUGINS > COJA tools: RACE, CHALLENGES and TRACK STUDIO tabs.
- Gates are selected with PgUp/PgDn (and F6) on the keyboard; F5 is the menu now.
- Where the game will not open the menu from F5, F5 says where it is, and the reason goes to the server log.

## 0.1.5

- Place new and moved gates 3 m ahead of the admin, and sink their bottom stroke slightly into the floor.
- Every editor action has a controller default (LB+RB+A/X/B/Y/Start, LB+Left/Right, RB+D-pad); the F-keys and
  number keys keep working beside them.
- Race setup: publish it as soon as it opens, refresh its track choices as the server sends them, show an empty
  track list plainly with a **Refresh tracks** button, and confirm the picked track in a toast.
- Log throwdown UI publication errors instead of ignoring them.

## 0.1.2

- Add dashboard, controller and keyboard help, and HUD settings tabs to the COJA tools page.
- Restyle Track Studio's track library and gate editor in the COCOJAMBO look.
- Let race hosts select a track, player count, circuit laps, and quick/standard/relaxed grid time.
- Add controller combos to create/place and move gates.

## 0.1.1

- Register the COJA gate race mode as `deathraces` / Cocojambo Deathraces.
- Start each race clock at the start gate, hold racers during the grid countdown, and show the selected track in the branded race HUD.

## 0.1.0

- The COJA tools HUD: a corner tag with your role, time played, points and rank; a panel (F4) with your stats, the board and the server's links and rules; notes from the server at the top of the screen; settings on the plugin's page in the ReSkate menu.
- The server's tools fill the HUD through the `coco` console command.
- The Cocojambo Race throwdown mode: neon gates passed in order, start lights, live standings and a results page.
- Tracks kept on the server per map; staff build them in game (F7).
- Challenges: staff mark a track as one; its start has a marker, F8 there runs it alone and timed, and the server keeps its top 10 and announces new records.
