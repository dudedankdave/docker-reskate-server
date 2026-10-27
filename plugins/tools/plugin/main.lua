-- COJA tools (COCOJAMBO-TOOLS): the COCOJAMBO servers' plugin. The HUD (corner tag, panel, notes from the server)
-- is in hud/ and loads first; the rest of this file is the races.
local hud, hud_server = nil, nil
if reskate.side == "server" then hud_server = require("hud/server") else hud = require("hud/client") end

-- Races: gate races as a ReSkate throwdown mode (contrib/throwdown-kit.md in ReSkate).
--
-- A track is a row of gates: glowing neon frames the racers skate through, in order. Gate 1 is
-- the start; the last is the finish, or in a circuit gate 1 is the start/finish line of every
-- lap. Tracks are the server's: its copy of this plugin keeps them per map and only its staff
-- (admins and roles marked staff) may make, change or delete them, in game with the track editor
-- (F7). Everyone gets the map's tracks when they join.
--
-- A race is hosted from its card on the Throwdowns page: the setup page picks the track and laps,
-- the native flag gathers the racers (it is on the map like every throwdown flag), and the host's
-- Start sends everyone the track. Each racer's game lines its skater up on the grid behind the
-- start gate, runs the start lights, checks its own gates and tells the others its progress; the
-- results page follows the finish.

local R = reskate

local MODE = "deathraces"
local TITLE = "COCOJAMBO DEATHRACES"
local MAX_GATES = 40             -- a "go" message carries the track and stays under 4 KB
local MAX_TRACKS = 20            -- per map
local LAPS = {1, 2, 3, 5}
local GRID_MS = 2500             -- lining up on the grid
local GRID_DELAYS = {0, 2500, 5000}
local COUNT_MS = 3000            -- the game's own 3-2-1, then GO (the kit's hud.countdown)
local GRACE_MS = 60000           -- the rest's time once the first racer finished
local MAX_RACE_MS = 15 * 60 * 1000
local RESULTS_MS = 25000         -- the results page stays this long (F10 closes it)
local OFFER_EVERY_MS = 2000      -- a host repeats its flag for players who arrive later
local OFFER_TTL_MS = 7000
local HELLO_EVERY_MS = 10000     -- asking the server for the tracks until it answers
local JUMP_SPEED = 80            -- m/s between two ticks: faster is a teleport
local SEND_BUDGET = 12000        -- bytes a second (the plugin limit is 16 KB)

-- The COCOJAMBO look (lib/theme.lua): the gates glow in the logo's sky blue, the selected one in its sun yellow.
local theme = require("lib/theme")
local NEON = {"#87CFE422", "#87CFE455", "#87CFE4AA", "#E6F6FBFF"}
local NEON_DIM = {nil, "#87CFE418", "#87CFE440", "#E6F6FB60"}
local SELECTED = {"#FFD97222", "#FFD97255", "#FFD972AA", "#FFF4D6FF"}
local THICK = {18, 10, 5, 2}

-- ---- helpers ------------------------------------------------------------------------------------

local function now() return R.time.now() end
local function toast(text, level) pcall(R.ui.toast, text, level or "info") end
local function sound(name) pcall(R.audio.play_ui, name) end

-- Messages leave through a queue that keeps under the plugin's bytes a second.
local outbox, sent_second, sent_bytes = {}, 0, 0
local function flush()
    local t = now()
    if t - sent_second >= 1000 then sent_second, sent_bytes = t, 0 end
    while #outbox > 0 and sent_bytes + #outbox[1].text <= SEND_BUDGET do
        local m = table.remove(outbox, 1)
        sent_bytes += #m.text + 14
        local ok, why = pcall(R.net.send, m.to, m.text)
        if not ok then R.log("a message was not sent: " .. tostring(why)) end
    end
end
local function send(to, message)
    table.insert(outbox, {to = to, text = R.json.encode(message)})
    flush()
end

local function v3(p) return vector.create(p[1], p[2], p[3]) end
local function round(n, step) return math.floor(n / step + 0.5) * step end
local function dot(a, b) return a.x * b.x + a.y * b.y + a.z * b.z end
local function distance(a, b)
    local dx, dy, dz = a.x - b.x, a.y - b.y, a.z - b.z
    return math.sqrt(dx * dx + dy * dy + dz * dz)
end
local function finite(n, most) return type(n) == "number" and n == n and math.abs(n) <= (most or 1e6) end

local function me()
    local ok, player = pcall(R.session.me)
    if ok and player then return player.id, player.name end
    return "local", "You"
end

local function clock_text(ms)
    if not ms then return "--:--.--" end
    local cs = math.floor(ms / 10)
    return string.format("%d:%02d.%02d", math.floor(cs / 6000), math.floor(cs / 100) % 60, cs % 100)
end

local function trim(text) return (tostring(text or ""):gsub("^%s+", ""):gsub("%s+$", "")) end

local function remove_value(list, value)
    local at = table.find(list, value)
    if at then table.remove(list, at) end
end

local function copy(value)
    if type(value) ~= "table" then return value end
    local out = {}
    for k, v in pairs(value) do out[k] = copy(v) end
    return out
end

-- The map the tracks are kept for: the level being played (reskate.level.current(), the map's own level asset, e.g.
-- dingolevel_reskate_skate3improved), as the game and the server both have it. session.world().map is only the base
-- world every modded map loads on top of ("bam" for all of them), so it is the fallback while no level is known.
local BASE_WORLDS = {bam = true, grom = true, ftue = true, stadium_1 = true, stadium_2 = true, mpr = true}
local function map_key()
    local map = ""
    local ok_level, level = pcall(function() return R.level.current() end)
    if ok_level and type(level) == "table" and type(level.name) == "string" then
        map = string.lower(level.name):match("([^/\\]+)$") or ""
    end
    if map == "" then
        local ok, world = pcall(R.session.world)
        map = ok and world and world.map or ""
    end
    map = map:gsub("[^%w_%-]", "_")
    if map == "" then map = "unknown" end
    return map:sub(1, 64)
end

-- ---- gates ----------------------------------------------------------------------------------------
-- A gate: {p = {x, y, z} (the middle of its bottom edge), n = {x, z} (the way through it, level),
-- w = width, h = height (metres), heading = the skater's heading when it was placed (gate 1: where
-- the grid faces)}.

local function normal(g) return vector.create(g.n[1], 0, g.n[2]) end
local function across(g) return vector.create(g.n[2], 0, -g.n[1]) end

local function clean_gate(g)
    if type(g) ~= "table" or type(g.p) ~= "table" or type(g.n) ~= "table" then return nil end
    local x, y, z, nx, nz = g.p[1], g.p[2], g.p[3], g.n[1], g.n[2]
    if not (finite(x) and finite(y) and finite(z) and finite(nx, 2) and finite(nz, 2)) then return nil end
    local len = math.sqrt(nx * nx + nz * nz)
    if len < 0.01 then return nil end
    return {
        p = {round(x, 0.01), round(y, 0.01), round(z, 0.01)}, n = {round(nx / len, 0.001), round(nz / len, 0.001)},
        w = math.clamp(round(tonumber(g.w) or 8, 0.1), 2, 30), h = math.clamp(round(tonumber(g.h) or 5, 0.1), 1.5, 12),
        heading = finite(g.heading, 100) and round(g.heading, 0.001) or 0,
    }
end

local function clean_track(t)
    if type(t) ~= "table" or type(t.gates) ~= "table" then return nil end
    local name = trim(t.name):sub(1, 32)
    if name == "" then return nil end
    local gates = {}
    for _, g in ipairs(t.gates) do
        local clean = clean_gate(g)
        if not clean then return nil end
        table.insert(gates, clean)
    end
    local circuit = t.circuit == true
    if #gates < (circuit and 3 or 2) or #gates > MAX_GATES then return nil end
    local best = nil
    if type(t.best) == "table" and finite(t.best.ms) and t.best.ms > 0 then
        best = {n = tostring(t.best.n or "?"):sub(1, 32), ms = t.best.ms}
    end
    return {id = type(t.id) == "string" and t.id:sub(1, 40) or nil, name = name, author = tostring(t.author or ""):sub(1, 40),
            circuit = circuit, gates = gates, challenge = t.challenge == true, best = best}
end

-- On the wire a gate is a list, {x, y, z, nx, nz, w, h, heading}: a race of 40 gates and 10
-- racers stays under a message's 4 KB.
local function pack(t)
    local gates = {}
    for i, g in ipairs(t.gates) do gates[i] = {g.p[1], g.p[2], g.p[3], g.n[1], g.n[2], g.w, g.h, g.heading} end
    return {id = t.id, name = t.name, author = t.author, circuit = t.circuit, challenge = t.challenge or nil, best = t.best, g = gates}
end

local function unpack_track(t)
    if type(t) ~= "table" or type(t.g) ~= "table" then return nil end
    local gates = {}
    for i, g in ipairs(t.g) do
        if type(g) ~= "table" then return nil end
        gates[i] = {p = {g[1], g[2], g[3]}, n = {g[4], g[5]}, w = g[6], h = g[7], heading = g[8]}
    end
    return clean_track({id = t.id, name = t.name, author = t.author, circuit = t.circuit, challenge = t.challenge, best = t.best, gates = gates})
end

-- Whether moving from `a` to `b` went through gate `g` the right way: `b` is past the gate's plane,
-- `a` was not, and where it crossed is inside the frame.
local function crossed(g, a, b)
    local p, n = v3(g.p), normal(g)
    local da, db = dot(a - p, n), dot(b - p, n)
    if not (da < 0 and db >= 0) then return false, false end
    local at = a + (b - a) * (da / (da - db))
    local side, up = dot(at - p, across(g)), at.y - p.y
    return math.abs(side) <= g.w / 2 + 0.5 and up >= -1.5 and up <= g.h + 0.5, true
end

-- The gates in the order they are passed: the start, then 2..n; a circuit then goes back through
-- gate 1 at the end of every lap.
local function course(track, laps)
    local seq = {1}
    for _ = 1, track.circuit and laps or 1 do
        for i = 2, #track.gates do table.insert(seq, i) end
        if track.circuit then table.insert(seq, 1) end
    end
    return seq
end

-- The way through a gate as a heading: the game's heading h faces (sin h, cos h) in x and z (the skater's +Z), so
-- the grid faces along the gate, whichever way the admin's skater looked when placing it.
local atan2 = math.atan2 or math.atan
local function gate_heading(g) return atan2(g.n[1], g.n[2]) end

-- The grid, a fair start: everyone the same distance behind the start gate, spread evenly across it. A second row
-- only when the gate is too narrow for all of them GRID_GAP apart; each row spread evenly too. `count` racers.
local GRID_SLOTS = 10
local GRID_GAP, GRID_MOST_GAP = 1.8, 3    -- metres between racers in a row: at least, at most
local GRID_BACK, GRID_ROW = 4, 3          -- metres behind the start gate, between rows
local function grid_slot(track, index, count)
    local g = track.gates[1]
    count = math.clamp(math.floor(tonumber(count) or index), math.max(index, 1), GRID_SLOTS)
    local usable = math.max(g.w - 1.5, 0)
    local per_row = math.max(1, math.min(count, math.floor(usable / GRID_GAP) + 1))
    local row = math.floor((index - 1) / per_row)
    local in_row = math.min(per_row, count - row * per_row)
    local gap = in_row > 1 and math.min(usable / (in_row - 1), GRID_MOST_GAP) or 0
    local offset = ((index - 1) % per_row - (in_row - 1) / 2) * gap
    local at = v3(g.p) - normal(g) * (GRID_BACK + row * GRID_ROW) + across(g) * offset
    return at + vector.create(0, 0.3, 0)
end

-- ---- the server's copy: the tracks ----------------------------------------------------------------

local function server_main()
    local function key(map) return "tracks:" .. tostring(map):gsub("[^%w_%-]", "_"):sub(1, 64) end
    local function tracks_of(map)
        local ok, list = pcall(R.storage.get, key(map))
        return ok and type(list) == "table" and list or {}
    end
    local function staff(player)
        local ok, role = pcall(function() return player.role end)
        return ok and type(role) == "table" and role.staff == true
    end
    -- A challenge's times: its top 10, each player once with their best ({id, n, ms}), per map and track.
    local function best_key(map, id) return "best:" .. key(map):sub(8) .. ":" .. tostring(id):sub(1, 40) end
    local function times_of(map, id)
        local ok, list = pcall(R.storage.get, best_key(map, id))
        return ok and type(list) == "table" and list or {}
    end
    local function send_tracks(to, map)
        local list, ids = tracks_of(map), {}
        for i, t in ipairs(list) do ids[i] = t.id end
        send(to, {t = "list", map = map, ids = ids})
        for _, t in ipairs(list) do
            local shown = copy(t)
            local first = t.challenge and times_of(map, t.id)[1]
            shown.best = first and {n = first.n, ms = first.ms} or nil
            send(to, {t = "track", map = map, track = pack(shown)})
        end
    end
    local next_id = 0

    local handlers = {}
    function handlers.hello(from, m)
        send(from.id, {t = "staff", staff = staff(from)})
        send_tracks(from.id, m.map)
    end
    function handlers.save(from, m)
        if not staff(from) then
            send(from.id, {t = "refused", why = "Only admins make tracks."})
            return
        end
        local track = unpack_track(m.track)
        if not track then
            send(from.id, {t = "refused", why = "That track is not valid."})
            return
        end
        local list = tracks_of(m.map)
        local at = nil
        for i, t in ipairs(list) do
            if t.id == track.id or t.name == track.name then at = i end
        end
        if not at and #list >= MAX_TRACKS then
            send(from.id, {t = "refused", why = "This map has " .. MAX_TRACKS .. " tracks: delete one first."})
            return
        end
        if at then
            track.id = list[at].id
        else
            next_id += 1
            track.id = string.format("%d-%d", math.floor(now()), next_id)
        end
        track.author = from.name
        track.best = nil
        -- moved gates make it another course: its challenge times start over
        if at and R.json.encode(list[at].gates) ~= R.json.encode(track.gates) then R.storage.set(best_key(m.map, track.id), nil) end
        if at then list[at] = track else table.insert(list, track) end
        R.storage.set(key(m.map), list)
        send(from.id, {t = "saved", id = track.id, name = track.name})
        send_tracks("all", m.map)
    end
    function handlers.delete(from, m)
        if not staff(from) then
            send(from.id, {t = "refused", why = "Only admins delete tracks."})
            return
        end
        local list = tracks_of(m.map)
        for i, t in ipairs(list) do
            if t.id == m.id then
                table.remove(list, i)
                R.storage.set(key(m.map), list)
                R.storage.set(best_key(m.map, t.id), nil)
                send(from.id, {t = "deleted", name = t.name})
                send_tracks("all", m.map)
                return
            end
        end
    end

    -- Staff mark a track as a challenge: its start shows a marker to everyone, and F8 there runs it alone, timed.
    function handlers.challenge(from, m)
        if not staff(from) then
            send(from.id, {t = "refused", why = "Only admins set challenges."})
            return
        end
        local list = tracks_of(m.map)
        for _, t in ipairs(list) do
            if t.id == m.id then
                t.challenge = not t.challenge
                R.storage.set(key(m.map), list)
                send(from.id, {t = "challenge_set", name = t.name, on = t.challenge})
                send_tracks("all", m.map)
                return
            end
        end
    end
    -- A challenge run's time, from the player's game (which checks the gates, as in every race): kept when it
    -- is their best, logged for the server's tools, a new record told to everyone.
    function handlers.challenge_time(from, m)
        local ms = tonumber(m.ms)
        if not ms or ms < 1000 or ms > MAX_RACE_MS then return end
        local track = nil
        for _, t in ipairs(tracks_of(m.map)) do
            if t.id == m.id and t.challenge then track = t end
        end
        if not track then return end
        local times = times_of(m.map, track.id)
        local record = not times[1] or ms < times[1].ms
        local mine = nil
        for i, e in ipairs(times) do
            if e.id == from.id then mine = i end
        end
        local improved = not mine or ms < times[mine].ms
        if improved then
            if mine then table.remove(times, mine) end
            table.insert(times, {id = from.id, n = tostring(from.name):sub(1, 32), ms = ms})
            table.sort(times, function(a, b) return a.ms < b.ms end)
            while #times > 10 do table.remove(times) end
            pcall(R.storage.set, best_key(m.map, track.id), times)
        end
        local rank = nil
        for i, e in ipairs(times) do
            if e.id == from.id then rank = i end
        end
        pcall(R.console.print, string.format("[coco race] challenge %s %s %d %s %s", key(m.map):sub(8), track.id, math.floor(ms), from.id, tostring(from.name)))
        send(from.id, {t = "challenge_result", name = track.name, ms = ms, rank = rank, improved = improved,
                       best = times[1] and {n = times[1].n, ms = times[1].ms} or nil})
        if record then
            send("all", {t = "challenge_record", map = m.map, name = track.name, who = tostring(from.name), ms = ms})
            send_tracks("all", m.map)
        end
    end

    -- A racer whose game could not put them on the grid asks the server to: it moves them, but only to a grid
    -- slot of a track saved on this map, and at most once every 2 s.
    local gridded_at = {}
    local function move(player, at)   -- reskate.server.move(player, position): no heading
        local ok, moved, why = pcall(R.server.move, player.id, at)
        if not ok then return false, tostring(moved) end
        if moved == false then return false, tostring(why or "refused") end
        return true
    end
    function handlers.grid(from, m)
        local t = now()
        if gridded_at[from.id] and t - gridded_at[from.id] < 2000 then return end
        gridded_at[from.id] = t
        local track = nil
        for _, one in ipairs(tracks_of(m.map)) do
            if one.id == m.id then track = one end
        end
        if not track then
            send(from.id, {t = "gridded", race = m.race, ok = false, why = "the track is not on the server"})
            return
        end
        local slot = math.clamp(math.floor(tonumber(m.slot) or 1), 1, GRID_SLOTS)
        local ok, why = move(from, grid_slot(track, slot, tonumber(m.count) or slot))
        pcall(R.console.print, string.format("[coco race] grid %s slot %d on %s: %s", tostring(from.name), slot, track.name,
            ok and "moved" or ("not moved, " .. tostring(why))))
        send(from.id, {t = "gridded", race = m.race, ok = ok, why = why})
    end

    -- What a player's game says about the calls the races make (teleport, keys, the setup page), for the server's log.
    local diags = {}
    function handlers.diag(from, m)
        diags[from.id] = (diags[from.id] or 0) + 1
        if diags[from.id] > 50 then return end
        local line = tostring(m.text):gsub("%c", " "):sub(1, 300)
        pcall(R.console.print, "[coco race] diag " .. tostring(from.name) .. ": " .. line)
    end

    -- `coco import <map> <base64 json>`: tracks made elsewhere (plugins/tools/deathrace_tracks.py: Skate 3's races),
    -- typed by the server's tools. A track or {tracks = [...]}, as stored; each is checked as Track Studio's are and
    -- saved under its name: one of that name is replaced (its challenge times start over when its gates moved).
    hud_server.commands.import = function(args)
        local map = tostring(args[2] or ""):gsub("[^%w_%-]", "_"):sub(1, 64)
        local value = map ~= "" and args[3] and hud_server.payload(args[3], "import")
        if not value then return R.console.print("error: coco import <map> <base64 json>") end
        local list, saved = tracks_of(map), {}
        for _, t in ipairs(type(value.tracks) == "table" and value.tracks or {value}) do
            local track = clean_track(t)
            local at = nil
            for i, one in ipairs(list) do
                if track and one.name == track.name then at = i end
            end
            if not track then
                R.console.print("error: coco import: not a valid track: " .. tostring(type(t) == "table" and t.name or t))
            elseif not at and #list >= MAX_TRACKS then
                R.console.print("error: coco import: " .. map .. " has " .. MAX_TRACKS .. " tracks, " .. track.name .. " left out")
            else
                if at then
                    track.id = list[at].id
                    if R.json.encode(list[at].gates) ~= R.json.encode(track.gates) then R.storage.set(best_key(map, track.id), nil) end
                    list[at] = track
                else
                    next_id += 1
                    track.id = string.format("%d-%d", math.floor(now()), next_id)
                    table.insert(list, track)
                end
                track.best = nil
                table.insert(saved, track.name)
            end
        end
        if #saved == 0 then return end
        R.storage.set(key(map), list)
        R.console.print(string.format("[coco race] imported %d track(s) on %s: %s", #saved, map, table.concat(saved, ", ")))
        send_tracks("all", map)
    end

    -- The tracks the plugin brings (plugin/tracks/bundled.lua, from deathrace_tracks.py --bundle: Skate 3's races),
    -- added to their map once each when the server starts ("seeded:<map>" keeps which): one staff delete or rename
    -- stays so, and a track of the same name already there is left as it is.
    local function seed_bundled()
        local ok, text = pcall(require, "tracks/bundled")
        if not ok or type(text) ~= "string" then return end
        local ok_json, maps = pcall(R.json.decode, text)
        if not ok_json or type(maps) ~= "table" then return R.console.print("error: tracks/bundled is not JSON by map") end
        for map, bundle in pairs(maps) do
            map = tostring(map):gsub("[^%w_%-]", "_"):sub(1, 64)
            local seeded_key = "seeded:" .. map
            local seeded = R.storage.get(seeded_key)
            if type(seeded) ~= "table" then seeded = {} end
            local list, added = tracks_of(map), {}
            for _, t in ipairs(type(bundle) == "table" and bundle or {}) do
                local track = clean_track(t)
                if track and not seeded[track.name] then
                    seeded[track.name] = true
                    local there = false
                    for _, one in ipairs(list) do
                        if one.name == track.name then there = true end
                    end
                    if not there and #list < MAX_TRACKS then
                        next_id += 1
                        track.id = string.format("%d-%d", math.floor(now()), next_id)
                        track.best = nil
                        table.insert(list, track)
                        table.insert(added, track.name)
                    end
                end
            end
            R.storage.set(seeded_key, seeded)
            if #added > 0 then
                R.storage.set(key(map), list)
                R.console.print(string.format("[coco race] added %d bundled track(s) on %s: %s", #added, map, table.concat(added, ", ")))
            end
        end
    end
    -- Before 0.2.7 tracks were kept per base world ("bam" for every modded map): a server playing a map of its own
    -- takes what is kept there (tracks, challenge times, the bundle's record) as its map's, once.
    local function move_base_world()
        local here = map_key()
        if BASE_WORLDS[here] or here == "unknown" or #tracks_of(here) > 0 then return end
        for base in pairs(BASE_WORLDS) do
            local moved = tracks_of(base)
            if #moved > 0 then
                local ok, keys = pcall(R.storage.keys)
                for _, k in ipairs(ok and type(keys) == "table" and keys or {}) do
                    local id = type(k) == "string" and k:match("^best:" .. base .. ":(.+)$")
                    if id then
                        R.storage.set(best_key(here, id), R.storage.get(k))
                        R.storage.set(k, nil)
                    end
                end
                R.storage.set(key(here), moved)
                R.storage.set(key(base), nil)
                R.storage.set("seeded:" .. base, nil)
                R.console.print(string.format("[coco race] moved %d track(s) from the base world %s to %s", #moved, base, here))
                return
            end
        end
    end
    -- Both once the server knows the level it plays (not always so while the plugin loads): the move first, so the
    -- bundle's tracks do not make the map look kept already. After 30 s without a level the bundle goes in anyway.
    local settled, settle_by = false, now() + 30000
    local function settle()
        if settled then return end
        local here = map_key()
        if (BASE_WORLDS[here] or here == "unknown") and now() < settle_by then return end
        settled = true
        pcall(R.console.print, "[coco race] map key " .. here)
        move_base_world()
        seed_bundled()
    end

    R.on("message", function(from, payload)
        local ok, m = pcall(R.json.decode, payload)
        if not ok or type(m) ~= "table" or type(m.map) ~= "string" then return end
        local handler = handlers[m.t]
        if handler then handler(from, m) end
    end)
    R.on("tick", function()
        flush()
        settle()
    end)
end

if R.side == "server" then
    server_main()
    return
end

-- ---- the player's copy: state ---------------------------------------------------------------------

local tracks = {}        -- the map's tracks, from the server
local tracks_map = nil   -- the map they are for
local staff = false
local answered = false   -- the server's copy answered
local last_hello = -HELLO_EVERY_MS

local editor = nil       -- {track, sel, mode, changed}
local admin_menu, admin_sel = false, 1
local setup = {track = 1, laps = 1, players = 6, grid_choice = 2, grid_delay = GRID_MS, access = 1, invited = {}}

-- Who can join a race: everyone, the host's crew (the crew tag the server's tools send each player with their
-- stats), or the players the host invited in the COJA menu (F5 > RACES). A private race's offer goes only to its
-- invited players; a crew race's to everyone, and each game shows it only to that crew; the host turns away the rest.
local ACCESS = {"all", "crew", "invite"}
local ACCESS_NAMES = {"Everyone", "My crew", "Invited players"}
local function my_team() return hud and hud.my_team() or nil end
local function online_players()   -- the other players here: {id, name}, by name
    local ok, list = pcall(R.session.players)
    local out, my_id = {}, me()
    for _, p in ipairs(ok and type(list) == "table" and list or {}) do
        local id = tostring(p.id or "")
        if id ~= my_id and not p.me and not p.server and id:match("^%d+$") then out[#out + 1] = {id = id, name = tostring(p.name or id)} end
    end
    table.sort(out, function(a, b) return a.name:lower() < b.name:lower() end)
    return out
end
local setup_open = false
local host = nil         -- this player's flag
local offers = {}        -- others' flags by race id
local joined = nil       -- {race, leader, name, track}: enrolled on someone else's flag
local race = nil         -- the race this player is in
local trail = {}         -- the skater's last positions, {at, pos}: which way they skate
local close_menu = function() end   -- the COJA menu's close (hud/menu.lua, loaded further down)
local release_hold                   -- the grid's hold let go (defined with the race below)

local function hello()
    last_hello = now()
    send("server", {t = "hello", map = map_key()})
end
local told_map = nil   -- the map key told to the server's log (diag below): it has to match the server's

-- Tells the server's log what this game made of a call (each different line once), so a test round shows it.
local diag_sent = {}
local function diag(text)
    text = tostring(text):sub(1, 300)
    if diag_sent[text] then return end
    diag_sent[text] = true
    send("server", {t = "diag", map = map_key(), text = text})
end
local function tell_map()
    local here = map_key()
    if here ~= told_map then
        told_map = here
        diag("map key " .. here)
    end
end
local function json_of(value)
    local ok, text = pcall(R.json.encode, value)
    return ok and text or tostring(value)
end

local function track_by_id(id)
    for i, t in ipairs(tracks) do
        if t.id == id then return t, i end
    end
end

-- ---- documents ------------------------------------------------------------------------------------

local function lap_index()
    for i, laps in ipairs(LAPS) do
        if laps == setup.laps then return i end
    end
    return 1
end

local function setup_document()
    local rows
    if #tracks > 0 then
        setup.track = math.clamp(setup.track, 1, #tracks)
        local t = tracks[setup.track]
        local names = {}
        for i, one in ipairs(tracks) do names[i] = one.name end
        rows = {
            -- Setup documents use one-based choice values; setup_row events return zero-based choices.
            {id = "track", kind = "choice", caption = "Track", options = names, value = setup.track},
            {id = "gates", kind = "info", caption = "Gates", label = string.format("%d  (%s)", #t.gates, t.circuit and "circuit" or "sprint")},
            {id = "grid", kind = "choice", caption = "Grid time", options = {"Quick", "Standard", "Relaxed"},
             value = setup.grid_choice or 1},
        }
        if t.circuit then
            local laps = {}
            for i, n in ipairs(LAPS) do laps[i] = tostring(n) end
            table.insert(rows, {id = "laps", kind = "choice", caption = "Laps", options = laps, value = lap_index()})
        end
        table.insert(rows, {id = "access", kind = "choice", caption = "Who can join", options = ACCESS_NAMES, value = setup.access})
        if t.author and t.author ~= "" then table.insert(rows, {id = "by", kind = "info", caption = "Made by", label = t.author}) end
    else
        rows = {
            {id = "none", kind = "info", caption = "Track", label = answered and "No tracks saved for this map" or "Waiting for the server..."},
            {id = "refresh_tracks", kind = "button", caption = "Track list", label = "Refresh tracks"},
        }
    end
    return {
        mode = MODE, title = TITLE, back = "BACK",
        description = "Choose a Cocojambo track, set the race size and laps, then skate the gates in order.",
        players = {caption = "Racers", min = 1, max = 10, step = 1, value = setup.players},
        rows = rows,
    }
end

local function host_racers()
    local list = {}
    for _, id in ipairs(host.order) do table.insert(list, {id, host.racers[id]}) end
    return list
end

local function laps_of(t, laps) return t.circuit and laps or 1 end
local function grid_label(ms) return string.format("%.1f s", (tonumber(ms) or GRID_MS) / 1000) end
local function setup_label(track, laps, grid_delay)
    local race_type = track.circuit and string.format("%d lap%s", laps, laps == 1 and "" or "s") or "Sprint"
    return race_type .. " · " .. grid_label(grid_delay) .. " grid"
end

-- The challenges as throwdown flags on the map (a map point, its bubble and a card): Enroll there runs it. The game
-- shows a flag only when its leader is another player here (one led by this player goes to the others instead,
-- throwdown_relay.cpp), so each game publishes its own, led by the online player with the lowest Steam ID; alone on
-- the server there is nobody to lead them, and they wait for someone to join.
local CHALLENGE_GAME = 7000      -- their game numbers: 7001 and up, one per challenge
local MAX_CHALLENGE_FLAGS = 8
local function flag_leader()
    local ok, list = pcall(R.session.players)
    if not ok or type(list) ~= "table" then return nil end
    local my_id, best = me(), nil
    for _, p in ipairs(list) do
        local id = tostring(p.id or "")
        if id ~= my_id and not p.me and not p.server and id:match("^%d+$") and (not best or #id < #best or (#id == #best and id < best)) then
            best = id
        end
    end
    return best
end
local function challenge_flags(flags)
    if race or host or joined then return end
    local leader = flag_leader()
    if not leader then return end
    local n = 0
    for _, t in ipairs(tracks) do
        if t.challenge and not t.loading and t.gates[1] and n < MAX_CHALLENGE_FLAGS then
            n += 1
            table.insert(flags, {
                id = "challenge:" .. tostring(t.id), mode = MODE, leader = leader, game = CHALLENGE_GAME + n,
                at = v3(t.gates[1].p), yaw = 0, players = 0, capacity = 1, open = true,
                stats = {{title = "CHALLENGE", value = t.name:sub(1, 32)},
                         {title = "RECORD", value = (t.best and (clock_text(t.best.ms) .. " " .. tostring(t.best.n)) or "none yet"):sub(1, 32)},
                         {title = "GATES", value = tostring(#t.gates)}},
            })
        end
    end
end

local function publish()
    local doc = {}
    if setup_open then doc.setup = setup_document() end
    if host then
        doc.waiting = {
            mode = MODE, title = TITLE,
            status = string.format("%s  |  %s  |  Host starts", host.track.name, setup_label(host.track, host.laps, host.grid_delay)),
            enrolled = #host.order, capacity = host.capacity, can_start = true,
        }
    end
    local flags = {}
    for id, o in pairs(offers) do
        table.insert(flags, {
            id = id, mode = MODE, leader = o.host, game = o.game, at = v3(o.at), yaw = o.yaw,
            players = #o.racers, capacity = o.cap, open = #o.racers < o.cap,
            stats = {{title = "TRACK", value = o.track.name},
                     {title = "SETUP", value = setup_label(o.track, o.laps, o.grid_delay)},
                     o.access == "crew" and {title = "FOR", value = "Crew [" .. tostring(o.crew) .. "]"}
                     or o.access == "invite" and {title = "FOR", value = "Invited players"}
                     or {title = "GATES", value = tostring(o.track.gates)}},
        })
    end
    challenge_flags(flags)
    if #flags > 0 then doc.flags = flags end
    if race and race.phase ~= "done" then
        doc.match = {markers = true, teleports = true, in_activity = true, input_lock = race.phase == "start" and race.placed}
        -- The game lets a mode teleport (the line-up) on a server without noclip only while its match asks for it
        -- and its HUD is active or it holds the flag; the flag is left at the start, so the HUD (drawn by the
        -- plugin itself, no native slots) is what keeps it allowed.
        doc.hud = {active = true}
        if race.phase == "start" then
            -- The game's own 3-2-1-GO: it shows the last 3 s of this clock and GO as the race starts; before that,
            -- while the racers line up, its intro card names the track.
            local t = now()
            doc.hud.countdown = {remaining_ms = math.max(0, race.go_at - t), total_ms = math.max(1, race.go_at - race.counted_from),
                                 sampled_ms = t, ticking = true}
            doc.hud.intro = {title = string.upper(race.track.name), rules = setup_label(race.track, race.laps, race.grid_delay) ..
                             (race.challenge and "  ·  solo challenge" or ""), until_ms = race.go_at - COUNT_MS}
        end
    end
    local ok, why = pcall(R.throwdowns.ui, doc)
    if not ok then R.log("could not publish throwdown UI: " .. tostring(why)) end
end

-- ---- hosting --------------------------------------------------------------------------------------

local function broadcast_offer()
    if not host then return end
    host.last_offer = now()
    local _, my_name = me()
    local t = host.track
    local offer = {t = "offer", race = host.id, name = my_name, game = host.game, at = host.at, yaw = host.yaw,
                   track = {id = t.id, name = t.name, gates = #t.gates}, laps = host.laps, grid_delay = host.grid_delay,
                   cap = host.capacity, racers = host_racers(), access = host.access, crew = host.crew}
    if host.access == "invite" then
        for id in pairs(host.invited) do send(id, offer) end
    else
        send("all", offer)
    end
end

local function cancel_host(tell)
    if not host then return end
    if tell then send("all", {t = "cancel", race = host.id}) end
    host = nil
    pcall(R.throwdowns.flag.leave)
end

local function leave_flag(tell_host)
    if not joined then return end
    if tell_host then send(joined.leader, {t = "unjoin", race = joined.race}) end
    joined = nil
end

-- ---- the race -------------------------------------------------------------------------------------

local function done(r) return r.ms ~= nil or r.dnf ~= nil end

local function end_race()
    if not race or race.phase == "done" then return end
    release_hold()
    for _, r in pairs(race.racers) do
        if not done(r) then r.dnf = "time" end
    end
    race.phase = "done"
    race.ended_at = now()
    pcall(R.physics.competition, false)
    local mine = race.racers[race.me]
    sound(mine and mine.ms and "go" or "cancel")
end

local function begin_race(message, from_id)
    close_menu()   -- the start sequence is the race's: the menu goes
    leave_flag(false)
    local track = message.track
    local grid_delay = math.clamp(tonumber(message.grid_delay) or GRID_MS, 0, 5000)
    local my_id = me()
    race = {
        id = message.race, host = from_id, me = my_id, track = track, laps = message.laps,
        racers = {}, order = {}, phase = "start", grid_delay = grid_delay, go_at = now() + grid_delay + COUNT_MS, counted_from = now(),
        next = 1, seq = course(track, message.laps), placed = false, tp_until = now() + 5000,
        last_pos = nil, last_at = nil, lap_start = nil, best_lap = nil, missed_at = nil, first_finish = nil,
    }
    local slot = 1
    for i, entry in ipairs(message.racers) do
        race.racers[entry[1]] = {name = entry[2], n = 0, at_ms = 0}
        table.insert(race.order, entry[1])
        if entry[1] == my_id then slot = i end
    end
    pcall(R.physics.prepare)
    pcall(R.physics.competition, true)
    publish()
    local id = race.id
    R.time.after(250, function()
        if not race or race.id ~= id then return end
        local ok, moved, why = pcall(R.skater.teleport, grid_slot(track, slot, #race.order), gate_heading(track.gates[1]))
        race.placed = ok and moved == true
        diag(string.format("teleport: ok=%s returned=%s %s", tostring(ok), tostring(moved), tostring(why or "")))
        race.tp_until = now() + 5000
        -- Start the countdown only after the grid placement, then publish the input lock.
        race.go_at = now() + grid_delay + COUNT_MS
        race.counted_from = now()
        publish()
        if not race.placed then
            -- the game would not move the skater: the server's copy puts them on the grid of the saved track
            if track.id then
                send("server", {t = "grid", map = map_key(), race = id, id = track.id, slot = slot, count = #race.order})
            else
                toast("Skate to the start gate: " .. tostring(why or moved), "warn")
            end
        end
    end)
end

local function start_race()
    local message = {t = "go", race = host.id, laps = host.laps, grid_delay = host.grid_delay,
                     racers = host_racers(), track = pack(host.track)}
    local my_id = me()
    local track = host.track
    host = nil
    pcall(R.throwdowns.flag.leave)
    send("all", message)
    message.track = track
    begin_race(message, my_id)
end

local function check_end()
    if not race or race.phase ~= "running" then return end
    local all = true
    for _, r in pairs(race.racers) do
        if not done(r) then all = false end
    end
    local t = now()
    if all or (race.first_finish and t - race.first_finish > GRACE_MS) or t - race.go_at > MAX_RACE_MS then end_race() end
end

local function finished(id, ms, best)
    local r = race.racers[id]
    if not r or done(r) then return end
    r.ms, r.best, r.n = ms, best, #race.seq
    if not race.first_finish then race.first_finish = now() end
end

local function out(id, why)
    local r = race.racers[id]
    if r and not done(r) then r.dnf = why or "out" end
end

-- What a racer's game tells the others about its run; a challenge is run alone, so nobody.
local function tell_racers(message)
    if not race.challenge then send("all", message) end
end

local function disqualify(why)
    out(race.me, why)
    tell_racers({t = "dnf", race = race.id, why = why})
    toast("Out of the race: " .. why, "warn")
    sound("cancel")
end

local function passed(t)
    local mine = race.racers[race.me]
    local gate = race.seq[race.next]
    -- A challenge's clock starts when the skater crosses the start gate; a race's started for everyone at GO.
    if race.next == 1 and race.challenge then race.started_at = t end
    local elapsed = race.started_at and (t - race.started_at) or 0
    race.next += 1
    mine.n, mine.at_ms = race.next - 1, elapsed
    if race.track.circuit and gate == 1 then
        if race.lap_start then
            local lap = elapsed - race.lap_start
            if not race.best_lap or lap < race.best_lap then race.best_lap = lap end
        end
        race.lap_start = elapsed
    end
    if race.next > #race.seq then
        finished(race.me, elapsed, race.best_lap)
        tell_racers({t = "fin", race = race.id, ms = elapsed, best = race.best_lap})
        if race.challenge then send("server", {t = "challenge_time", map = map_key(), id = race.track.id, ms = elapsed}) end
        sound("go")
    else
        sound("navigate")
        tell_racers({t = "cp", race = race.id, n = mine.n, ms = elapsed})
    end
end

-- The grid's hold let go (at GO, or when the race goes before it).
release_hold = function()
    if race and race.held then
        race.held = false
        pcall(R.skater.hold, false)
    end
end

local function race_tick(t)
    if race.phase == "start" then   -- the game shows the countdown (publish: hud.countdown); the sounds are ours
        -- Held on the grid: input_lock alone did not keep the skater once the game's countdown showed, so the skater
        -- is held too (skater.hold, under the stock physics begin_race turned on), and let go at GO.
        if race.placed and not race.held then
            local ok, held, why = pcall(R.skater.hold, true)
            race.held = ok and held ~= false
            diag("race hold: " .. tostring(ok) .. " " .. tostring(held) .. " " .. tostring(why or ""))
        end
        local left = math.ceil((race.go_at - t) / 1000)
        if left >= 1 and left <= COUNT_MS / 1000 and left ~= race.beeped then
            race.beeped = left
            sound("countdown")
        end
        if t >= race.go_at then
            race.phase = "running"
            if not race.challenge then race.started_at = race.go_at end   -- a race: one clock, from GO
            sound("go")
            release_hold()
        end
        return
    end
    if race.phase == "done" then
        if t - race.ended_at > RESULTS_MS then race = nil end
        return
    end
    local mine = race.racers[race.me]
    if mine and not done(mine) then
        local ok, pos = pcall(R.skater.position)
        if ok and pos then
            if race.last_pos then
                local moved = distance(pos, race.last_pos)
                local dt = math.max((t - race.last_at) / 1000, 1 / 120)
                if t > race.tp_until and moved > 25 and moved / dt > JUMP_SPEED then
                    disqualify("teleported")
                else
                    local through, plane = crossed(race.track.gates[race.seq[race.next]], race.last_pos, pos)
                    if through then passed(t)
                    elseif plane then
                        race.missed_at = t
                        sound("cancel")
                    end
                end
            end
            race.last_pos, race.last_at = pos, t
        end
    end
    check_end()
end

local function leave_race()
    if not race then return end
    release_hold()
    if race.phase ~= "done" then tell_racers({t = "dnf", race = race.id, why = "left"}) end
    pcall(R.physics.competition, false)
    race = nil
end

-- ---- messages -------------------------------------------------------------------------------------

local from_server, from_players = {}, {}

function from_server.staff(m)
    answered, staff = true, m.staff == true
end

function from_server.list(m)
    if m.map ~= map_key() or type(m.ids) ~= "table" then return end
    answered = true
    local kept = {}
    for _, id in ipairs(m.ids) do
        local t = track_by_id(id)
        if t then table.insert(kept, t) end
    end
    tracks, tracks_map = kept, m.map
    for _, id in ipairs(m.ids) do
        if not track_by_id(id) then table.insert(tracks, {id = id, name = "...", gates = {}, loading = true}) end
    end
    if setup_open then publish() end
end

function from_server.track(m)
    if m.map ~= map_key() then return end
    local t = unpack_track(m.track)
    if not t or not t.id then return end
    local _, at = track_by_id(t.id)
    if at then tracks[at] = t else table.insert(tracks, t) end
    if setup_open then publish() end
end

function from_server.saved(m)
    admin_menu = true
    toast("Track \"" .. tostring(m.name) .. "\" saved for everyone.")
end
function from_server.challenge_set(m)
    toast(m.on and ("\"" .. tostring(m.name) .. "\" is a challenge: its start has a marker, F8 there runs it.")
          or ("\"" .. tostring(m.name) .. "\" is no challenge any more."))
end
function from_server.challenge_result(m)
    local text = string.format("%s: %s", tostring(m.name), clock_text(tonumber(m.ms)))
    if m.rank then text = text .. string.format("  -  #%d", m.rank) end
    if not m.improved then text = text .. "  (not your best)" end
    if type(m.best) == "table" then text = text .. string.format("  -  record %s by %s", clock_text(tonumber(m.best.ms)), tostring(m.best.n)) end
    toast(text)
end
function from_server.challenge_record(m)
    if m.map ~= map_key() then return end
    toast(string.format("New record on %s: %s %s!", tostring(m.name), tostring(m.who), clock_text(tonumber(m.ms))))
    sound("go")
end
function from_server.gridded(m)
    if not race or race.id ~= m.race or race.phase ~= "start" then return end
    if m.ok then
        race.placed = true
        race.tp_until = now() + 5000
        publish()
    else
        toast("Skate to the start gate: " .. tostring(m.why), "warn")
    end
end
function from_server.deleted(m) toast("Track \"" .. tostring(m.name) .. "\" deleted.") end
function from_server.refused(m) toast(tostring(m.why), "warn") end

function from_players.offer(from, m)
    if from.id == me() or type(m.at) ~= "table" or type(m.racers) ~= "table" or type(m.track) ~= "table" then return end
    if m.access == "crew" and (not my_team() or my_team() ~= m.crew) then return end   -- another crew's race
    -- The track itself comes with "go"; the flag names it, and the server's copy is shown meanwhile.
    local track = {id = tostring(m.track.id), name = tostring(m.track.name):sub(1, 32), gates = tonumber(m.track.gates) or 0}
    offers[m.race] = {
        host = from.id, name = m.name or from.name, game = tonumber(m.game) or 1, at = m.at, yaw = tonumber(m.yaw) or 0,
        track = track, laps = tonumber(m.laps) or 1, grid_delay = tonumber(m.grid_delay) or GRID_MS,
        cap = tonumber(m.cap) or 10, racers = m.racers, until_ms = now() + OFFER_TTL_MS,
        access = m.access == "crew" and "crew" or (m.access == "invite" and "invite" or "all"), crew = m.crew,
    }
    if joined and joined.race == m.race then joined.track = track_by_id(track.id) end
end

function from_players.cancel(from, m)
    local o = offers[m.race]
    if o and o.host == from.id then offers[m.race] = nil end
    if joined and joined.race == m.race then
        joined = nil
        toast("The host called the race off.", "warn")
    end
end

function from_players.join(from, m)
    if not host or m.race ~= host.id then
        send(from.id, {t = "joined", race = m.race, ok = false, why = "That race is not open any more."})
        return
    end
    if not host.racers[from.id] then
        if host.access == "invite" and not host.invited[from.id] then
            send(from.id, {t = "joined", race = m.race, ok = false, why = "That race is invite-only."})
            return
        elseif host.access == "crew" and m.crew ~= host.crew then
            send(from.id, {t = "joined", race = m.race, ok = false, why = "That race is for crew [" .. tostring(host.crew) .. "] only."})
            return
        end
        if #host.order >= host.capacity then
            send(from.id, {t = "joined", race = m.race, ok = false, why = "That race is full."})
            return
        end
        host.racers[from.id] = from.name
        table.insert(host.order, from.id)
        sound("select")
    end
    send(from.id, {t = "joined", race = m.race, ok = true})
    host.last_offer = math.min(host.last_offer, now() - OFFER_EVERY_MS + 500)
end

function from_players.unjoin(from, m)
    if not host or m.race ~= host.id or not host.racers[from.id] then return end
    host.racers[from.id] = nil
    remove_value(host.order, from.id)
    host.last_offer = math.min(host.last_offer, now() - OFFER_EVERY_MS + 500)
end

function from_players.joined(from, m)
    if not joined or joined.race ~= m.race or joined.leader ~= from.id then return end
    if m.ok then
        toast("You are in " .. tostring(joined.name) .. "'s race. F10 leaves it.")
    else
        toast(tostring(m.why or "Could not join."), "warn")
        joined = nil
    end
end

function from_players.go(from, m)
    local track = unpack_track(m.track)
    if not track or type(m.racers) ~= "table" then return end
    m.track = track
    m.laps = laps_of(track, tonumber(m.laps) or 1)
    offers[m.race] = nil
    local my_id = me()
    for _, entry in ipairs(m.racers) do
        if entry[1] == my_id and not race then begin_race(m, from.id) end
    end
    if joined and joined.race == m.race then joined = nil end
end

function from_players.cp(from, m)
    if not race or m.race ~= race.id then return end
    local r = race.racers[from.id]
    if r and not done(r) and tonumber(m.n) then r.n, r.at_ms = tonumber(m.n), tonumber(m.ms) or 0 end
end

function from_players.fin(from, m)
    if race and m.race == race.id and tonumber(m.ms) then finished(from.id, tonumber(m.ms), tonumber(m.best)) end
end

function from_players.dnf(from, m)
    if race and m.race == race.id then out(from.id, tostring(m.why or "out")) end
end

R.on("message", function(from, payload)
    local ok, m = pcall(R.json.decode, payload)
    if not ok or type(m) ~= "table" or type(m.t) ~= "string" then return end
    if from.server then
        local handler = from_server[m.t]
        if handler then handler(m) end
    elseif type(m.race) == "string" then
        local handler = from_players[m.t]
        if handler then handler(from, m) end
    end
end)

R.on("player_left", function(p)
    for id, o in pairs(offers) do
        if o.host == p.id then offers[id] = nil end
    end
    if joined and joined.leader == p.id then
        joined = nil
        toast("The race's host left.", "warn")
    end
    if host and host.racers[p.id] and p.id ~= host.order[1] then
        host.racers[p.id] = nil
        remove_value(host.order, p.id)
    end
    if race then out(p.id, "left") end
end)

-- ---- the track editor (staff) ---------------------------------------------------------------------

local MODES = {
    {name = "MOVE", help = "3/2 forward/back, 4/5 left/right"},
    {name = "HEIGHT & TURN", help = "3/2 up/down, 4/5 turn"},
    {name = "SIZE", help = "3/2 taller/lower, 4/5 narrower/wider"},
}

local function open_dialog(id, spec)
    local ok, opened, why = pcall(R.ui.dialog, id, spec)
    if not ok or not opened then toast("Could not open the dialog: " .. tostring(why or opened), "error") end
end

-- Which way the skater is going: the last half second of movement, else the heading of the
-- track so far, else north.
local function facing(pos)
    local t, oldest = now(), nil
    for _, s in ipairs(trail) do
        if t - s.at <= 800 then oldest = s end
    end
    if oldest then
        local dx, dz = pos.x - oldest.pos.x, pos.z - oldest.pos.z
        local len = math.sqrt(dx * dx + dz * dz)
        if len > 1 then return {dx / len, dz / len} end
    end
    local gates = editor and editor.track.gates or {}
    local last = gates[editor and editor.sel or 0]
    if last then
        local dx, dz = pos.x - last.p[1], pos.z - last.p[3]
        local len = math.sqrt(dx * dx + dz * dz)
        if len > 1 then return {dx / len, dz / len} end
        return {last.n[1], last.n[2]}
    end
    return {0, 1}
end

-- The ground under a point: a gate placed mid-air (an ollie, a drop) stands on it, not where the skater was.
-- A hit above the point is the skater's own body or a roof, and is not taken; no ground within 30 m keeps y.
local function ground(x, y, z)
    local ok, hit = pcall(R.world.raycast, vector.create(x, y + 0.5, z), vector.create(x, y - 30, z))
    if ok and hit and hit.y <= y + 0.05 then return hit.y end
    return y
end

local GATE_PLACE_AHEAD = 3

local function gate_here()
    local ok, s = pcall(R.skater.get)
    if not ok or not s then return nil end
    local p = s.position
    local n = facing(p)
    local x, z = p.x + n[1] * GATE_PLACE_AHEAD, p.z + n[2] * GATE_PLACE_AHEAD
    return clean_gate({p = {x, ground(x, p.y, z), z}, n = n, w = 8, h = 5, heading = s.heading})
end

local function list_dialog()
    admin_menu = true
    admin_sel = math.clamp(admin_sel, 1, math.max(#tracks, 1))
end

local function admin_track()
    admin_sel = math.clamp(admin_sel, 1, math.max(#tracks, 1))
    local track = tracks[admin_sel]
    if track and not track.loading then return track end
end

local function save_dialog()
    local t = editor.track
    open_dialog("save_track", {
        title = "COJA | SAVE TRACK",
        message = string.format("%d gates, %s. This track will be shared on the server.", #t.gates, t.circuit and "a circuit" or "a sprint"),
        field = {text = t.name ~= "" and t.name or ("Track " .. (#tracks + 1)), max = 32},
        buttons = {{id = "save", label = "Save"}, {id = "discard", label = "Discard"}, {id = "cancel", label = "Keep editing"}},
    })
end

-- ---- challenges: a track's start with a marker, run alone and timed (F8 there) -----------------------

local CHALLENGE_REACH = 10      -- metres from the start gate where F8 runs it
local CHALLENGE_SHOWN = 150     -- metres within which its marker shows

-- The challenge whose start is nearest, within `reach` metres: the track and how far.
local function challenge_near(reach)
    local ok, pos = pcall(R.skater.position)
    if not ok or not pos then return nil end
    local best, far = nil, reach
    for _, t in ipairs(tracks) do
        if t.challenge and not t.loading and t.gates[1] then
            local d = distance(pos, v3(t.gates[1].p))
            if d <= far then best, far = t, d end
        end
    end
    return best, far
end

local function start_challenge(t)
    if race or host or joined then return toast("Leave your race first (F10).", "warn") end
    if editor then return toast("Close Track Studio first (F7).", "warn") end
    local my_id, my_name = me()
    begin_race({race = "challenge:" .. my_id .. ":" .. math.floor(now()), laps = 1, racers = {{my_id, my_name}}, track = t}, my_id)
    race.challenge = true
    toast("Challenge: " .. t.name .. (t.best and ("  -  record " .. clock_text(t.best.ms) .. " by " .. t.best.n) or "") .. ". F10 stops.")
end

local function challenge_key()
    if race or host or joined then return end
    local t = challenge_near(CHALLENGE_REACH)
    if t then start_challenge(t) end
end

local function edit_key(name)
    if name == "race_editor" then
        if race or host or joined then return toast("Not during a race.", "warn") end
        if not staff then return toast(answered and "Only admins make race tracks." or "The server has not answered yet.", "warn") end
        if admin_menu then admin_menu = false
        elseif not editor then list_dialog()
        elseif editor.changed then save_dialog()
        else
            editor = nil
            admin_menu = true
            toast("Track editor closed.")
        end
        return
    end
    if admin_menu then
        if name == "gate_prev" or name == "gate_next" then
            if #tracks > 0 then
                local step = name == "gate_prev" and -1 or 1
                admin_sel = ((admin_sel - 1 + step) % #tracks) + 1
                sound("navigate")
            end
        elseif name == "gate_add" then
            admin_menu = false
            editor = {track = {name = "", circuit = false, gates = {}}, sel = 0, mode = 1, changed = false}
            toast("New track: skate to the start, F8 places a gate where you are, facing the way you skate.")
        elseif name == "gate_here" then
            local t = admin_track()
            if t then
                admin_menu = false
                editor = {track = copy(t), sel = 1, mode = 1, changed = false}
                toast("Editing \"" .. t.name .. "\". PgUp/PgDn pick a gate.")
            end
        elseif name == "gate_circuit" then
            local t = admin_track()
            if t then send("server", {t = "challenge", map = map_key(), id = t.id}) end
        elseif name == "gate_delete" then
            local t = admin_track()
            if t then
                open_dialog("delete_track", {
                    title = "COJA | DELETE TRACK", message = "Delete \"" .. t.name .. "\" for everyone?",
                    buttons = {{id = "delete:" .. tostring(t.id), label = "Delete track"}, {id = "cancel", label = "Keep track"}},
                })
            end
        end
        return
    end
    if not editor then
        if name == "gate_add" then challenge_key() end
        return
    end
    local gates, sel = editor.track.gates, editor.sel
    local g = gates[sel]
    if name == "gate_add" then
        if #gates >= MAX_GATES then return toast("A track has at most " .. MAX_GATES .. " gates.", "warn") end
        local new = gate_here()
        if not new then return end
        table.insert(gates, sel + 1, new)
        editor.sel = sel + 1
        sound("select")
    elseif name == "gate_delete" and g then
        table.remove(gates, sel)
        editor.sel = math.clamp(sel, #gates > 0 and 1 or 0, #gates)
        sound("cancel")
    elseif name == "gate_prev" and #gates > 0 then
        editor.sel = sel <= 1 and #gates or sel - 1
        sound("navigate")
        return
    elseif name == "gate_next" and #gates > 0 then
        editor.sel = sel >= #gates and 1 or sel + 1
        sound("navigate")
        return
    elseif name == "gate_here" and g then
        local new = gate_here()
        if not new then return end
        new.w, new.h = g.w, g.h
        gates[sel] = new
    elseif name == "adjust_mode" then
        editor.mode = editor.mode % #MODES + 1
        sound("tab")
        return
    elseif name == "gate_circuit" then
        editor.track.circuit = not editor.track.circuit
        toast(editor.track.circuit and "Circuit: gate 1 is start and finish." or "Sprint: the last gate is the finish.")
    elseif g and (name == "adjust_up" or name == "adjust_down" or name == "adjust_left" or name == "adjust_right") then
        local sign = (name == "adjust_up" or name == "adjust_right") and 1 or -1
        local vertical = name == "adjust_up" or name == "adjust_down"
        local mode = MODES[editor.mode].name
        if mode == "MOVE" then
            local step = (vertical and normal(g) or across(g)) * (0.5 * sign)
            g.p = {g.p[1] + step.x, g.p[2], g.p[3] + step.z}
        elseif mode == "HEIGHT & TURN" then
            if vertical then
                g.p[2] += 0.25 * sign
            else
                local a = math.rad(15) * sign
                local nx, nz = g.n[1], g.n[2]
                g.n = {nx * math.cos(a) + nz * math.sin(a), -nx * math.sin(a) + nz * math.cos(a)}
            end
        elseif vertical then
            g.h += 0.5 * sign
        else
            g.w += 0.5 * sign
        end
        gates[sel] = clean_gate(g)
    else
        return
    end
    editor.changed = true
end

local function on_dialog(args)
    local id, button, text, row = args[1], args[2], args[3], tonumber(args[4])
    pcall(R.ui.close_dialog, id)
    if id == "tracks" then
        local t = row and tracks[row]
        if button == "new" then
            admin_menu = false
            editor = {track = {name = "", circuit = false, gates = {}}, sel = 0, mode = 1, changed = false}
            toast("New track: skate to the start, F8 places a gate where you are, facing the way you skate.")
        elseif button == "edit" and t and not t.loading then
            admin_menu = false
            editor = {track = copy(t), sel = 1, mode = 1, changed = false}
            toast("Editing \"" .. t.name .. "\". PgUp/PgDn pick a gate.")
        elseif button == "challenge" and t and not t.loading then
            send("server", {t = "challenge", map = map_key(), id = t.id})
        elseif button == "delete" and t then
            open_dialog("delete_track", {
                title = "COJA | DELETE TRACK", message = "Delete \"" .. t.name .. "\" for everyone?",
                buttons = {{id = "delete:" .. tostring(t.id), label = "Delete track"}, {id = "cancel", label = "Keep track"}},
            })
        end
    elseif id == "delete_track" and type(button) == "string" and button:sub(1, 7) == "delete:" then
        send("server", {t = "delete", map = map_key(), id = button:sub(8)})
    elseif id == "save_track" and editor then
        if button == "save" then
            local t = editor.track
            t.name = trim(text):sub(1, 32)
            if t.name == "" then t.name = "Track " .. (#tracks + 1) end
            if not clean_track(t) then
                return toast(t.circuit and "A circuit needs 3 gates or more." or "A track needs 2 gates or more.", "warn")
            end
            send("server", {t = "save", map = map_key(), track = pack(t)})
            editor = nil
        elseif button == "discard" then
            editor = nil
            admin_menu = true
            toast("Changes discarded.")
        end
    end
end

-- ---- the throwdown kit's events -------------------------------------------------------------------

R.on("throwdown_ui", function(e)
    local a, args = e.action, e.args or {}
    if a == "dialog" then
        on_dialog(args)
    elseif a == "open" then
        if admin_menu then admin_menu = false end
        if race or joined or editor then
            pcall(R.throwdowns.flag.leave)
            toast(editor and "Close Track Studio first (F7)." or "Leave your race first (F10).", "warn")
            return
        end
        if not answered then hello() end
        setup_open = true
    elseif a == "setup_row" then
        diag("setup_row " .. json_of(args))
        local value = tonumber(args[2]) or 0
        if args[1] == "track" then
            -- the choice's label, when the game sends one, names the track; else its index
            local named = nil
            for i = 2, #args do
                for at, one in ipairs(tracks) do
                    if type(args[i]) == "string" and args[i] == one.name then named = at end
                end
            end
            setup.track = named or math.clamp(value + 1, 1, math.max(#tracks, 1))
            local selected = tracks[setup.track]
            if selected and not selected.loading then toast("Selected track: " .. selected.name) end
        elseif args[1] == "laps" then setup.laps = LAPS[value + 1] or 1
        elseif args[1] == "access" then setup.access = math.clamp(value + 1, 1, #ACCESS)
        elseif args[1] == "grid" then
            setup.grid_choice = math.clamp(value + 1, 1, #GRID_DELAYS)
            setup.grid_delay = GRID_DELAYS[setup.grid_choice]
        elseif args[1] == "players" then setup.players = math.clamp(value, 1, 10) end
    elseif a == "setup_button" and args[1] == "refresh_tracks" then
        answered = false
        hello()
    elseif a == "setup_back" then
        setup_open = false
        pcall(R.throwdowns.flag.leave)
    elseif a == "setup_confirm" then
        diag("setup_confirm " .. json_of(args))
        setup_open = false
        setup.players = math.clamp(tonumber(args[1]) or setup.players, 1, 10)
        local t = tracks[setup.track]
        if not t or t.loading then
            pcall(R.throwdowns.flag.leave)
            toast("No track is available for this map. Refresh the list or ask staff to save a track with F7.", "warn")
        end
    elseif a == "flag_placed" then
        local t = tracks[math.clamp(setup.track, 1, math.max(#tracks, 1))]
        if not t or t.loading then return pcall(R.throwdowns.flag.leave) end
        local my_id, my_name = me()
        host = {
            id = my_id .. ":" .. math.floor(now()), game = tonumber(args[1]) or 1,
            at = {round(tonumber(args[2]) or 0, 0.1), round(tonumber(args[3]) or 0, 0.1), round(tonumber(args[4]) or 0, 0.1)},
            yaw = tonumber(args[5]) or 0, track = t, laps = laps_of(t, setup.laps), grid_delay = setup.grid_delay,
            capacity = setup.players, access = ACCESS[setup.access] or "all", crew = my_team(), invited = {},
            racers = {[my_id] = my_name}, order = {my_id}, last_offer = 0,
        }
        for id, on in pairs(setup.invited) do
            if on then host.invited[id] = true end
        end
        local who = ""
        if host.access == "crew" and not host.crew then
            host.access = "all"
            toast("You are in no crew: this race is open to everyone.", "warn")
        elseif host.access == "crew" then
            who = "  -  crew [" .. host.crew .. "] only"
        elseif host.access == "invite" then
            local n = 0
            for _ in pairs(host.invited) do
                n += 1
            end
            who = n > 0 and string.format("  -  %d invited", n) or "  -  nobody invited yet (F5 > RACES)"
        end
        toast("Race on " .. t.name .. "  -  " .. setup_label(t, host.laps, host.grid_delay) .. who)
        broadcast_offer()
    elseif a == "start" then
        if host then start_race() end
    elseif a == "leave" or a == "flag_left" then
        cancel_host(true)
    elseif a == "quit" then
        leave_race()
    elseif a == "join" then
        local id, leader = args[1], args[2]
        local o = offers[id]
        if type(id) == "string" and id:sub(1, 10) == "challenge:" then   -- a challenge's flag on the map
            local t = track_by_id(id:sub(11))
            pcall(R.throwdowns.flag.leave)
            if t and t.challenge then start_challenge(t) end
        elseif race or host then
            toast("You are in a race already.", "warn")
        elseif o then
            leave_flag(true)
            joined = {race = id, leader = leader, name = o.name, track = track_by_id(o.track.id)}
            send(leader, {t = "join", race = id, crew = my_team()})
        end
    end
    publish()
end)

local function leave_key()
    if race then
        leave_race()
    elseif joined then
        leave_flag(true)
        toast("You left the race.")
    elseif host then
        cancel_host(true)
    elseif editor then
        if editor.changed then save_dialog() else editor = nil end
    elseif admin_menu then
        admin_menu = false
        toast("Track Studio closed.")
    end
end

-- ---- the COJA menu (F5, hud/menu.lua): the races' tabs, used with the mouse --------------------------------

local function new_track()
    admin_menu = false
    editor = {track = {name = "", circuit = false, gates = {}}, sel = 0, mode = 1, changed = false}
    toast("New track: skate to the start; Place a gate puts one 3 m ahead, facing the way you skate.")
end

local function edit_track(t)
    admin_menu = false
    editor = {track = copy(t), sel = 1, mode = 1, changed = false}
    toast("Editing \"" .. t.name .. "\".")
end

local function ask_delete(t)
    open_dialog("delete_track", {
        title = "COJA | DELETE TRACK", message = "Delete \"" .. t.name .. "\" for everyone?",
        buttons = {{id = "delete:" .. tostring(t.id), label = "Delete track"}, {id = "cancel", label = "Keep track"}},
    })
end

-- The race flag from the menu: the game's own placing, as Throwdowns does; flag_placed then hosts it.
local function place_flag()
    local ok, why = pcall(R.throwdowns.flag.begin, MODE, 1, {setup_page = false})
    if not ok then ok, why = pcall(R.throwdowns.flag.begin, MODE) end
    diag("flag.begin: " .. (ok and ("ok " .. tostring(why)) or tostring(why)))
    if ok and why ~= false then
        close_menu()   -- placing the flag is skating to the spot
        return toast("Place the race flag.")
    end
    toast("Open Throwdowns > Cocojambo Deathraces to place the flag: it starts with these choices.", "warn")
end

local function names_of(list)
    local out = {}
    for i, t in ipairs(list) do out[i] = t.name end
    return out
end

local function race_tab(ui)
    if race then
        ui:card(race.challenge and "SOLO CHALLENGE" or "IN A RACE", race.track.name)
        if ui:button("race_quit", race.phase == "done" and "Close the results" or "Leave the race") then leave_key() end
        return
    end
    if host then
        ui:card("YOUR RACE", host.track.name .. "  ·  " .. setup_label(host.track, host.laps, host.grid_delay))
        ui:info("Racers", string.format("%d / %d", #host.order, host.capacity))
        if ui:button("race_start", "Start the race") then
            start_race()
            publish()
        end
        if ui:button("race_cancel", "Call it off") then cancel_host(true) end
        return
    end
    if joined then
        ui:card("WAITING FOR THE START", "In " .. tostring(joined.name) .. "'s race")
        if ui:button("race_unjoin", "Leave the race") then
            leave_flag(true)
            toast("You left the race.")
        end
        return
    end
    ui:card("COCOJAMBO DEATHRACES", "Choose the race, then place its flag; others join it on the map")
    local ready = {}
    for _, t in ipairs(tracks) do
        if not t.loading then ready[#ready + 1] = t end
    end
    if #ready == 0 then
        ui:note(answered and "No tracks saved for this map yet: staff make them in TRACK STUDIO." or "Waiting for the server's tracks...")
        if ui:button("race_refresh", "Refresh tracks") then
            answered = false
            hello()
        end
        return
    end
    local chosen = 1
    for i, t in ipairs(ready) do
        if t == tracks[setup.track] then chosen = i end
    end
    local t = ready[ui:dropdown("race_track", "Track", names_of(ready), chosen)] or ready[1]
    for i, one in ipairs(tracks) do
        if one == t then setup.track = i end
    end
    ui:info("Gates", string.format("%d  (%s)%s", #t.gates, t.circuit and "circuit" or "sprint",
        t.author ~= "" and ("  ·  made by " .. t.author) or ""))
    if t.circuit then
        local laps = {}
        for i, n in ipairs(LAPS) do laps[i] = n == 1 and "1 lap" or (n .. " laps") end
        setup.laps = LAPS[ui:dropdown("race_laps", "Laps", laps, lap_index())] or 1
    end
    setup.grid_choice = math.clamp(ui:dropdown("race_grid", "Grid time", {"Quick (0 s)", "Standard (2.5 s)", "Relaxed (5 s)"},
        setup.grid_choice or 2), 1, #GRID_DELAYS)
    setup.grid_delay = GRID_DELAYS[setup.grid_choice]
    setup.players = math.clamp(math.floor(ui:slider("race_players", "Racers", setup.players, 1, 10, "%d") + 0.5), 1, 10)
    setup.access = math.clamp(ui:dropdown("race_access", "Who can join", ACCESS_NAMES, setup.access), 1, #ACCESS)
    if ACCESS[setup.access] == "crew" then
        ui:note(my_team() and ("Only crew [" .. my_team() .. "] sees and joins it.") or "You are in no crew: it would be open to everyone.")
    elseif ACCESS[setup.access] == "invite" then
        local players = online_players()
        if #players == 0 then ui:note("Nobody else is on the server to invite.") end
        for _, p in ipairs(players) do
            local on = ui:toggle("invite_" .. p.id, "Invite " .. p.name, setup.invited[p.id] == true)
            setup.invited[p.id] = on or nil
        end
    end
    if ui:button("race_flag", "Place the race flag") then place_flag() end
    ui:note("Throwdowns > Cocojambo Deathraces opens with the same choices.")
end

local function challenges_tab(ui)
    ui:card("CHALLENGES", "A track alone against the clock: the server keeps everyone's best, top 10")
    local ok, pos = pcall(R.skater.position)
    local n = 0
    for i, t in ipairs(tracks) do
        if t.challenge and not t.loading and t.gates[1] then
            n += 1
            local far = ok and pos and string.format("  ·  %d m away", math.floor(distance(pos, v3(t.gates[1].p)))) or ""
            ui:info(t.name, (t.best and ("record " .. clock_text(t.best.ms) .. " by " .. t.best.n) or "no record yet") .. far)
            if ui:button("challenge_" .. i, "Run " .. t.name) then start_challenge(t) end
        end
    end
    if n == 0 then ui:note("No challenges on this map yet.") end
    if race and race.challenge and ui:button("challenge_stop", "Stop the run") then leave_key() end
end

-- A gate change from a button: the edit mode it belongs to, then the editor's own step.
local function adjust(mode, action)
    editor.mode = mode
    edit_key(action)
end

local function studio_tab(ui)
    if not editor then
        ui:card("TRACK STUDIO", "The server's tracks on " .. map_key() .. ": every change is shared")
        if #tracks > 0 then
            admin_sel = ui:dropdown("studio_track", "Track", names_of(tracks), math.clamp(admin_sel, 1, #tracks))
            local t = admin_track()
            if t then
                ui:info("Gates", string.format("%d  ·  %s  ·  %s", #t.gates, t.circuit and "circuit" or "sprint",
                    t.challenge and "a challenge" or "no challenge"))
                if ui:button("studio_edit", "Edit this track") then edit_track(t) end
                if ui:button("studio_challenge", t.challenge and "Stop it being a challenge" or "Make it a challenge") then
                    send("server", {t = "challenge", map = map_key(), id = t.id})
                end
                if ui:button("studio_delete", "Delete this track") then ask_delete(t) end
            end
        else
            ui:note(answered and "No tracks on this map yet." or "Waiting for the server's tracks...")
        end
        if ui:button("studio_new", "New track") then new_track() end
        return
    end
    local t = editor.track
    ui:card(t.name ~= "" and t.name or "NEW TRACK", string.format("%s  ·  gate %d of %d", t.circuit and "circuit" or "sprint", editor.sel, #t.gates))
    if ui:button("gate_add", "Place a gate 3 m ahead of me") then edit_key("gate_add") end
    if editor.sel > 0 then
        if ui:button("gate_here", "Move this gate 3 m ahead of me") then edit_key("gate_here") end
        if ui:button("gate_prev", "Previous gate") then edit_key("gate_prev") end
        if ui:button("gate_next", "Next gate") then edit_key("gate_next") end
        ui:separator()
        if ui:button("move_fwd", "Forward 0.5 m") then adjust(1, "adjust_up") end
        if ui:button("move_back", "Back 0.5 m") then adjust(1, "adjust_down") end
        if ui:button("move_left", "Left 0.5 m") then adjust(1, "adjust_left") end
        if ui:button("move_right", "Right 0.5 m") then adjust(1, "adjust_right") end
        if ui:button("raise", "Up 0.25 m") then adjust(2, "adjust_up") end
        if ui:button("lower", "Down 0.25 m") then adjust(2, "adjust_down") end
        if ui:button("turn_left", "Turn 15° left") then adjust(2, "adjust_left") end
        if ui:button("turn_right", "Turn 15° right") then adjust(2, "adjust_right") end
        if ui:button("taller", "Taller") then adjust(3, "adjust_up") end
        if ui:button("shorter", "Lower") then adjust(3, "adjust_down") end
        if ui:button("wider", "Wider") then adjust(3, "adjust_right") end
        if ui:button("narrower", "Narrower") then adjust(3, "adjust_left") end
        ui:separator()
        if ui:button("gate_delete", "Delete this gate") then edit_key("gate_delete") end
    end
    local circuit = ui:toggle("studio_circuit", "Circuit: gate 1 is start and finish of every lap", t.circuit)
    if circuit ~= t.circuit then edit_key("gate_circuit") end
    if ui:button("studio_save", "Save the track") then save_dialog() end
    if ui:button("studio_close", editor.changed and "Discard changes" or "Close the track") then
        editor = nil
        toast("Track editor closed.")
    end
end

local menu = require("hud/menu")
close_menu = menu.close
menu.busy = function() return race ~= nil end
menu.on_report(function(text) diag("F5 menu: " .. text) end)
local hub = require("hud/hub")
local home = require("hud/home")
local settings = require("hud/settings")
hub.on_report(function(text) diag("F5 menu: " .. text) end)

-- HOME: the player's profile and stats, this server's and the hub's.
menu.tab("HOME", function(ui)
    if not (hud and hud.on_server()) then return ui:note("Your profile shows once the server has answered.") end
    if ui.custom then return ui:custom(function(d, x, y, w) return home.draw(d, x, y, w, hud, hub) end) end
    home.lines(ui, hud, hub)
end)

-- RACES: hosting a race, then the challenges.
menu.tab("RACES", function(ui)
    race_tab(ui)
    ui:separator()
    challenges_tab(ui)
end)

-- ADMIN (staff): Track Studio, and a search of the game's blueprints for ones that look like challenge markers (for
-- showing events with skate.'s own marker), told to the server's log. The game has thousands: they are read a
-- few hundred a tick, since a plugin that takes over 50 ms in one call, or 2 ms a tick for 3 s, is stopped.
local scan = nil   -- {names, at, found}
local function start_scan()
    if scan then return end
    local ok, names = pcall(function() return R.entities.blueprints() end)
    if not ok or type(names) ~= "table" then return diag("blueprints: " .. tostring(names)) end
    scan = {names = names, at = 1, found = {}}
    toast("Looking through " .. #names .. " blueprints...")
end
local function scan_tick()
    if not scan then return end
    local names, found = scan.names, scan.found
    local last = math.min(#names, scan.at + 299)
    for i = scan.at, last do
        local low = string.lower(tostring(names[i]))
        if low:find("challenge", 1, true) or low:find("marker", 1, true) or low:find("checkpoint", 1, true) then
            found[#found + 1] = tostring(names[i])
        end
    end
    scan.at = last + 1
    if scan.at <= #names then return end
    diag(string.format("blueprints: %d loaded, %d look like markers", #names, #found))
    local line = ""
    for i = 1, math.min(#found, 120) do
        if #line + #found[i] + 2 > 280 then
            diag("blueprints: " .. line)
            line = ""
        end
        line = line == "" and found[i] or (line .. ", " .. found[i])
    end
    if line ~= "" then diag("blueprints: " .. line) end
    toast(#found .. " marker blueprints told to the server's log.")
    scan = nil
end
menu.tab("ADMIN", function(ui)
    studio_tab(ui)
    if not editor then
        ui:separator()
        ui:card("EVENT MARKERS", "Which of the game's objects could mark our events")
        if ui:button("find_markers", scan and "Looking..." or "Log the game's marker blueprints") then start_scan() end
    end
end, function() return staff end)

-- SETTINGS: the HUD's settings (as on Insert > PLUGINS > COJA tools) and the controls.
local SIZES, SIZE_NAMES = {0.8, 1, 1.2, 1.4}, {"Small", "Normal", "Large", "Huge"}
menu.tab("SETTINGS", function(ui)
    ui:card("HUD", "Your points chip, notes and the F4 panel")
    local changed = false
    local corner = ui:toggle("set_corner", "Points chip (points, rank, time played)", settings.corner)
    if corner ~= settings.corner then settings.corner, changed = corner, true end
    local side = ui:dropdown("set_side", "Chip on the", {"Left", "Right"}, settings.side == "right" and 2 or 1)
    if (side == 2 and "right" or "left") ~= settings.side then settings.side, changed = side == 2 and "right" or "left", true end
    local toasts = ui:toggle("set_toasts", "Notes from the server at the top of the screen", settings.toasts)
    if toasts ~= settings.toasts then settings.toasts, changed = toasts, true end
    local size = 2
    for i, v in ipairs(SIZES) do
        if math.abs(v - settings.scale) < 0.05 then size = i end
    end
    local picked = ui:dropdown("set_size", "HUD size", SIZE_NAMES, size)
    if picked ~= size then settings.scale, changed = SIZES[picked], true end
    if ui:button("set_reset", "Back to the defaults") then
        for k, v in pairs(settings.DEFAULTS) do settings[k] = v end
        changed = true
    end
    if changed then settings.save() end
    ui:separator()
    ui:card("CONTROLS", "Keyboard, controller and mouse")
    ui:info("F5", "This menu (Esc or B closes it)")
    ui:info("Arrows · D-pad", "Move; left/right on the tabs switch them")
    ui:info("Enter · Space · A", "Press")
    ui:info("Tab · LB / RB", "Next / previous tab")
    ui:info("F4", "Your stats card")
    ui:info("F8", "Run the challenge you stand at")
    ui:info("F10", "Leave a race, close the results")
end)

R.on("action", function(name)
    if menu.open and name ~= "coja_menu" then return end   -- the menu has the keys and buttons
    if name == "race_leave" then leave_key()
    elseif name == "coja_menu" then menu.toggle()
    else edit_key(name) end
end)

-- ---- drawing --------------------------------------------------------------------------------------

-- A card as on the website: a near-black surface with 8 px corners and a thin light border.
local function panel(d, x, y, w, h, colour)
    local k = d:size().y / 1080
    local tl, br = vector.create(x, y, 0), vector.create(x + w, y + h, 0)
    d:rect(tl, br, {colour = colour or theme.a(theme.surface, 0.9), fill = true, rounding = theme.radius * k})
    d:rect(tl, br, {colour = theme.line_strong, fill = false, thickness = 1, rounding = theme.radius * k})
end

local function text(d, x, y, words, style)
    d:text(vector.create(x, y, 0), words, style)
end

-- A gate as a glowing frame with an arrow through it.
local function draw_gate(d, g, label, colours, arrow)
    local p, u, n = v3(g.p), across(g), normal(g)
    local up = vector.create(0, g.h, 0)
    local bl, br = p - u * (g.w / 2), p + u * (g.w / 2)
    local tl, tr = bl + up, br + up
    for layer = 1, 4 do
        local colour = colours[layer]
        if colour then
            local style = {colour = colour, thickness = THICK[layer]}
            -- two posts and the top bar: no line along the ground, since world lines are drawn over everything
            -- (no depth), so one there showed through the floor from every side
            d:world_line(bl, tl, style)
            d:world_line(tl, tr, style)
            d:world_line(tr, br, style)
        end
    end
    if arrow then
        local c = p + vector.create(0, g.h / 2, 0)
        local style = {colour = colours[4], thickness = 4}
        d:world_line(c - n * 0.6 + u * 0.9, c + n * 0.6, style)
        d:world_line(c - n * 0.6 - u * 0.9, c + n * 0.6, style)
    end
    if label then
        d:world_text(p + up + vector.create(0, 0.8, 0), label, {colour = colours[4], size = 26, align = "centre", shadow = true, font = "bold"})
    end
end

local function gate_label(track, i)
    if i == 1 then return track.circuit and "START / FINISH" or "START" end
    if i == #track.gates and not track.circuit then return "FINISH" end
    return tostring(i)
end

local function draw_track(d, track, dim)
    for i, g in ipairs(track.gates) do
        draw_gate(d, g, (i == 1 or i == #track.gates) and gate_label(track, i) or nil, dim and NEON_DIM or NEON, false)
    end
end

local function draw_admin_menu(d, w, h, k)
    local x, y, bw, bh = w / 2 - 460 * k, h * 0.14, 920 * k, 620 * k
    local selected = admin_track()
    if selected then draw_track(d, selected, true) end
    panel(d, x, y, bw, bh)
    d:image("images/logo.png", vector.create(x + 24 * k, y + 22 * k, 0),
        vector.create(x + 24 * k + 144 * k, y + 42 * k, 0), {colour = theme.a("#ffffff", 0.92)})
    text(d, x + bw - 24 * k, y + 20 * k, "TRACK STUDIO", {colour = theme.sky, size = 20, align = "right", font = "bold"})
    text(d, x + bw - 24 * k, y + 48 * k, "SERVER TRACKS  ·  " .. tostring(map_key()),
        {colour = theme.muted, size = 15, align = "right", font = "bold"})
    local total = #tracks
    text(d, x + 26 * k, y + 84 * k, total == 0 and "No tracks on this map yet" or string.format("TRACK LIBRARY  ·  %d / %d", admin_sel, total),
        {colour = theme.ink, size = 20, font = "bold"})
    local first = math.max(1, math.min(admin_sel - 2, total - 5))
    for i = first, math.min(total, first + 5) do
        local t, ry = tracks[i], y + (112 + (i - first) * 60) * k
        if i == admin_sel then
            d:rect(vector.create(x + 18 * k, ry, 0), vector.create(x + bw - 18 * k, ry + 52 * k, 0),
                {colour = theme.a(theme.sky, 0.12), fill = true, rounding = theme.radius * k})
            d:rect(vector.create(x + 18 * k, ry, 0), vector.create(x + bw - 18 * k, ry + 52 * k, 0),
                {colour = theme.a(theme.sky, 0.48), fill = false, thickness = 1, rounding = theme.radius * k})
        end
        text(d, x + 34 * k, ry + 8 * k, t.name or "Loading track...", {colour = i == admin_sel and theme.ink or theme.muted, size = 22, font = "bold"})
        local detail = t.loading and "Loading track data..." or string.format("%d gates  ·  %s  ·  %s",
            #t.gates, t.circuit and "CIRCUIT" or "SPRINT", t.challenge and "CHALLENGE ON" or "CHALLENGE OFF")
        text(d, x + 34 * k, ry + 32 * k, detail, {colour = t.challenge and theme.sun or theme.muted, size = 15, font = "body"})
    end
    if total == 0 then
        text(d, x + 30 * k, y + 155 * k, answered and "Create the first track and place its start gate." or "Waiting for the server's track list...",
            {colour = theme.muted, size = 19, font = "body"})
    end
    local foot = y + bh - 94 * k
    d:rect(vector.create(x + 20 * k, foot, 0), vector.create(x + bw - 20 * k, foot + 1 * k, 0), {colour = theme.line_strong, fill = true})
    text(d, x + bw / 2, foot + 18 * k, "PGUP/PGDN · LB+←/→ SELECT   ·   F8 · LB+RB+A NEW   ·   6 · LB+RB+X EDIT   ·   7 · LB+RB+START CHALLENGE   ·   F9 · LB+RB+B DELETE   ·   F10 CLOSE",
        {colour = theme.ink, size = 14, align = "centre", font = "bold"})
    text(d, x + bw / 2, foot + 50 * k, "Only server staff can change tracks. All edits are shared on this map.",
        {colour = theme.muted, size = 15, align = "centre", font = "body"})
end

local function draw_editor(d, w, h, k)
    local t = editor.track
    for i, g in ipairs(t.gates) do
        draw_gate(d, g, gate_label(t, i), i == editor.sel and SELECTED or NEON, true)
        if i > 1 then
            d:world_line(v3(t.gates[i - 1].p) + vector.create(0, 0.2, 0), v3(g.p) + vector.create(0, 0.2, 0), {colour = "#87CFE450", thickness = 2})
        end
    end
    local mode = MODES[editor.mode]
    local x, y, bw = w / 2 - 460 * k, 16 * k, 920 * k
    panel(d, x, y, bw, 168 * k)
    d:image("images/logo.png", vector.create(x + 22 * k, y + 18 * k, 0),
        vector.create(x + 22 * k + 144 * k, y + 38 * k, 0), {colour = theme.a("#ffffff", 0.92)})
    text(d, x + bw - 22 * k, y + 17 * k, "TRACK STUDIO  /  GATE EDITOR", {colour = theme.sky, size = 18, align = "right", font = "bold"})
    text(d, x + bw / 2, y + 51 * k, string.format("%s   ·   %s   ·   GATE %d OF %d", t.name ~= "" and t.name or "NEW TRACK",
        t.circuit and "CIRCUIT" or "SPRINT", editor.sel, #t.gates), {colour = theme.ink, size = 24, align = "centre", font = "bold"})
    text(d, x + bw / 2, y + 84 * k, "1 MODE: " .. mode.name .. "   ·   " .. mode.help, {colour = theme.sun, size = 19, align = "centre", font = "bold"})
    text(d, x + bw / 2, y + 113 * k, "F8 PLACE 3 M AHEAD   ·   F9 DELETE   ·   PGUP/PGDN SELECT   ·   6 MOVE 3 M AHEAD   ·   7 CIRCUIT   ·   F7 SAVE   ·   F10 CLOSE",
        {colour = theme.muted, size = 14, align = "centre", font = "body"})
    text(d, x + bw / 2, y + 139 * k, "PAD: LB+RB+A PLACE   ·   LB+RB+X MOVE   ·   LB+RB+B DELETE   ·   LB+←/→ SELECT   ·   RB+D-PAD EDIT   ·   LB+RB+Y MODE",
        {colour = theme.muted, size = 14, align = "centre", font = "body"})
end

local function standings()
    local list = {}
    for _, id in ipairs(race.order) do table.insert(list, {id = id, r = race.racers[id]}) end
    table.sort(list, function(a, b)
        local ra, rb = a.r, b.r
        if (ra.dnf ~= nil) ~= (rb.dnf ~= nil) then return rb.dnf ~= nil end
        if ra.ms and rb.ms then return ra.ms < rb.ms end
        if (ra.ms ~= nil) ~= (rb.ms ~= nil) then return ra.ms ~= nil end
        if ra.n ~= rb.n then return ra.n > rb.n end
        return ra.at_ms < rb.at_ms
    end)
    return list
end

local function draw_results(d, w, h, k, list)
    local bw, row = 900 * k, 46 * k
    local x, y = (w - bw) / 2, h * 0.16
    panel(d, x, y, bw, row * (#list + 3) + 30 * k)
    d:image("images/logo.png", vector.create(x + 24 * k, y + 17 * k, 0),
        vector.create(x + 24 * k + 122 * k, y + 34 * k, 0), {colour = theme.a("#ffffff", 0.9)})
    text(d, x + 160 * k, y + 22 * k, "RESULTS", {colour = theme.muted, size = 18, font = "bold"})
    text(d, x + bw / 2, y + 18 * k, race.track.name, {colour = theme.ink, size = 36, align = "centre", font = "bold"})
    local cols = {x + 30 * k, x + 110 * k, x + 560 * k, x + 730 * k}
    local hy = y + 22 * k + row * 1.3
    for i, head in ipairs({"POS", "SKATER", "TIME", race.track.circuit and "BEST LAP" or "GAP"}) do
        text(d, cols[i], hy, head, {colour = theme.muted, size = 20, font = "bold"})
    end
    local winner = list[1] and list[1].r.ms
    for i, item in ipairs(list) do
        local r = item.r
        local ry = hy + row * i
        local colour = item.id == race.me and theme.sun or (i == 1 and r.ms and theme.mint or theme.ink)
        local last = r.dnf and "" or (race.track.circuit and clock_text(r.best)) or (winner and r.ms and i > 1 and ("+" .. clock_text(r.ms - winner)) or "")
        text(d, cols[1], ry, r.dnf and "-" or tostring(i), {colour = colour, size = 28, font = "bold"})
        text(d, cols[2], ry, r.name or "?", {colour = colour, size = 28})
        text(d, cols[3], ry, r.ms and clock_text(r.ms) or ("DNF (" .. tostring(r.dnf) .. ")"), {colour = colour, size = 28})
        text(d, cols[4], ry, last, {colour = colour, size = 28})
    end
    text(d, x + bw / 2, hy + row * (#list + 1), "F10 closes", {colour = theme.muted, size = 20, align = "centre", font = "body"})
end

local function draw_race(d, w, h, k, t)
    local mine = race.racers[race.me]
    local list = standings()
    if race.phase == "done" then return draw_results(d, w, h, k, list) end
    -- the gates: the next bright with its arrow, the one after dimmer, the rest faint
    if mine and not done(mine) then
        for i = race.next + 2, #race.seq do
            local gi = race.seq[i]
            if gi ~= race.seq[race.next] and gi ~= race.seq[race.next + 1] then draw_gate(d, race.track.gates[gi], nil, {nil, nil, "#87CFE425", nil}, false) end
        end
        local after = race.seq[race.next + 1]
        if after then draw_gate(d, race.track.gates[after], nil, NEON_DIM, false) end
        local gi = race.seq[race.next]
        local ok, pos = pcall(R.skater.position)
        local g = race.track.gates[gi]
        local far = ok and pos and string.format("  %dm", math.floor(distance(pos, v3(g.p)))) or ""
        local label = race.next == #race.seq and "FINISH" or (race.next == 1 and "START" or ("GATE " .. race.next - 1))
        draw_gate(d, g, label .. far, NEON, true)
    end
    -- the bar
    local elapsed = (mine and mine.ms) or (race.started_at and (t - race.started_at)) or 0
    local position = 1
    for i, item in ipairs(list) do
        if item.id == race.me then position = i end
    end
    local gates_total = #race.seq - 1
    local lap_len = #race.track.gates - (race.track.circuit and 0 or 1)
    local lap = race.track.circuit and math.clamp(math.floor(math.max(race.next - 2, 0) / lap_len) + 1, 1, race.laps) or 1
    local bx, by, bw = w / 2 - 440 * k, 16 * k, 880 * k
    panel(d, bx, by, bw, 88 * k)
    d:image("images/logo.png", vector.create(bx + 18 * k, by + 28 * k, 0),
        vector.create(bx + 18 * k + 122 * k, by + 45 * k, 0), {colour = theme.a("#ffffff", 0.9)})
    text(d, bx + 158 * k, by + 14 * k, race.challenge and "SOLO CHALLENGE" or "TRACK RACE",
        {colour = theme.sky, size = 15, font = "bold"})
    text(d, bx + 158 * k, by + 42 * k, race.track.name, {colour = theme.ink, size = 23, font = "bold"})
    text(d, bx + bw - 18 * k, by + 31 * k,
        string.format("P%d/%d   LAP %d/%d   GATE %d/%d   %s", position, #list, lap, race.laps,
            math.clamp(race.next - 2, 0, gates_total), gates_total, clock_text(elapsed)),
        {colour = theme.ink, size = 23, align = "right", font = "bold"})
    if race.missed_at and t - race.missed_at < 1500 then
        text(d, w / 2, h * 0.32, "MISSED THE GATE - GO BACK THROUGH IT", {colour = theme.danger, size = 40, align = "centre", font = "bold", shadow = true})
    end
    if mine and mine.ms then
        text(d, w / 2, h * 0.3, string.format("FINISHED  P%d  %s", position, clock_text(mine.ms)), {colour = theme.mint, size = 54, align = "centre", font = "bold", shadow = true})
    elseif mine and mine.dnf then
        text(d, w / 2, h * 0.3, "OUT: " .. string.upper(mine.dnf), {colour = theme.danger, size = 54, align = "centre", font = "bold", shadow = true})
    end
    -- the standings
    local bw, row = 380 * k, 34 * k
    local x, y = w - bw - 24 * k, 120 * k
    panel(d, x, y, bw, row * (#list + 1) + 14 * k)
    text(d, x + 16 * k, y + 10 * k, "STANDINGS", {colour = theme.muted, size = 18, font = "bold"})
    for i, item in ipairs(list) do
        local r = item.r
        local value = r.ms and clock_text(r.ms) or r.dnf and "OUT" or ("G" .. math.max(r.n - 1, 0))
        local colour = item.id == race.me and theme.sun or theme.ink
        text(d, x + 16 * k, y + 8 * k + row * i, string.format("%d. %s", i, r.name or "?"), {colour = colour, size = 22})
        text(d, x + bw - 16 * k, y + 8 * k + row * i, value, {colour = colour, size = 22, align = "right"})
    end
end

-- The challenges' markers, while not racing: a sun-yellow beacon on each start within 150 m, its start gate, the
-- name and the record over it; by the nearest, within reach, what F8 does.
local CHALLENGE_GATE = {"#FFD97218", "#FFD97240", "#FFD97290", "#FFF4D6D0"}
local function draw_challenges(d, w, h, k)
    local ok, pos = pcall(R.skater.position)
    if not ok or not pos then return end
    for _, t in ipairs(tracks) do
        local g = t.challenge and not t.loading and t.gates[1]
        if g and distance(pos, v3(g.p)) <= CHALLENGE_SHOWN then
            local base = v3(g.p)
            d:world_line(base, base + vector.create(0, 40, 0), {colour = theme.a(theme.sun, 0.55), thickness = 3})
            draw_gate(d, g, nil, CHALLENGE_GATE, true)
            d:world_text(base + vector.create(0, g.h + 1.6, 0), "CHALLENGE  " .. string.upper(t.name),
                {colour = theme.sun, size = 26, align = "centre", shadow = true, font = "bold"})
            d:world_text(base + vector.create(0, g.h + 0.8, 0), t.best and ("record " .. clock_text(t.best.ms) .. "  " .. t.best.n) or "no record yet",
                {colour = theme.ink, size = 20, align = "centre", shadow = true, font = "body"})
        end
    end
    local near = challenge_near(CHALLENGE_REACH)
    if near then
        panel(d, w / 2 - 300 * k, h * 0.74, 600 * k, 64 * k)
        text(d, w / 2, h * 0.74 + 10 * k, "F8  start the challenge: " .. near.name, {colour = theme.sun, size = 24, align = "centre", font = "bold"})
        text(d, w / 2, h * 0.74 + 38 * k, near.best and ("record " .. clock_text(near.best.ms) .. " by " .. near.best.n) or "no record yet - set one",
            {colour = theme.muted, size = 17, align = "centre", font = "body"})
    end
end

R.on("draw", function(d)
    local size = d:size()
    local w, h = size.x, size.y
    local k = h / 1080
    local t = now()
    if race then return draw_race(d, w, h, k, t) end
    if admin_menu then return draw_admin_menu(d, w, h, k) end
    if editor then return draw_editor(d, w, h, k) end
    if not joined and not host then return draw_challenges(d, w, h, k) end
    local preview = (host and host.track) or (joined and joined.track)
    if preview then draw_track(d, preview, false) end
    if joined then
        panel(d, w / 2 - 330 * k, 16 * k, 660 * k, 56 * k)
        text(d, w / 2, 30 * k, "In " .. tostring(joined.name) .. "'s race: waiting for the start  (F10 leaves)",
            {colour = theme.ink, size = 24, align = "centre", font = "bold"})
    end
end)

-- ---- tick and lifetime ----------------------------------------------------------------------------

-- Keep the keyboard shortcuts available while the action defaults are on the controller (ReSkate's
-- Binds page changes those). reskate.input.key takes a Windows virtual-key code ("a virtual key from
-- 1 to 254"); should a game take the key's name instead, that is used from then on.
local keyboard_editor_keys = {
    {0x21, "PageUp", "gate_prev"}, {0x22, "PageDown", "gate_next"}, {0x75, "F6", "gate_next"},
    {0x77, "F8", "gate_add"}, {0x78, "F9", "gate_delete"},
    {0x31, "1", "adjust_mode"}, {0x32, "2", "adjust_down"}, {0x33, "3", "adjust_up"}, {0x34, "4", "adjust_left"},
    {0x35, "5", "adjust_right"}, {0x36, "6", "gate_here"}, {0x37, "7", "gate_circuit"},
}
local keyboard_was_down = {}
local keys_by_name = false

local function key_down(code, name)
    local ok, down = pcall(R.input.key, keys_by_name and name or code)
    if not ok and not keys_by_name then
        local by_name, down_by_name = pcall(R.input.key, name)
        diag("input.key(" .. code .. "): " .. tostring(down) .. (by_name and "; by name it works" or ""))
        if by_name then keys_by_name, ok, down = true, true, down_by_name end
    end
    if not ok then
        diag("input.key(" .. name .. "): " .. tostring(down))
        return false
    end
    if type(down) ~= "boolean" then diag("input.key returned a " .. type(down)) end
    return down == true
end

-- Not while the player types in chat or a menu is open.
local function keys_allowed()
    local ok, focus = pcall(R.input.focus)
    if ok and type(focus) == "table" then return focus.gameplay ~= false and focus.interface_open ~= true end
    local ok_allowed, allowed = pcall(R.input.allowed)
    return not ok_allowed or allowed ~= false
end

local function poll_editor_keys()
    if menu.open or not keys_allowed() then
        keyboard_was_down = {}
        return
    end
    for _, key in ipairs(keyboard_editor_keys) do
        local code, name, action = key[1], key[2], key[3]
        local down = key_down(code, name)
        if down and not keyboard_was_down[code] then edit_key(action) end
        keyboard_was_down[code] = down
    end
end

R.on("tick", function()
    local t = now()
    flush()
    poll_editor_keys()
    if not answered and t - last_hello >= HELLO_EVERY_MS then hello() end
    tell_map()
    if tracks_map and tracks_map ~= map_key() then
        tracks, tracks_map = {}, nil
        hello()
    end
    local ok, pos = pcall(R.skater.position)
    if ok and pos then
        table.insert(trail, 1, {at = t, pos = pos})
        if #trail > 90 then table.remove(trail) end
    end
    for id, o in pairs(offers) do
        if t > o.until_ms then offers[id] = nil end
    end
    if joined and not offers[joined.race] then
        joined = nil
        toast("The race's host is gone.", "warn")
    end
    if host and t - host.last_offer >= OFFER_EVERY_MS then broadcast_offer() end
    if race then race_tick(t) end
    scan_tick()
    if menu.open and menu.state().tab == "HOME" and hud and hud.on_server() then hub.refresh(hud.my_name()) end
    publish()
end)

local function reset()
    if race then leave_race() end
    leave_flag(true)
    cancel_host(true)
    editor = nil
    admin_menu = false
    setup_open = false
    offers = {}
    tracks, tracks_map, answered = {}, nil, false
    hello()
end

R.on("level_loaded", reset)
R.on("level_unloading", reset)
R.on("unload", function()
    release_hold()
    pcall(R.physics.competition, false)
    pcall(R.throwdowns.flag.leave)
end)

menu.attach()   -- the menu's draw handler, after the races' own: the menu is drawn over them

R.throwdowns.mode{
    id = MODE, name = "Cocojambo Deathraces",
    description = "Choose a Cocojambo track, set the race size and laps, then skate the gates in order.",
    color = 0xffffb400, card = "spot", order = 10,
    players = {min = 1, max = 10, default = 6},
}
for action, key in pairs({
    race_editor = "F7", coja_menu = "F5", gate_add = "Pad.LB+Pad.RB+Pad.A", gate_delete = "Pad.LB+Pad.RB+Pad.B", race_leave = "F10",
    gate_prev = "Pad.LB+Pad.Left", gate_next = "Pad.LB+Pad.Right", adjust_mode = "Pad.LB+Pad.RB+Pad.Y",
    adjust_down = "Pad.RB+Pad.Down", adjust_up = "Pad.RB+Pad.Up", adjust_left = "Pad.RB+Pad.Left", adjust_right = "Pad.RB+Pad.Right",
    gate_here = "Pad.LB+Pad.RB+Pad.X", gate_circuit = "Pad.LB+Pad.RB+Pad.Start",
}) do
    R.input.bind(action, key)
end
