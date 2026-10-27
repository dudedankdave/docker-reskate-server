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

**Cocojambo Deathraces** is our custom throwdown mode (the Cocojambo card, separate from other
Deathrace modes). Choose a track from the server's map list and
set the racer count. Circuit tracks also prompt for laps before the flag is placed. A track is a
row of glowing neon-blue gates; racers skate through
them in order, and a gate only counts when you go through its frame the right way.

The track list comes from tracks saved by staff on the current map. If none are listed in setup,
use **Refresh tracks** or ask staff to create a track with F7.

### The COJA menu (F5)

**F5** opens the COJA menu: our own panel in the COCOJAMBO look, drawn over the game. While it is open the skater
is held and the game gets none of your input, and it closes by itself when a race or a challenge starts.

| | Keyboard | Controller | Mouse |
|---|---|---|---|
| Move between rows | arrows up/down | d-pad up/down | point |
| Switch tabs | Tab, or left/right on the tab row | LB / RB, or left/right on the tab row | click a tab |
| Change a choice | left/right | d-pad left/right | click < or > |
| Press | Enter or Space | A | click |
| Close | F5 or Esc | B | click X |

The tabs:

- **HOME**: your profile: the COCOJAMBO logo, your name, role and crew, your points, rank, time played and week on
  this server, and from the hub (reskate.cocojambo.net) your place on the ranked and unranked All Time boards, your
  crew's points and how many servers are up.
- **RACES**: choose a track, laps, grid time, racer count and **who can join** (everyone, your crew, or the
  players you tick) and place the race flag; while your race waits, start it or call it off. Below: every challenge on the map with its record and distance; **Run** puts you on its start.
- **ADMIN** (staff): Track Studio: new, edit, delete and challenge on/off for the map's tracks; while editing,
  buttons place, move, select, turn, resize and delete gates, switch circuit/sprint and save.
- **SETTINGS**: the points chip, its side, the notes and the HUD's size; the controls.

The hub knows players by the name its boards show and lists each board's top 50: a player below that shows as
not on the board yet.

### Race

1. Open **Throwdowns** and pick **Cocojambo Deathraces**. Choose a track, racer limit, grid time, and
   laps for a circuit.
2. Place the flag. It shows on the map like every throwdown flag, with the track, laps and gates on
   its card. Other players join it as usual; everyone on the flag sees the track's gates.
3. Press **Start**. Everyone is put on the grid behind the start gate, facing through it. The game's
   own intro card names the track while everyone lines up, then its 3-2-1 and **GO**. The game uses stock physics during the race.
4. Go through every gate in order: the bright one with the arrow is next, the dim one after it.
   A gate you miss has to be taken again: no gate can be skipped. Standings are on the right.
5. The results page shows positions, times, gaps or best laps once everyone finished, one minute
   after the winner, or after 15 minutes.

**F10** leaves the race, a flag you joined, or closes the results.

### Challenges

A track an admin marks as a **challenge** is there all the time, no host needed: its start has a sun-yellow
beacon with the track's name and record over it. Skate to it and press **F8** (or **Run** in F5 > RACES): the game counts down and you race
it alone, timed. The server keeps each challenge's top 10 (every player's best), tells you your time and place,
and tells everyone when the record falls. **F10** stops a run.

On the in-game map every challenge is a flag too: its card names the challenge, its record and its gates, and
**Enroll** runs it. The game only shows flags led by another player, so they show once someone else is on the server.

### Make tracks (admins)

Tracks are kept on the server for each map, and only its staff (admins, and roles marked staff)
can make, change or delete them. Everyone gets them when they join.

| Key | |
|---|---|
| **F7** | Open or close Track Studio; while editing, save the track |
| **F5** | The COJA menu: Track Studio with the mouse |
| **PgUp / PgDn**, **F6** | Select a track in the library or a gate while editing |
| **F8** | New track in the library; add a gate 3 metres ahead while editing; start a nearby challenge outside the editor |
| **6** | Edit the selected track in the library; move the selected gate 3 metres ahead while editing |
| **7** | Toggle a challenge in the library; switch circuit/sprint while editing |
| **F9** | Delete the selected track or gate, depending on the screen |
| **1** | Switch what 2/3/4/5 do: MOVE, HEIGHT & TURN, or SIZE |
| **F10** | Close Track Studio |

Gate 1 is the start. Place it facing the way the race goes: the grid faces where you faced. A gate stands on the
ground under you, also when you place it mid-air. Moving a challenge's gates starts its times over. The selected gate is yellow; the arrows show the way through each
gate.

On a controller, use **LB+RB+A** to add a gate (in the library: a new track), **LB+RB+X** to move the selected gate, and **LB+RB+B** to delete it. **LB+Left/Right** selects a gate. Hold **RB** and press the d-pad to adjust it: the current mode decides whether that moves, turns, or resizes the gate. **LB+RB+Y** changes mode; **LB+RB+Start** switches circuit/sprint. New gates are placed ahead of the admin so the frame does not surround their skater. A gate is two posts and a top bar: no line along the ground, which would show through the floor.

The controller combos can be changed on ReSkate's Binds page; the keyboard keys above are fixed.

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

### Skate 3's races as tracks

`coco import <map> <base64>` saves race tracks made elsewhere for a map: a track or `{"tracks": [...]}`, as the
server stores them (`{"name", "author", "circuit", "challenge", "gates": [{"p": [x, y, z], "n": [nx, nz], "w", "h",
"heading"}]}`). Each is checked as Track Studio's are, and replaces the map's track of the same name.

`deathrace_tracks.py` (in the plugin's source, not in the package) makes those lines from Skate 3's ten races, as the
Skate 3 Improved map (333-Skate_3_Improved) ships them for its Deathraces in `data/races.json`: gate 1 a start line
in front of the original start slots, then one gate per checkpoint.

```
python plugins/tools/deathrace_tracks.py <Mods>/333-Skate_3_Improved/data/races.json --map bam [--challenge]
```

It prints one `coco import` line per race for the server's console. A race is about 1.2 KB, too long for the bridge's
console (300 characters), so the servers get them with the plugin instead: `--bundle` writes them into
`plugin/tracks/bundled.lua` (by map), and the plugin's server copy adds each to its map once when it starts (a track
staff delete or rename stays so). The bundle holds Skate 3's ten races for the Skate 3 map (`bam`), from
333-Skate_3_Improved 1.1.8's `data/races.json` (Skate 3's own races, ported by 333).

```
python plugins/tools/deathrace_tracks.py races.json --bundle --map bam
```

Source: https://github.com/dudedankdave/docker-reskate-server/tree/main/plugins/tools
