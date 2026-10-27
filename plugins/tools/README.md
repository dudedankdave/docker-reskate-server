# COJA tools

The plugin of the COCOJAMBO ReSkate servers (ReSkate 2.1.2 and later): a HUD with your points, rank
and time played, a panel with the board and the server's links, notes from the server, and neon gate
races as a throwdown mode. It only shows on a server that runs its server side.

## HUD

In the COCOJAMBO look of the website:

- **Points chip** (top left, small and faint): your points, rank and the time you played on this server.
- **F4** opens the panel on the other side: your stats, the board (top 10), the server's rules and links.
- **Notes** from the server at the top of the screen instead of chat lines.

Both step aside while a menu or another screen is open. Insert > PLUGINS > **COJA tools** has dashboard,
controls help and settings tabs. The dashboard shows your server stats; settings control the chip, notes,
side and size.

## Races

Gate races as a throwdown mode. A track is a row of glowing neon-blue gates; racers skate through
them in order, and a gate only counts when you go through its frame the right way.

### Race

1. Open **Throwdowns** and pick **Cocojambo Deathraces**. Choose a track, racer limit, grid time, and
   laps for a circuit.
2. Place the flag. It shows on the map like every throwdown flag, with the track, laps and gates on
   its card. Other players join it as usual; everyone on the flag sees the track's gates.
3. Press **Start**. Everyone is put on the grid behind the start gate. Five red lights come on one
   by one; when they go out, **GO**. The game uses stock physics during the race.
4. Go through every gate in order: the bright one with the arrow is next, the dim one after it.
   A gate you miss has to be taken again: no gate can be skipped. Standings are on the right.
5. The results page shows positions, times, gaps or best laps once everyone finished, one minute
   after the winner, or after 15 minutes.

**F10** leaves the race, a flag you joined, or closes the results.

### Challenges

A track an admin marks as a **challenge** is there all the time, no host needed: its start has a sun-yellow
beacon with the track's name and record over it. Skate to it and press **F8**: the start lights run and you race
it alone, timed. The server keeps each challenge's top 10 (every player's best), tells you your time and place,
and tells everyone when the record falls. **F10** stops a run.

### Make tracks (admins)

Tracks are kept on the server for each map, and only its staff (admins, and roles marked staff)
can make, change or delete them. Everyone gets them when they join.

| Key | |
|---|---|
| **F7** | Open or close Track Studio; while editing, save the track |
| **F5 / F6** | Select a track in the library or a gate while editing |
| **F8** | New track in the library; add a gate while editing; start a nearby challenge outside the editor |
| **6** | Edit the selected track in the library; move the selected gate while editing |
| **7** | Toggle a challenge in the library; switch circuit/sprint while editing |
| **F9** | Delete the selected track or gate, depending on the screen |
| **1** | Switch what 2/3/4/5 do: MOVE, HEIGHT & TURN, or SIZE |
| **F10** | Close Track Studio |

Controller: **LB+RB+A** creates a track or places a gate, and **LB+RB+X** edits a track or moves the selected gate. Rebind either combo on ReSkate's Binds page.

Gate 1 is the start. Place it facing the way the race goes: the grid faces where you faced. A gate stands on the
ground under you, also when you place it mid-air. Moving a challenge's gates starts its times over. The selected gate is yellow; the arrows show the way through each
gate.

All keys can be changed on ReSkate's Binds page.

### Fair play

A racer who moves further than skating allows (noclip, teleporting) is out of the race.

## For server owners

The server side keeps the race tracks and runs the HUD's server copy. With the
dudedankdave/reskate-server image, require the plugin and the image installs both sides:

```
PLUGINS_REQUIRED=COCOJAMBO-TOOLS
```

Elsewhere, put the plugin's folder into `Plugins` beside the server (`ReSkateServer`).

### Filling the HUD from the server's tools

The HUD's server copy counts the time played itself (per server). Points, rank, the board and the
server's links come from the server's own tools through the console, as base64 JSON (a console line
splits on spaces):

| Command | JSON |
|---|---|
| `coco player <SteamID64> <base64>` | a player's stats: `{"points": 1234, "rank": 3, "of": 120, "board": "Week 43", "team": "Night Owls", "lines": [{"l": "Props", "v": "12"}]}`, every field optional |
| `coco top <base64>` | the board on the panel: `{"board": "Week 43", "rows": [{"n": "Name", "p": 1234}]}` |
| `coco info <base64>` | the panel's server part, kept across restarts: `{"name": "COCOJAMBOS YARD", "links": [{"label": "Discord", "url": "discord.gg/..."}], "rules": ["Be nice"]}` |
| `coco toast <SteamID64\|all> <base64>` | a note at the top of the screen: `{"text": "Rank up!", "kind": "good", "secs": 5}` (kind `info`, `good`, `warn`, `error`, `race`) |
| `coco status` | who has the HUD |

A player who leaves is logged as `[coco] left <SteamID64> played <seconds>`.

Source: https://github.com/dudedankdave/docker-reskate-server/tree/main/plugins/tools
