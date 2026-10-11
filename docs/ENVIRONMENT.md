# Environment variables

All settings of the [ReSkate server image](../README.md), one table per topic. [`example.env`](../example.env) has the same variables as a starting point.

- They are written to `/data/ReSkateServer.json` on every start (older configs are moved to the 1.1.7 layout first).
- **Unset or empty variables leave the existing value alone**, so changes made in-game or from the console survive restarts. Use `off` where a table lists it to clear a value.
- Values containing `#` or `'` must be double-quoted in env files. JSON values (`VOTES_CUSTOM`, `COMMANDS`) are single-quoted.
- With several `env_file` entries, a later one wins over an earlier one. Settings that differ per server go into `serverN.env`, see [Multi-server support](../README.md#multi-server-support).
- Settings marked "Pins the list" replace in-game changes on every restart.

[Per server](#per-server) · [Access](#access) · [Gameplay](#gameplay) · [Anti-cheat](#anti-cheat) · [Voice](#voice) · [Network](#network) · [Voting](#voting) · [Announcements](#announcements) · [Parks and layers](#parks-and-layers) · [Mods](#mods) · [Updates](#updates) · [Discord](#discord) · [Leaderboard](#leaderboard)

<br/>

## Per server

| Variable | Values | Description |
|---|---|---|
| `SERVER_NAME` | text, 1-64 chars | Name in the server browser. Only ASCII letters, numbers, spaces and `- _ / [ ] ( )`. Anything else is cleaned automatically (accents removed, `' . # %` dropped, others become `-`), with a `[config]` log line. |
| `MAP` | text | Map everyone skates: `San Vansterdam`, `Isle of Grom`, `Super Ultra Mega Resort`, `Stadium 1`, or the `displayName` of a custom map mod (`reskate-levels.json`). |
| `MAP_POOL` | map list, `off` | Maps players may vote for and the rotation goes through, in order, e.g. `San Vansterdam,Isle of Grom,Skate2Map`. Empty = every map; admins can still pick any. `off` empties it. Pins the list (in-game changes are replaced on restart). Unknown names stop the server (`Config problem`). |
| `MAP_ROTATION_MINUTES` | minutes, `0` = off | Minutes on each map before the server moves to the next one in `MAP_POOL`. Players get a minute's warning; the clock waits while nobody is on and starts over whenever the map changes (by a vote or an admin too). |
| `PORT` | number | Game port. Only used with `USE_STEAM_RELAY=false`. |
| `QUERY_PORT` | number | Server browser / A2S queries. |
| `STEAM_TOKEN` | token, `off` | Steam game server token: keeps the Steam ID across restarts (not always the join code). One per server from steamcommunity.com/dev/managegameservers (App ID 3354750) and keep it private. Empty = anonymous sign-in, a new Steam ID on every start. `off` clears it. |

<br/>

## Access

| Variable | Values | Description |
|---|---|---|
| `SERVER_PASSWORD` | text, `off` | Join password. Empty = anyone can join; `off` clears an existing password. |
| `LISTED` | `true` / `false` | `false` hides the server from the browser; players then need the join code. |
| `MAX_PLAYERS` | 1-249 | Player limit. |
| `ADMINS` | SteamID64 list | Comma-separated admins who may change settings in-game. Merged. |
| `BANS` | `id[:name]` list | Comma-separated players who can never join. Merged into `/data/data/bans.json`. |
| `GLOBAL_BANS` | `true` / `false` | Turn away players the ReSkate team banned. Default `true`. |
| `WELCOME_MESSAGE` | text, `off` | Chat line sent to each player as they join. `off` clears it. |
| `CHAT_COLOR` | `#RRGGBB` | Colour of the server's "Server" badge and name in chat (default `#8E5CFF`). |
| `CHAT_TEXT_COLOR` | `#RRGGBB` | Colour of the text of the server's chat lines (default `#D9C8FF`). Pick one that reads on a dark background. |
| `RESERVED` | SteamID64 list | Players with a reserved slot (merged). They and admins can join a full server, on top of `MAX_PLAYERS`. |

<br/>

## Gameplay

| Variable | Values | Description |
|---|---|---|
| `OBJECT_PLACEMENT` | `everyone` `admins` `nobody` | Who can build and place objects. |
| `OBJECT_LIMIT` | 0-1024 | Objects each player may have placed (default `100`), `0` = no limit. Admins are never limited. A player placing more than twice the limit plus 100 in a minute has their objects deleted. |
| `OBJECT_SCALING` | `true` / `false` | Let players place objects bigger or smaller than their own size. Default `true`; admins can always resize. |
| `BONE_SCALE_LIMIT` | 0-8 | How far mods may scale skater bones (big heads) for others. Default `2` (`1` also evens out height), `0` = no limit. |
| `BONE_REACH_LIMIT` | 0.5-20, `0` | How far (metres) a bone of a skater's body or board may be from the one it hangs from, for the other players. Stops a hacked game from stretching its skater across the map; an ordinary player never gets near it. Default `1`, `0` = no limit. |
| `NOCLIP` | `true` / `false` | Let players use noclip (and tp). Default `true`, admins always can. |
| `NO_BAIL` | `true` / `false` | Let players use No Bail. Default `true`, admins always can. |
| `BOOSTS` | `true` / `false` | Let players use the forward and up boosts. Default `true`, admins always can. |
| `ENFORCE_TUNING` | `true` / `false` | Players skate with the game's own physics tuning, not edited copies. Default `true`. |
| `PARTIES` | `true` / `false` | Let players form parties. Default `true`. |
| `PARTY_SIZE` | 2-8 | Most players in one party. Default `8`. |
| `ANNOUNCE_THROWDOWNS` | `true` / `false` | Tell everyone in chat when a throwdown drop is placed. Default `true`. |
| `ANNOUNCE_KICKS` | `true` / `false` | Tell everyone in chat when an admin kicks or bans a player ("Server: Name was kicked."). ReSkate itself only tells the admin. Done by the image, not the server: it types `say` into the server console, which `docker attach` still reaches. Default `true`. |
| `WORD_WARNINGS` | 0-10 | Chat messages with a word from the ReSkate team's list are never passed on and their player is warned; after this many warnings the next one gets them kicked (they can rejoin). Warnings last until the server restarts. Default `3`, `0` = never warn or kick. |
| `AFK_KICK_MINUTES` | 0-1440 | Remove a player who has been away (not moving, speaking, chatting or editing objects) this many minutes, after a warning a minute before. Default `0` = never; admins are never removed. |
| `SYNC_EFFECTS` | `true` / `false` | Let players see each other's skater effects (sparks, dust, costume and board trails). `false` saves a little traffic on busy servers. Default `true`. |
| `ACTIVITY_LOG` | `true` / `false` | Log what players do: throwdowns, joins, objects placed or removed, load times. Default `true`. |

<br/>

## Anti-cheat

| Variable | Values | Description |
|---|---|---|
| `SPEED_CHECK` | `off` `warn` `kick` | Catch players whose game runs faster than normal (speedhack). `warn` (default) takes them out of throwdowns and coop challenges and tells admins. |
| `SCORE_CHECK` | `off` `warn` `kick` | Catch players whose mods change trick scoring or handling. `warn` (default) takes them out of throwdowns and coop challenges. |
| `SCORE_ALLOW` | hex list | Comma-separated scoring fingerprints (16 hex digits) accepted like the game's own, for servers running a scoring mod everyone installs. Merged with the existing list. |

<br/>

## Voice

| Variable | Values | Description |
|---|---|---|
| `VOICE_CHAT` | `true` / `false` | Allow voice chat. |
| `VOICE_RANGE` | 50-1000 | How far proximity voice reaches, in metres. |

<br/>

## Network

| Variable | Values | Description |
|---|---|---|
| `USE_STEAM_RELAY` | `true` / `false` | `false`: players connect straight to `PORT` (UDP, must be open) for lower ping, falling back to Steam's relays. Default `true` (relays only). |
| `SEND_RATE` | 128-16384 | Most the server sends one player, KB/s (default `900`). About 1100 is what reaches a player through Steam's relays: set higher, packets are lost and resent. |
| `CROWD_BUDGET` | number, `0` = no limit | Most position updates per second one player is sent (default `600`). Only matters in crowds (about 30 players): the farthest drop to 10 and 5 updates a second. |
| `PACK_MS` | 0-50 | ms a message may wait to share a packet (default `10`, `0` = send at once). |
| `FINGER_DISTANCE` | metres | Beyond this a player's fingers aren't sent (default `25`, `0` = always). |
| `THREADS` | 0-64 | Threads that build each player's updates (default `0` = one per processor but one, up to 8; `1` = single thread). The log says how many on start. |
| `STEAM_DEBUG` | `true` / `false` | Log Steam networking details (connection problems). Verbose. |
| `DISTANCE_FULL_RATE_RETURN` | metres | Players closer than this are back at the full update rate. |
| `DISTANCE_HALF_RATE_START` | metres | Players farther than this update at half rate. |
| `DISTANCE_HALF_RATE_RETURN` | metres | Players closer than this are back at half rate. |
| `DISTANCE_LOW_RATE_START` | metres | Players farther than this update at the low rate. |

<br/>

## Voting

| Variable | Values | Description |
|---|---|---|
| `VOTE_MAP_ENABLED` | `true` / `false` | Players can vote for a map change (`/vote map <map>`). Off until turned on. |
| `VOTE_MAP_PERCENT` | number | Share of connected players whose yes passes a map vote. |
| `VOTE_KICK_ENABLED` | `true` / `false` | Players can vote to kick someone (`/vote kick <player>`). Admins cannot be vote-kicked. |
| `VOTE_KICK_PERCENT` | number | Share of connected players whose yes passes a kick vote. |
| `VOTE_TIME_OF_DAY_ENABLED` | `true` / `false` | Players can vote on the time of day (`/vote tod <time>`). Needs `WORLD_LAYER_SYNC`. |
| `VOTE_TIME_OF_DAY_PERCENT` | number | Share of connected players whose yes passes a time-of-day vote. |
| `VOTE_SECONDS` | seconds | How long a vote runs. Default `30`. |
| `VOTE_COOLDOWN_SECONDS` | seconds | How long a player waits before starting another vote. Default `60`. |
| `VOTE_<MAP\|KICK\|TIME_OF_DAY>_SECONDS` | seconds, `0` | That vote's own run time. `0` (default) = `VOTE_SECONDS`. |
| `VOTE_<MAP\|KICK\|TIME_OF_DAY>_COOLDOWN_SECONDS` | seconds, `0` | That vote's own cooldown. `0` (default) = `VOTE_COOLDOWN_SECONDS`. |
| `VOTE_<MAP\|KICK\|TIME_OF_DAY>_MIN_PLAYERS` | 1-249 | Players who must be on before anyone can start that vote. Default `1`. |
| `VOTE_STARTER_YES` | `true` / `false` | Whoever starts a vote has voted yes. Default `true`. |
| `VOTES_CUSTOM` | JSON list, `off` | Votes of your own, each running a server command when it passes, e.g. `[{"name": "restart", "description": "Reload the map", "command": "map {map}", "percent": 60}]`. `{map}` = current map, `{arg}` = the choice picked from `"choices"`. Players use `/vote restart`, `/vote list`. Up to 16, names 1-16 of `a-z 0-9 - _`. Each takes `enabled`, `percent`, `seconds`, `cooldown_seconds`, `min_players`. Pins the list; `off` clears it. Single-quote the value in env files. |
| `POLLS` | `off` `admins` `everyone` | Who may ask everyone a question (`/poll Next map? \| Grom \| Stadium`, answers with `/1`, `/2`...). Default `admins`. |
| `POLL_SECONDS` | seconds | How long a poll runs. Default `60`. |

<br/>

## Announcements

| Variable | Values | Description |
|---|---|---|
| `ANNOUNCEMENTS` | `\|`-separated lines, `off` | Chat lines the server posts in turn while players are on, e.g. `Join the Discord: discord.gg/xyz\|Be nice`. Pins the list (console `announcements add` is replaced on restart); `off` clears it. |
| `ANNOUNCEMENT_INTERVAL_MINUTES` | minutes, `0` = off | Minutes between announcements. Default `0`. |
| `COMMANDS` | JSON list, `off` | Chat commands of your own, e.g. `[{"name": "discord", "reply": "Join us: discord.gg/xyz"}, {"name": "rules", "command": "announce-to {player} No griefing!"}]`. Each has a `name` (1-16 of `a-z 0-9 - _`) and a `reply` (chat line back), a `command` (server command or list of up to 8, run as the console; `{player}` = SteamID64, `{map}`, `{arg}`) or both, and `admin` (default `false`). Up to 32; not listed in `/help`. Pins the list; `off` clears it. Single-quote the value in env files. |

ReSkate 2.0.3 always shows announcements as a card at the top of the screen; `ANNOUNCEMENT_CARD` is ignored since then.

<br/>

## Parks and layers

| Variable | Values | Description |
|---|---|---|
| `PARK_CONSTRUCTION` | park id | Layout of the construction park lot, e.g. `skatepark_01`, or `empty`. |
| `PARK_HISTORIC` | park id | Layout of the historic park lot, e.g. `megapark_05`, or `empty`. |
| `PARK_FINANCIAL` | park id | Layout of the financial park lot, e.g. `flumppark_08`, or `empty`. |
| `WORLD_LAYER_SYNC` | `true` / `false` | Force the `LAYERS` below on every player. |
| `LAYERS` | `key=on\|off\|default` list | World layers, comma-separated, e.g. `key=on,other=off`. `default` removes the setting. |

<br/>

## Mods

| Variable | Values | Description |
|---|---|---|
| `MODS` | package list | Thunderstore packages to install into `/data/Mods` on start: `Owner-Name`, `Owner-Name-1.2.3` or a package URL, comma or space separated. See [Custom maps and mods](../README.md#custom-maps-and-mods). |
| `MODS_UPDATE` | `true` / `false` | `false` keeps installed versions even when a newer one exists (pinned entries and missing packages are still installed). Default `true`. |

<br/>

## Updates

| Variable | Values | Description |
|---|---|---|
| `UPDATE_MODE` | `pinned` / `auto` | `pinned` (default): the server in the image runs, the image tag is the version; the `updates-admin` feed (else `admin`) says when a newer release is out. `auto`: the server runs from `/data/server` and new ReSkate releases are installed there (they survive a recreate; a newer image replaces them). `AUTO_UPDATE=true` is the older name of `auto`. |
| `UPDATE_POLICY` | `instant` / `timed` / `ask` / `scheduled` | When `auto` installs a new release. `instant`: right away. `timed` (default): in-game `announce` countdown of `UPDATE_COUNTDOWN` minutes, then install. `scheduled`: at `UPDATE_SCHEDULE`, then the countdown. `ask`: a Discord bot asks for approval (below), then the countdown. The countdown is skipped while nobody is on. |
| `UPDATE_COUNTDOWN` | minutes, `0`-`1440` | Countdown before the restart (default `10`), announced at the start, 5 and 1 min. |
| `UPDATE_SCHEDULE` | `HH:MM` or `days HH:MM` (UTC) | For `scheduled`, e.g. `04:00` or `sat,sun 04:00`. |
| `UPDATE_CHECK_MINUTES` | `5`-`1440` | How often the release is checked (default `30` in `auto` or with an `updates` webhook, else `180`). |
| `DISCORD_BOT_TOKEN` | bot token | For `ask`: a Discord bot (no intents needed) in your server that can post, react and read reactions in `DISCORD_APPROVAL_CHANNEL`. Keep it private. |
| `DISCORD_APPROVAL_CHANNEL` | channel id | Where the bot asks. It adds ✅ and ❌; the first reaction by one of `DISCORD_MENTION_IDS` (anyone, if unset) decides. ❌ skips that version. |

- Typing `update` in the console (`docker attach`) installs the latest release at once, in any policy.
- Downloads come from the release's `launcher.json` and are checked against its SHA-256 before anything is replaced. The previous version is kept in `/data/server.old`; if the new server stops within 3 minutes it is rolled back and the `updates-admin` feed (else `admin`) says so.
- The container keeps running through an update: the entrypoint runs the server as a child and restarts it. Console input is passed through as before.

<br/>

## Discord

Webhooks come in pairs that share a suffix (`1`, `2`, `admin`, ...); add as many as you need. What each scope gets is described under [Discord webhook](../README.md#discord-webhook).

| Variable | Meaning |
|---|---|
| `WEBHOOK_URL_<n>` | A webhook URL (Discord: channel settings, Integrations, Webhooks). |
| `WEBHOOK_SCOPE_<n>` | What it gets: `admin`, `log`, `public`, `chat`, `leaderboard`, `updates`, `updates-admin`, comma-separated. |
| `DISCORD_MENTION_IDS` | Comma-separated Discord user ids pinged in the `admin` and `updates-admin` scopes (releases, failures, approvals) and allowed to approve updates. |
| `DISCORD_USERNAME` | Name shown on the posts. Default is `SERVER_NAME` without any `discord...` word, which Discord rejects in webhook names. |

The older names still work, next to the new ones: `DISCORD_WEBHOOK_ESSENTIALS` = admin, `DISCORD_WEBHOOK_LOG` = log + chat, `DISCORD_WEBHOOK_PUBLIC` = public, `DISCORD_WEBHOOK_CHAT` = chat, `LEADERBOARD_WEBHOOK` = leaderboard, `DISCORD_WEBHOOK_ADMIN` / `DISCORD_WEBHOOK` = admin + log + chat (`DISCORD_CONSOLE=false` drops log and chat), `DISCORD_WEBHOOK_USER` = public. So the older names keep the chat in their console as before. Update approval needs a bot, see [Updates](#updates).

<br/>

## Leaderboard

How ranking and points work: [Ranked and leaderboard](../README.md#ranked-and-leaderboard).

| Variable | Meaning |
|---|---|
| `LEADERBOARD` | `true` to turn it on (default off). |
| `LEADERBOARD_INTERVAL` | Minutes between posts, 1-1440 (default 60). |
| `LEADERBOARD_SIZE` | Players listed in chat, 1-10 (default 5). |
| `LEADERBOARD_POINTS` | Points per place, comma-separated; the last value is for every later place (default `10,6,4,2`). |
| `LEADERBOARD_SCOPE` | `shared` (default): one board for every server that mounts the same folder at `/shared`, e.g. `- /root/reskate/shared:/shared` on each one (writable by uid 1000). Without that mount the server keeps its own board and says so in its log. `server`: always this server's own board, in `/data`. |
| `LEADERBOARD_FILE` | Use another file than `/shared/leaderboard.json` / `/data/leaderboard.json`. The shared file is locked while changed, and only one server posts it to Discord per interval. |
| `RANKED_MESSAGE` / `UNRANKED_MESSAGE` | The DMs, to replace the English defaults. `{mods}` in `UNRANKED_MESSAGE` is the mods the server named. At most 200 bytes. |
