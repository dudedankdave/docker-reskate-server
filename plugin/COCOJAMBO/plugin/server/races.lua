-- Races, the server copy: the session's authority. Tracks are checkpoints on a map, built in the game by staff
-- (/race new) and kept in this server's storage, or shipped in the package (tracks/*.json). A race runs from
-- /race (a vote when the map has several tracks) through sign-up, start spots, the countdown, GO, checkpoints and
-- the finish (from the poses the server has of every skater) to the results and the track record.
--
-- The log lines the server's tools read (all start "[coco race] "):
--   started <track> <racers>                                  a race went GO
--   finished <track> <place>/<of> <ms> <SteamID64> <name>     each finisher, as they cross the line
--   dnf <track> <SteamID64> <name>                            who did not finish
--   record <track> <ms> <SteamID64> <name>                    a new track record
--   saved <map> <track> <SteamID64>  and  track <base64 json> a track was saved (race import takes the second)
local util = require("lib/util")
local route = require("lib/route")
local calls = require("server/calls")
local core = require("server/core")
local config = require("server/config")
local b64 = require("lib/base64")

local now = reskate.time.now
local json = reskate.json
local send = core.send
local ICON = (config.icon ~= "" and (config.icon .. " ")) or ""

local map = ""          -- the map the server is on
local tracks = {}       -- this map's tracks: id -> track
local race = nil        -- the race going on
local poll = nil        -- the vote on the track
local edits = {}        -- the editor, per player: {track, undo}
local cooldown_until = 0

local function log(line) reskate.console.print("[coco race] " .. line) end
local function say(text) calls.say(ICON .. text) end
local tell = calls.tell
local function staff(id) return core.staff(id, config) end
local function epoch()
    if os and os.time then return os.time() end
    return 0
end

-- ---- tracks ------------------------------------------------------------------------------------------
local function current_map()
    local ok, world = pcall(reskate.session.world)
    if ok and type(world) == "table" and type(world.map) == "string" then return world.map end
    return ""
end

local function valid(t)
    return type(t) == "table" and type(t.id) == "string" and util.slug(t.id) == t.id and type(t.title) == "string"
        and route.problem(t) == nil
end

-- The package's tracks for this map (tracks/*.json, each with its "map"), then this server's own, which win.
local function load_tracks()
    map = current_map()
    tracks = {}
    local slug = util.slug(map)
    if not slug then return end
    local ok, names = pcall(reskate.package.list, "tracks")
    if ok and type(names) == "table" then
        for _, name in ipairs(names) do
            if name:sub(-5) == ".json" then
                local read, t = pcall(reskate.package.read_json, "tracks/" .. name)
                if read and valid(t) and util.slug(t.map) == slug then
                    t.shipped = true
                    tracks[t.id] = t
                end
            end
        end
    end
    local prefix = "track." .. slug .. "."
    for _, key in ipairs(reskate.storage.keys()) do
        if key:sub(1, #prefix) == prefix then
            local t = reskate.storage.get(key)
            if valid(t) then tracks[t.id] = t end
        end
    end
end

local function sorted()
    local list = {}
    for _, t in pairs(tracks) do list[#list + 1] = t end
    table.sort(list, function(a, b) return a.title:lower() < b.title:lower() end)
    return list
end
local function names()
    local list = {}
    for _, t in ipairs(sorted()) do list[#list + 1] = t.id end
    if #list == 0 then return "none yet" end
    return table.concat(list, ", ")
end
local function best_of(t) return reskate.storage.get(route.record_key(map, t)) end

-- What a client needs of a track to draw it: {c = {x, y, z}, r} per checkpoint.
local function packed(t)
    local cps = {}
    for i, cp in ipairs(t.checkpoints) do cps[i] = {c = cp.c, r = cp.r} end
    return cps
end

-- ---- players' positions -----------------------------------------------------------------------------
local function skaters()
    local out = {}
    local ok, list = pcall(reskate.session.skaters)
    if ok and type(list) == "table" then
        for _, s in ipairs(list) do out[s.id] = s end
    end
    return out
end
local function where(id)
    local s = skaters()[id]
    if s and s.age_ms < 2000 then return s.position, s.heading end
    return nil
end

-- ---- tools locked while racing ----------------------------------------------------------------------
-- What each locked racer had ("locked") and what is owed to one who left ("owed") are kept in storage, so a
-- reload or the server stopping mid-race leaves nobody locked: on loading, every lock on record is given back.
local TOOLS = {"noclip", "nobail", "boosts", "tuning", "objects", "ollies"}
local locked = reskate.storage.get("locked") or {}
local owed = reskate.storage.get("owed") or {}
local function plain(perms)
    local out = {}
    for _, tool in ipairs(TOOLS) do out[tool] = perms[tool] end
    return out
end
local function lock(r)
    if #config.locks == 0 then return end
    calls.later(2, function()
        local p = calls.players()[r.id]
        if not p or r.unlocked then return end
        local had = p.permissions
        if not had then return end
        r.had = plain(had)
        locked[r.id] = r.had
        reskate.storage.set("locked", locked)
        local racing = had:clone()
        for _, tool in ipairs(config.locks) do racing[tool] = false end
        p:apply_permissions(racing)
    end)
end
local function give_back(id, perms)
    local p = calls.players()[id]
    if p then
        p:apply_permissions(perms)
    else
        owed[id] = perms
        reskate.storage.set("owed", owed)
    end
    locked[id] = nil
    reskate.storage.set("locked", locked)
end
local function unlock(r)
    r.unlocked = true
    if not r.had then return end
    local had = r.had
    r.had = nil
    calls.later(1, function() give_back(r.id, had) end)
end

-- ---- the race ---------------------------------------------------------------------------------------
local function racer(id)
    if not race then return nil end
    for _, r in ipairs(race.racers) do
        if r.id == id then return r end
    end
    return nil
end
local function to_racers(message)
    if not race then return end
    for _, r in ipairs(race.racers) do
        if not r.out then send(r.id, message) end
    end
end
local function spot_of(r) return race.spots[r.slot] or race.spots[#race.spots] end
local function place_on_spot(r)
    calls.move(r.id, spot_of(r))
    r.moved_at = now()
end

local function join(p)
    if race.trial and p.id ~= race.by then return tell(p.id, "That is a test run of a track being built - join the next race.") end
    if racer(p.id) then return tell(p.id, "You're in already - stay on your spot.") end
    if #race.racers >= config.max_racers then return tell(p.id, "The race is full (" .. config.max_racers .. ").") end
    race.seq = race.seq + 1
    local r = {id = p.id, name = util.one_line(p.name, 24), slot = #race.racers + 1, next = 1, seq = race.seq, penalty = 0}
    race.racers[#race.racers + 1] = r
    if race.ready then place_on_spot(r) end
    tell(p.id, "You're in! Wait on your spot for GO.")
    send(p.id, {t = "signup", title = race.track.title, until_ms = math.max(0, race.signup_until - now())})
end

-- by: who started it (a player) or nil (the console); opts: {voters, signup_s, trial, track}
local function start(by, id, opts)
    opts = opts or {}
    if race then return "A race is on already." end
    if now() < cooldown_until and not opts.trial then
        return "Wait " .. math.ceil((cooldown_until - now()) / 1000) .. " s before the next race."
    end
    local t = opts.track or tracks[id or ""]
    if not t then
        local list = sorted()
        if id and id ~= "" then return "No track " .. id .. " here. Tracks: " .. names() end
        if #list == 0 then return "This map has no race tracks yet." end
        t = list[math.random(1, #list)]
    end
    local problem = route.problem(t)
    if problem then return "The track can't be raced: " .. problem .. "." end
    if poll then poll = nil end
    local alone = util.count(calls.players()) <= 1
    local signup = (opts.signup_s or ((alone or opts.trial) and config.solo_signup_s or config.signup_s)) * 1000
    race = {phase = "signup", track = t, zones = route.build(t), limit = route.limit(t), racers = {}, seq = 0,
            by = by and by.id or "", by_name = by and util.one_line(by.name, 24) or "the server", trial = opts.trial,
            spots = route.spots(t.start, t.heading, config.max_racers), splits = {},
            ready = true, signup_until = now() + signup}
    if by then join(by) end
    for _, v in ipairs(opts.voters or {}) do
        if not racer(v.id) then join(v) end
    end
    if not opts.trial and not alone then
        local laps = (t.laps or 1) > 1 and (" (" .. t.laps .. " laps)") or ""
        say(race.by_name .. " starts a race on " .. t.title .. laps .. "! Type /race in the next " .. math.floor(signup / 1000) .. " s to join.")
    end
    return "Race on " .. t.title .. ": sign-up open."
end

local function finish(reason)
    local r_ = race
    if not r_ then return end
    race = nil
    for _, r in ipairs(r_.racers) do unlock(r) end
    if r_.phase ~= "running" then
        if reason then log("called off: " .. reason) end
        for _, r in ipairs(r_.racers) do send(r.id, {t = "end", cancelled = true, rows = {}}) end
        return
    end
    local done, rows = {}, {}
    for _, r in ipairs(r_.racers) do
        if r.place then done[#done + 1] = r end
    end
    table.sort(done, function(a, b) return a.place < b.place end)
    for _, r in ipairs(done) do rows[#rows + 1] = {place = r.place, name = r.name, ms = r.ms} end
    for _, r in ipairs(r_.racers) do
        if not r.place then
            rows[#rows + 1] = {name = r.name, dnf = true}
            if not r_.trial then log("dnf " .. r_.track.id .. " " .. r.id .. " " .. r.name) end
        end
    end
    if reason then log(r_.track.id .. ": " .. reason) end
    local first = done[1]
    local record, best = false, best_of(r_.track)
    if first and first.penalty == 0 and not r_.trial and (not best or first.ms < best.ms) then
        local ok = pcall(reskate.storage.set, route.record_key(map, r_.track), {ms = first.ms, name = first.name, id = first.id, at = epoch()})
        if ok then
            record = true
            log("record " .. r_.track.id .. " " .. math.floor(first.ms) .. " " .. first.id .. " " .. first.name)
            say("New record on " .. r_.track.title .. ": " .. first.name .. " " .. util.clock(first.ms) .. "!")
        end
    end
    if not r_.trial then
        local top = {}
        for i = 1, math.min(3, #done) do top[#top + 1] = util.ordinal(i) .. " " .. done[i].name .. " " .. util.clock(done[i].ms) end
        if #top > 0 then say("Race over on " .. r_.track.title .. ": " .. table.concat(top, ", ")) else say("Race over: nobody finished.") end
        cooldown_until = now() + config.cooldown_s * 1000
    end
    for _, r in ipairs(r_.racers) do
        if not r.out then send(r.id, {t = "end", rows = rows, record = record, best = best and best.ms or nil}) end
    end
end

local function stop(reason)
    if not race then return "No race is on." end
    finish(reason or "stopped")
    return "Race stopped."
end

local function countdown()
    local alive = {}
    for _, r in ipairs(race.racers) do
        if not r.out then alive[#alive + 1] = r end
    end
    if #alive == 0 then
        if not race.trial then say("Nobody joined the race.") end
        race = nil
        return
    end
    race.phase = "countdown"
    race.go_at = now() + config.countdown_s * 1000
    for _, r in ipairs(alive) do lock(r) end
    local best = best_of(race.track)
    for _, r in ipairs(alive) do
        send(r.id, {t = "race", title = race.track.title, laps = race.track.laps or 1, limit = race.limit, cps = packed(race.track),
                    go_in_ms = config.countdown_s * 1000, racers = #alive, best = best and best.ms or nil, trial = race.trial})
        if not core.has_hud(r.id) then tell(r.id, "Race on " .. race.track.title .. ": GO in " .. config.countdown_s .. " s - stay on your spot!") end
    end
end

-- Until GO a racer stays on their spot: further off it than hold_radius and they go back (at most every 1.5 s,
-- and not in the second after a move, while the pose is still the old one).
local function hold(all)
    for _, r in ipairs(race.racers) do
        local s = all[r.id]
        if s and not r.out and r.moved_at and now() - r.moved_at > 1500 and s.age_ms < 1000 then
            if route.flat(s.position, spot_of(r)) > config.hold_radius then place_on_spot(r) end
        end
    end
end

local function go(all)
    race.phase = "running"
    local count = 0
    for _, r in ipairs(race.racers) do
        if not r.out then
            count = count + 1
            local s = all[r.id]
            if s then
                r.last, r.last_t = route.point(s.position), now() - s.age_ms
                if route.flat(s.position, spot_of(r)) > config.hold_radius * 1.5 then
                    r.penalty = config.false_start_s
                    send(r.id, {t = "false", s = r.penalty})
                    tell(r.id, "False start: +" .. r.penalty .. " s.")
                end
            end
        end
    end
    if not race.trial then log("started " .. race.track.id .. " " .. count) end
    to_racers({t = "go"})
    for _, r in ipairs(race.racers) do
        if not core.has_hud(r.id) and not r.out then tell(r.id, "GO!") end
    end
end

local function run(all)
    local clock = now() - race.go_at
    local crossed = {}
    for _, r in ipairs(race.racers) do
        local s = all[r.id]
        if s and not r.out and not r.ms then
            local t = now() - s.age_ms
            if r.last and t > r.last_t then
                for _, pass in ipairs(route.advance(r, race.zones, r.last, r.last_t, s.position, t)) do
                    local at = math.max(0, pass.at_ms - race.go_at)
                    if pass.index < #race.zones then
                        r.gap = route.split(race.splits, pass.index, at)
                        send(r.id, {t = "cp", k = pass.index, n = #race.zones, ms = at, gap = r.gap})
                    else
                        r.ms = at + r.penalty * 1000
                        crossed[#crossed + 1] = r
                    end
                end
            end
            if not r.last or t > r.last_t then r.last, r.last_t = route.point(s.position), t end
        end
    end
    -- who crossed the line this tick, in the order they crossed it
    table.sort(crossed, function(a, b) return a.ms < b.ms end)
    for _, r in ipairs(crossed) do
        race.placed = (race.placed or 0) + 1
        r.place = race.placed
        local of = 0
        for _, o in ipairs(race.racers) do
            if not o.out or o.place then of = of + 1 end
        end
        if not race.trial then
            log("finished " .. race.track.id .. " " .. r.place .. "/" .. of .. " " .. math.floor(r.ms) .. " " .. r.id .. " " .. r.name)
            if of > 1 then say(r.name .. " finished " .. util.ordinal(r.place) .. " in " .. util.clock(r.ms) .. "!") end
        end
        send(r.id, {t = "fin", place = r.place, ms = r.ms})
    end
    -- the standings for the racers' HUDs, every half second, each told when theirs changes
    if now() >= (race.standings_at or 0) then
        race.standings_at = now() + 500
        local at = {}
        for id, s in pairs(all) do at[id] = s.position end
        local order = route.order(race.racers, race.zones, at)
        for i, r in ipairs(order) do
            if r.shown_place ~= i or r.shown_of ~= #order then
                r.shown_place, r.shown_of = i, #order
                send(r.id, {t = "pos", place = i, of = #order})
            end
            if #order > 1 and core.has_hud(r.id) then
                local rows = route.board(order, r.id, 5)
                local sig = json.encode(rows)
                if r.board_sig ~= sig then
                    r.board_sig = sig
                    send(r.id, {t = "board", rows = rows})
                end
            end
        end
    end
    local open = false
    for _, r in ipairs(race.racers) do
        if not r.out and not r.ms then open = true end
    end
    if not open then
        finish()
    elseif clock >= race.limit * 1000 then
        say("Time's up!")
        finish("time's up")
    end
end

-- ---- the vote on the track ----------------------------------------------------------------------------
local function poll_message()
    local counts = {}
    for i in ipairs(poll.options) do counts[i] = 0 end
    for _, n in pairs(poll.votes) do counts[n] = counts[n] + 1 end
    local options = {}
    for i, t in ipairs(poll.options) do
        local best = best_of(t)
        options[i] = {n = i, title = t.title, laps = t.laps or 1, votes = counts[i], best = best and best.ms or nil}
    end
    return {t = "poll", options = options, until_ms = math.max(0, poll.until_at - now()), by = poll.by_name}, counts
end
local function publish_poll() send("all", (poll_message())) end

local function start_poll(p)
    local pool = sorted()
    local options = {}
    while #pool > 0 and #options < config.poll_options do
        options[#options + 1] = table.remove(pool, math.random(1, #pool))
    end
    poll = {options = options, votes = {}, voters = {}, by = p, by_name = util.one_line(p.name, 24), until_at = now() + config.poll_s * 1000}
    poll.voters[p.id] = p
    local parts = {}
    for i, t in ipairs(options) do parts[#parts + 1] = i .. " " .. t.title end
    say("Which track? Vote with /race <number> in the next " .. config.poll_s .. " s - voters race too: " .. table.concat(parts, " | "))
    publish_poll()
end

local function vote(p, n)
    if not poll.options[n] then return tell(p.id, "Vote with /race 1 to /race " .. #poll.options .. ".") end
    poll.votes[p.id] = n
    poll.voters[p.id] = p
    send(p.id, {t = "voted", n = n})
    local everyone = true
    for id in pairs(calls.players()) do
        if not poll.votes[id] then everyone = false end
    end
    if everyone then poll.until_at = now() end
    publish_poll()
end

local function end_poll()
    local _, counts = poll_message()
    local most, best = -1, {}
    for i, c in ipairs(counts) do
        if c > most then most, best = c, {i} elseif c == most then best[#best + 1] = i end
    end
    local winner = poll.options[best[math.random(1, #best)]]
    local here = calls.players()
    local voters = {}
    for id in pairs(poll.voters) do
        if here[id] and id ~= poll.by.id then voters[#voters + 1] = here[id] end
    end
    local by = here[poll.by.id]
    poll = nil
    send("all", {t = "poll_end", winner = winner.title})
    local answer = start(by, winner.id, {voters = voters})
    if answer:sub(1, 8) ~= "Race on " then say(answer) end
end

-- ---- the editor: staff build a track where they stand ---------------------------------------------------
local function edit_message(id)
    local e = edits[id]
    local t = e.track
    send(id, {t = "edit", id = t.id, title = t.title, laps = t.laps or 1, start = t.start, heading = t.heading,
              cps = packed(t), dirty = e.dirty or false})
end
local function close_edit(id)
    edits[id] = nil
    send(id, {t = "edit_end"})
end
local function new_track(p, title)
    local id = util.slug(title)
    if not id then return nil, "/race new <title>" end
    if tracks[id] then return nil, "There is a track " .. id .. " here already: /race edit " .. id end
    return {id = id, title = util.one_line(title, 32), map = map, laps = 1, checkpoints = {}, by = util.one_line(p.name, 24), by_id = p.id}
end

local editor   -- the verbs, below
local function edit(p, words, raw)
    local id, verb = p.id, words[1]
    if verb == "new" or verb == "edit" then
        if not staff(id) then return tell(id, "Only staff can build tracks.") end
        if not core.has_hud(id) then return tell(id, "The track editor needs the COCOJAMBO plugin (Thunderstore).") end
        for other, o in pairs(edits) do
            if other ~= id and o.track.id == words[2] then return tell(id, "Someone is editing " .. o.track.title .. " right now.") end
        end
        local t, why
        if verb == "new" then
            local title = {}
            for i = 2, #raw do title[#title + 1] = raw[i] end
            t, why = new_track(p, table.concat(title, " "))
            if not t then return tell(id, why) end
        else
            if not words[2] or not tracks[words[2]] then return tell(id, "/race edit <track>. Tracks here: " .. names()) end
            if race and race.track.id == words[2] then return tell(id, "A race is on on that track - edit it after.") end
            t = util.copy(tracks[words[2]])
            t.shipped = nil
        end
        edits[id] = {track = t, undo = {}, dirty = verb == "new"}
        edit_message(id)
        return tell(id, "Editing " .. t.title .. ": 1 checkpoint here, 2 delete the nearest, 3 insert before the nearest, "
            .. "4 start here, 5 try it, Ctrl+S save, Ctrl+Z undo, X cancel. The last checkpoint is the finish.")
    end
    if not edits[id] then return tell(id, "First /race new <title> or /race edit <track>.") end
    editor(p, words, raw)
end

local function nearest(t, at)
    local best, best_d = nil, nil
    for i, cp in ipairs(t.checkpoints) do
        local d = route.distance(route.vec(cp.c), at)
        if not best_d or d < best_d then best, best_d = i, d end
    end
    return best, best_d
end

editor = function(p, words, raw)
    raw = raw or words
    local id, verb = p.id, words[1]
    local e = edits[id]
    local t = e.track
    local function here()
        local at, heading = where(id)
        if not at then tell(id, "Your position isn't known yet - move a little and try again.") end
        return at, heading
    end
    local function keep()
        e.undo[#e.undo + 1] = util.copy(t)
        if #e.undo > 40 then table.remove(e.undo, 1) end
        e.dirty = true
    end
    local function radius(text) return util.clamp(tonumber(text) or config.checkpoint_radius, route.MIN_RADIUS, route.MAX_RADIUS) end
    if verb == "cp" then
        local at = here()
        if not at then return end
        if #t.checkpoints >= route.MAX_CHECKPOINTS then return tell(id, "At most " .. route.MAX_CHECKPOINTS .. " checkpoints.") end
        keep()
        t.checkpoints[#t.checkpoints + 1] = {c = route.pack(at), r = radius(words[2])}
    elseif verb == "insert" then
        local at = here()
        if not at then return end
        local n = tonumber(words[2]) or nearest(t, at)
        if not n then return editor(p, {"cp", words[3]}) end
        n = util.clamp(math.floor(n), 1, #t.checkpoints)
        keep()
        table.insert(t.checkpoints, n, {c = route.pack(at), r = radius(words[3])})
    elseif verb == "del" then
        local n = tonumber(words[2])
        if not n then
            local at = here()
            if not at then return end
            local d
            n, d = nearest(t, at)
            if not n or d > 30 then return tell(id, "No checkpoint within 30 m; /race del <n> takes one by number.") end
        end
        if not t.checkpoints[n] then return tell(id, "No checkpoint " .. n .. ".") end
        keep()
        table.remove(t.checkpoints, n)
    elseif verb == "radius" then
        local n, m = tonumber(words[2]), tonumber(words[3])
        if not (n and m and t.checkpoints[n]) then return tell(id, "/race radius <checkpoint> <metres>") end
        keep()
        t.checkpoints[n].r = radius(words[3])
    elseif verb == "start" then
        local at, heading = here()
        if not at then return end
        keep()
        t.start = route.pack(at)
        t.heading = math.floor((heading or 0) * 1000 + 0.5) / 1000
    elseif verb == "laps" then
        local n = tonumber(words[2])
        if not n or n < 1 or n > route.MAX_LAPS then return tell(id, "/race laps <1-" .. route.MAX_LAPS .. ">") end
        keep()
        t.laps = math.floor(n)
    elseif verb == "title" then
        local title = {}
        for i = 2, #raw do title[#title + 1] = raw[i] end
        if #title == 0 then return tell(id, "/race title <title>") end
        keep()
        t.title = util.one_line(table.concat(title, " "), 32)
    elseif verb == "undo" then
        local before = table.remove(e.undo)
        if not before then return tell(id, "Nothing to take back.") end
        e.track = before
    elseif verb == "try" then
        local problem = route.problem(t)
        if problem then return tell(id, "Not yet: " .. problem .. ".") end
        if race then return tell(id, "A race is on - try it after.") end
        local answer = start(p, nil, {track = util.copy(t), trial = true})
        if answer:sub(1, 8) ~= "Race on " then return tell(id, answer) end
        return tell(id, "Test run: no record counts. /race stop ends it; the editor stays open.")
    elseif verb == "save" then
        local problem = route.problem(t)
        if problem then return tell(id, "Not saved: " .. problem .. ".") end
        t.updated, t.edited_by = epoch(), util.one_line(p.name, 24)
        local ok, why = pcall(reskate.storage.set, route.track_key(map, t.id), t)
        if not ok then return tell(id, "Not saved: " .. tostring(why)) end
        tracks[t.id] = t
        log("saved " .. (util.slug(map) or "?") .. " " .. t.id .. " " .. id)
        log("track " .. json.encode(t))
        tell(id, t.title .. " saved: " .. #t.checkpoints .. " checkpoint(s), " .. (t.laps or 1) .. " lap(s).")
        return close_edit(id)
    elseif verb == "cancel" then
        tell(id, "Editor closed, nothing saved.")
        return close_edit(id)
    elseif verb == "help" then
        return tell(id, "/race cp [r] | insert [n] [r] | del [n] | radius <n> <m> | start (here) | laps <n> | title <t> | undo | try | save | cancel")
    else
        return tell(id, "Unknown: /race " .. tostring(verb) .. " (/race help-edit)")
    end
    edit_message(id)
end

-- ---- what players type ---------------------------------------------------------------------------------
local EDITOR = {new = true, edit = true, cp = true, insert = true, del = true, radius = true, start = true, laps = true,
                title = true, undo = true, try = true, save = true, cancel = true, ["help-edit"] = true}

local function chat(id, words, raw)
    raw = raw or words
    local p = calls.players()[id]
    if not p then return end
    local word = words[1] or ""
    if word == "start" and not edits[id] then word = "" end  -- /race start: as /race
    if EDITOR[word] then
        if word == "help-edit" then words[1] = "help" end
        return edit(p, words, raw)
    end
    if word == "help" then
        return tell(id, "/race: start a vote or join. /race <n>: vote. /race <track>: race there. /race tracks. /race best [track]. "
            .. "/race stop. Staff: /race new <title>, /race edit <track>, /race delete <track>.")
    elseif word == "tracks" then
        return tell(id, "Tracks here: " .. names())
    elseif word == "best" then
        local t = tracks[words[2] or ""] or (race and race.track)
        if not t then return tell(id, "/race best <track>. Tracks here: " .. names()) end
        local b = best_of(t)
        return tell(id, t.title .. ": " .. (b and (util.clock(b.ms) .. " by " .. b.name) or "no record yet"))
    elseif word == "delete" then
        if not staff(id) then return tell(id, "Only staff can delete tracks.") end
        local t = tracks[words[2] or ""]
        if not t then return tell(id, "/race delete <track>. Tracks here: " .. names()) end
        if t.shipped then return tell(id, t.title .. " comes with the plugin; it can be edited, not deleted.") end
        reskate.storage.set(route.track_key(map, t.id), nil)
        tracks[t.id] = nil
        log("deleted " .. (util.slug(map) or "?") .. " " .. t.id .. " " .. id)
        return tell(id, t.title .. " deleted.")
    elseif word == "stop" then
        local by = (race and race.by) or (poll and poll.by.id)
        if not by then return tell(id, "No race is on.") end
        if id ~= by and not staff(id) then return tell(id, "Only who started it or staff can stop it.") end
        if poll and not race then
            poll = nil
            send("all", {t = "poll_end"})
            return say("The vote was called off.")
        end
        return tell(id, stop("stopped by " .. p.name))
    end
    if poll then
        local n = tonumber(word)
        if not n then
            for i, t in ipairs(poll.options) do
                if t.id == word then n = i end
            end
        end
        if n then return vote(p, n) end
        poll.voters[id] = p
        return tell(id, "You're in - vote with /race 1 to /race " .. #poll.options .. ".")
    elseif race and race.phase == "signup" then
        return join(p)
    elseif race then
        return tell(id, "A race is on - the next one can start once it's over.")
    end
    if word == "" and util.count(tracks) >= 2 and util.count(calls.players()) > 1 then
        if now() < cooldown_until then return tell(id, "Wait " .. math.ceil((cooldown_until - now()) / 1000) .. " s before the next race.") end
        return start_poll(p)
    end
    local answer = start(p, word)
    if answer:sub(1, 8) ~= "Race on " then tell(id, answer) end
end

reskate.on("chat", function(line)
    if line.server then return end
    local words, raw = util.command_words(line.text, "race")
    if words then chat(line.sender, words, raw) end
end)

reskate.on("message", function(from, text)
    if from.server then return end
    local m = util.decode(json, text)
    if not m then return end
    local p = calls.players()[from.id]
    if not p then return end
    if m.t == "vote" and type(m.n) == "number" and poll then
        vote(p, m.n)
    elseif m.t == "edit_key" and type(m.verb) == "string" and edits[from.id] then
        local verbs = {cp = true, del = true, insert = true, start = true, try = true, save = true, undo = true, cancel = true}
        if verbs[m.verb] then editor(p, {m.verb}) end
    elseif m.t == "race" and not edits[from.id] then
        -- the HUD's race key: as /race
        chat(from.id, {})
    end
end)

-- ---- the tick ------------------------------------------------------------------------------------------
reskate.on("tick", function()
    if poll and now() >= poll.until_at then end_poll() end
    if not race then return end
    local all = skaters()
    local here = calls.players()
    for _, r in ipairs(race.racers) do
        if not r.out and not here[r.id] then
            r.out = true
            unlock(r)
        end
    end
    if race.phase == "signup" then
        if now() >= race.signup_until then countdown() else hold(all) end
    elseif race.phase == "countdown" then
        hold(all)
        if now() >= race.go_at then go(all) end
    elseif race.phase == "running" then
        run(all)
    end
end)

reskate.on("level_unloading", function()
    poll = nil
    for id in pairs(edits) do close_edit(id) end
    if race then stop("the map changed") end
end)
reskate.on("level_loaded", function() load_tracks() end)

reskate.on("player_joined", function(p)
    local back = owed[p.id]
    if back then
        owed[p.id] = nil
        reskate.storage.set("owed", owed)
        calls.later(1, function() p:apply_permissions(back) end)
    end
end)
reskate.on("player_left", function(p)
    edits[p.id] = nil
    if poll then poll.votes[p.id], poll.voters[p.id] = nil, nil end
    local r = racer(p.id)
    if r then
        r.out = true
        unlock(r)
    end
end)

-- ---- the console -----------------------------------------------------------------------------------------
reskate.console.command("race", "COCOJAMBO races: race start [track] | stop | status | records | import <base64 json> | chat <SteamID64> ...",
    function(args)
        local verb = args[1] or ""
        if verb == "start" then
            if map ~= current_map() then load_tracks() end
            return reskate.console.print(start(nil, args[2]))
        elseif verb == "stop" then
            return reskate.console.print(stop("stopped by the console"))
        elseif verb == "status" then
            if race then
                local list = {}
                for _, r in ipairs(race.racers) do list[#list + 1] = r.name .. ((r.out and " (gone)") or (r.place and (" " .. r.place .. ".")) or "") end
                reskate.console.print("race: " .. race.phase .. " on " .. race.track.id .. ", " .. #race.racers .. " racer(s): " .. table.concat(list, ", "))
            else
                reskate.console.print("race: none" .. (poll and ", a vote is on" or ""))
            end
            local open = {}
            for _, e in pairs(edits) do open[#open + 1] = e.track.id end
            return reskate.console.print("map: " .. map .. ", tracks: " .. names() .. ", editing: " .. (#open > 0 and table.concat(open, ", ") or "none"))
        elseif verb == "records" then
            for _, t in ipairs(sorted()) do
                local b = best_of(t)
                reskate.console.print(t.id .. ": " .. (b and (util.clock(b.ms) .. " " .. b.name .. " (" .. b.id .. ")") or "-"))
            end
            return
        elseif verb == "import" then
            -- a track saved on another server: the JSON of its "[coco race] track ..." line, in base64
            local raw = b64(args[2])
            local t = raw and util.decode(json, raw)
            if not valid(t) then return reskate.console.print("race import: not a track") end
            local ok, why = pcall(reskate.storage.set, route.track_key(t.map, t.id), t)
            if not ok then return reskate.console.print("race import: " .. tostring(why)) end
            if util.slug(t.map) == util.slug(map) then tracks[t.id] = t end
            return reskate.console.print("race import: " .. t.id .. " on " .. tostring(t.map))
        elseif verb == "chat" then
            -- a player's /race from the server's own chat commands: race chat <SteamID64> [words]
            local words, raw = {}, {}
            for i = 3, #args do
                raw[#raw + 1] = args[i]
                words[#words + 1] = string.lower(args[i])
            end
            return chat(args[2], words, raw)
        end
        reskate.console.print("race start [track] | race stop | race status | race records | race import <base64 json> | race chat <SteamID64> [words]")
    end, {
        {name = "start", args = "[track]", help = "start a race (a random track without one)"},
        {name = "stop", args = "", help = "stop the race"},
        {name = "status", args = "", help = "the race, the map's tracks and who is editing"},
        {name = "records", args = "", help = "the best time on each track here"},
        {name = "import", args = "<base64 json>", help = "a track saved on another server"},
        {name = "chat", args = "<SteamID64> [words]", help = "a player's /race"},
    })

-- ---- loading ----------------------------------------------------------------------------------------------
-- A race cut off by a reload or a stop: the locks still on record given back.
for id, perms in pairs(locked) do
    calls.later(1, function() give_back(id, perms) end)
end
load_tracks()
log("ready: " .. util.count(tracks) .. " track(s) on " .. (map ~= "" and map or "no map yet"))
