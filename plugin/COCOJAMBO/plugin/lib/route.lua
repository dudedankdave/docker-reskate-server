-- A race's course, without the game: tracks, the route a race runs (every lap's checkpoints in order),
-- passing a checkpoint between two poses, splits, standings and start spots. Positions are anything with
-- x, y and z (the game's vectors, or {x=, y=, z=} tables), so tests/test_plugin.py runs this with lupa.
local util = require("lib/util")

local route = {}

route.MIN_RADIUS, route.MAX_RADIUS = 2, 50
route.MAX_CHECKPOINTS = 40   -- a saved track is one console line of at most 2000 characters
route.MAX_LAPS = 10

function route.point(p) return {x = p.x, y = p.y, z = p.z} end
-- A stored point ({x, y, z} as JSON keeps it) back to one with x, y and z.
function route.vec(p)
    if p == nil then return nil end
    if p.x then return p end
    return {x = p[1], y = p[2], z = p[3]}
end
-- What a track keeps of a point: {x, y, z} to a tenth of a metre.
function route.pack(p)
    local function t(v) return math.floor(v * 10 + 0.5) / 10 end
    return {t(p.x), t(p.y), t(p.z)}
end

function route.distance(a, b)
    local dx, dy, dz = a.x - b.x, a.y - b.y, a.z - b.z
    return math.sqrt(dx * dx + dy * dy + dz * dz)
end
function route.flat(a, b)
    local dx, dz = a.x - b.x, a.z - b.z
    return math.sqrt(dx * dx + dz * dz)
end

-- Where on the segment a -> b it comes closest to c: (fraction 0..1, distance).
function route.closest(a, b, c)
    local dx, dy, dz = b.x - a.x, b.y - a.y, b.z - a.z
    local len2 = dx * dx + dy * dy + dz * dz
    local f = 0
    if len2 > 1e-9 then f = util.clamp(((c.x - a.x) * dx + (c.y - a.y) * dy + (c.z - a.z) * dz) / len2, 0, 1) end
    local p = {x = a.x + dx * f, y = a.y + dy * f, z = a.z + dz * f}
    return f, route.distance(p, c)
end

-- What is wrong with a track before it is saved or raced, or nil.
function route.problem(track)
    if type(track) ~= "table" then return "no track" end
    if not track.start then return "it has no start (key 4 in the editor, or /race start-here)" end
    local n = type(track.checkpoints) == "table" and #track.checkpoints or 0
    if n < 1 then return "it needs at least one checkpoint (the last one is the finish)" end
    if n > route.MAX_CHECKPOINTS then return "at most " .. route.MAX_CHECKPOINTS .. " checkpoints" end
    local laps = track.laps or 1
    if laps < 1 or laps > route.MAX_LAPS then return "1 to " .. route.MAX_LAPS .. " laps" end
    if laps > 1 and n < 2 then return "a track with laps needs at least two checkpoints" end
    return nil
end

-- The zones a race runs through, every lap's in order: {c, r, cp (its number on the track), lap (it ends a lap
-- that is not the last), finish (the last)}.
function route.build(track)
    local zones = {}
    local laps = track.laps or 1
    local n = #track.checkpoints
    for lap = 1, laps do
        for i, cp in ipairs(track.checkpoints) do
            zones[#zones + 1] = {c = route.vec(cp.c), r = util.clamp(cp.r or 8, route.MIN_RADIUS, route.MAX_RADIUS), cp = i,
                                 lap = (i == n and lap < laps), finish = (i == n and lap == laps)}
        end
    end
    return zones
end

-- The time limit of a track in seconds: the given one, else a guess from its length (12 m/s, at least 60 s).
function route.limit(track)
    if track.limit then return track.limit end
    local length, at = 0, route.vec(track.start)
    for _ = 1, track.laps or 1 do
        for _, cp in ipairs(track.checkpoints) do
            local c = route.vec(cp.c)
            length = length + route.distance(at, c)
            at = c
        end
    end
    return util.clamp(math.ceil(length / 12 * 2 + 30), 60, 1800)
end

-- The zones passed between two poses (p0 at t0 ms, p1 at t1 ms) by a racer who has `racer.next` to pass next:
-- {index, at_ms} for each, and racer.next moved on. Several can be passed in one step (fast, or a lag spike).
function route.advance(racer, zones, p0, t0, p1, t1)
    local passes = {}
    while zones[racer.next] do
        local z = zones[racer.next]
        local f, d = route.closest(p0, p1, z.c)
        if d > z.r then break end
        local at = t0 + (t1 - t0) * f
        passes[#passes + 1] = {index = racer.next, at_ms = at}
        racer.next = racer.next + 1
        -- the next zone is looked for from here on the segment
        p0 = {x = p0.x + (p1.x - p0.x) * f, y = p0.y + (p1.y - p0.y) * f, z = p0.z + (p1.z - p0.z) * f}
        t0 = at
    end
    return passes
end

-- The first through zone `index` sets its split; everyone after gets their gap to it (ms), the first nil.
function route.split(splits, index, at)
    if splits[index] == nil then
        splits[index] = at
        return nil
    end
    return math.max(0, at - splits[index])
end

-- The order of a race now: finished first (by time), then by zones passed, then by distance to the next.
-- racers: {id, name, next, ms, out}; where: id -> position (or nil).
function route.order(racers, zones, where)
    local list = {}
    for _, r in ipairs(racers) do
        if not r.out then
            local far = 1e9
            local z, at = zones[r.next], where[r.id]
            if z and at then far = route.distance(z.c, at) end
            list[#list + 1] = {r = r, far = far}
        end
    end
    table.sort(list, function(a, b)
        if a.r.ms or b.r.ms then
            if a.r.ms and b.r.ms then return a.r.ms < b.r.ms end
            return a.r.ms ~= nil
        end
        if a.r.next ~= b.r.next then return a.r.next > b.r.next end
        if a.far ~= b.far then return a.far < b.far end
        return a.r.seq < b.r.seq
    end)
    local out = {}
    for i, o in ipairs(list) do out[i] = o.r end
    return out
end

-- The standings a racer sees: the first `n`, and them below if they are further back.
-- order: from route.order; rows: {p, n (name), g (gap ms), t (finish ms), me}.
function route.board(order, me, n)
    local rows = {}
    local function row(i, r) return {p = i, n = util.one_line(r.name, 18), g = r.gap, t = r.ms, me = (r.id == me) or nil} end
    for i, r in ipairs(order) do
        if i <= n then rows[#rows + 1] = row(i, r)
        elseif r.id == me then rows[#rows + 1] = row(i, r) end
    end
    return rows
end

-- Start spots: the start, then 3 m to either side, rows of four going back 4 m along the heading.
function route.spots(start, heading, count)
    start = route.vec(start)
    heading = heading or 0
    local fx, fz = math.sin(heading), math.cos(heading)
    local rx, rz = fz, -fx
    local spots = {}
    local sides = {0, 1, -1, 2}
    for i = 1, count do
        local row = (i - 1) // 4
        local side = sides[(i - 1) % 4 + 1] * 3
        spots[i] = {x = start.x + rx * side - fx * row * 4, y = start.y, z = start.z + rz * side - fz * row * 4}
    end
    return spots
end

-- A record's storage key: per map, track and laps, so a changed track starts a new record list.
function route.record_key(map, track)
    return "best." .. util.slug(map or "map") .. "." .. track.id .. "." .. (track.laps or 1) .. "." .. #track.checkpoints
end

-- A track's storage key, per map.
function route.track_key(map, id) return "track." .. (util.slug(map or "map") or "map") .. "." .. id end

return route
