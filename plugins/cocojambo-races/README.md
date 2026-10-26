# Cocojambo Races

Gate races for ReSkate 2.1.2 and later, as a throwdown mode. A track is a row of glowing neon-blue
gates; racers skate through them in order, and a gate only counts when you go through its frame
the right way. Made for the Cocojambo servers; it works on any server that runs its server side.

## Race

1. Open **Throwdowns** and pick **Cocojambo Race**. Choose the track, the laps (circuits only) and
   how many racers.
2. Place the flag. It shows on the map like every throwdown flag, with the track, laps and gates on
   its card. Other players join it as usual; everyone on the flag sees the track's gates.
3. Press **Start**. Everyone is put on the grid behind the start gate. Five red lights come on one
   by one; when they go out, **GO**. The game uses stock physics during the race.
4. Go through every gate in order: the bright one with the arrow is next, the dim one after it.
   A gate you miss has to be taken again: no gate can be skipped. Standings are on the right.
5. The results page shows positions, times, gaps or best laps once everyone finished, one minute
   after the winner, or after 15 minutes.

**F10** leaves the race, a flag you joined, or closes the results.

## Make tracks (admins)

Tracks are kept on the server for each map, and only its staff (admins, and roles marked staff)
can make, change or delete them. Everyone gets them when they join.

| Key | |
|---|---|
| **F7** | Track list (new, edit, delete); while editing: save |
| **F8** | New gate where you stand, facing the way you skate, after the selected one |
| **F9** | Delete the selected gate |
| **F5 / F6** | Select the previous / next gate |
| **Alt+H** | Move the selected gate to where you stand |
| **Alt+M** | Switch what Alt+W/A/S/D do: MOVE (forward/back, left/right), HEIGHT & TURN (up/down, turn 15°), SIZE (taller/lower, narrower/wider) |
| **Alt+C** | Circuit (gate 1 is start and finish, laps) or sprint (the last gate is the finish) |
| **F10** | Close the editor |

Gate 1 is the start. Place it facing the way the race goes: the grid faces where you faced. The
selected gate is yellow; the arrows show the way through each gate.

All keys can be changed on ReSkate's Binds page.

## Fair play

A racer who moves further than skating allows (noclip, teleporting) is out of the race.

## For server owners

The server side keeps the tracks. With the dudedankdave/reskate-server image, require the plugin
and the image installs both sides:

```
PLUGINS_REQUIRED=Cocojambo-Cocojambo_Races
```

Elsewhere, put the plugin's folder into `Plugins` beside the server (`ReSkateServer`).

Source: https://github.com/dudedankdave/docker-reskate-server/tree/main/plugins/cocojambo-races
