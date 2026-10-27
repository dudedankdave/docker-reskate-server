# Changelog

Versions below 1.0.0 are pre-releases.

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
