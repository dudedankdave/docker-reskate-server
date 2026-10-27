# Changelog

## 1.0.6

- The keyboard keys of Track Studio and challenges (F5, F6, F8, F9, 1-7) work again: they are read by their key codes.
- A racer the game could not put on the grid is moved there by the server, and held until the lights go out.
- Race setup also picks the track by its name when the game sends it, and placing the flag confirms the track in a toast.
- Players' games report teleports, keys and the setup page's choices to the server log (`[coco race] diag`).

## 1.0.5

- Place new and moved gates 3 m ahead of the admin, and sink their bottom stroke slightly into the floor.
- Every editor action has a controller default (LB+RB+A/X/B/Y/Start, LB+Left/Right, RB+D-pad); the F-keys and
  number keys keep working beside them.
- Race setup: publish it as soon as it opens, refresh its track choices as the server sends them, show an empty
  track list plainly with a **Refresh tracks** button, and confirm the picked track in a toast.
- Log throwdown UI publication errors instead of ignoring them.

## 1.0.2

- Add dashboard, controller and keyboard help, and HUD settings tabs to the COJA tools page.
- Restyle Track Studio's track library and gate editor in the COCOJAMBO look.
- Let race hosts select a track, player count, circuit laps, and quick/standard/relaxed grid time.
- Add controller combos to create/place and move gates.

## 1.0.1

- Register the COJA gate race mode as `deathraces` / Cocojambo Deathraces.
- Start each race clock at the start gate, hold racers during the grid countdown, and show the selected track in the branded race HUD.

## 1.0.0

- The COJA tools HUD: a corner tag with your role, time played, points and rank; a panel (F4) with your stats, the board and the server's links and rules; notes from the server at the top of the screen; settings on the plugin's page in the ReSkate menu.
- The server's tools fill the HUD through the `coco` console command.
- The Cocojambo Race throwdown mode: neon gates passed in order, start lights, live standings and a results page.
- Tracks kept on the server per map; staff build them in game (F7).
- Challenges: staff mark a track as one; its start has a marker, F8 there runs it alone and timed, and the server keeps its top 10 and announces new records.
