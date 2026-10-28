-- The COCOJAMBO hub (reskate.cocojambo.net, the public status site the dashboard serves from the leaderboard and
-- the teams): the player's line on the All Time board of both leagues, their crew, and the servers. The game makes
-- the requests (a plugin's server copy has no web requests); the hub answers by name, so a player beyond its top
-- 50 shows as unranked there. Fetched for the COJA menu's HOME tab, at most once a minute.
local hub = {}
local BASE = "https://reskate.cocojambo.net/status/api/"
local EVERY = 60000
local now = reskate.time.now

local data, error_text, fetched_at, waiting = nil, nil, -EVERY, 0
local report = function() end
function hub.on_report(fn) report = fn end

local function decode(body)
    local ok, value = pcall(reskate.json.decode, body or "")
    if ok and type(value) == "table" then return value end
end

-- The player's row on a board answer: {rank, points, wins, played, total}.
local function row_of(board, name)
    if type(board) ~= "table" or type(board.rows) ~= "table" then return nil end
    for _, r in ipairs(board.rows) do
        if type(r) == "table" and r.name == name then
            return {rank = r.rank, points = r.points, wins = r.wins, played = r.played, total = board.total}
        end
    end
    return {total = board.total}
end

local function crew_of(teams, name)
    if type(teams) ~= "table" or type(teams.teams) ~= "table" then return nil end
    for _, t in ipairs(teams.teams) do
        for _, m in ipairs(type(t.members) == "table" and t.members or {}) do
            if m.name == name then
                local points = type(t.points) == "table" and t.points or {}
                return {tag = t.tag, name = t.name, role = m.role, members = #t.members,
                        ranked = points.ranked or 0, unranked = points.unranked or 0}
            end
        end
    end
    return nil
end

local function servers_of(list)
    if type(list) ~= "table" or type(list.servers) ~= "table" then return nil end
    local online, players = 0, 0
    for _, s in ipairs(list.servers) do
        if s.online then
            online = online + 1
            players = players + (tonumber(s.players) or 0)
        end
    end
    return {online = online, total = #list.servers, players = players}
end

-- Asks the hub again when the last answer is a minute old: for `name` (the player's name as the boards show it).
function hub.refresh(name)
    if waiting > 0 or now() - fetched_at < EVERY or not name then return end
    fetched_at = now()
    local answers = {}
    local paths = {ranked = "board?league=ranked", unranked = "board?league=unranked", teams = "teams", servers = "servers"}
    for key, path in pairs(paths) do
        waiting = waiting + 1
        local ok, id, why = pcall(function(fn) return reskate.http.request({url = BASE .. path}, fn) end, function(status, body, err)
            waiting = waiting - 1
            if status == 200 then
                answers[key] = decode(body)
            else
                error_text = "the hub answered " .. tostring(status or err)
            end
            if waiting == 0 then
                data = {ranked = row_of(answers.ranked, name), unranked = row_of(answers.unranked, name),
                        crew = crew_of(answers.teams, name), servers = servers_of(answers.servers), at = now()}
                if answers.ranked then error_text = nil end
            end
        end)
        if not ok or id == nil then
            waiting = waiting - 1
            error_text = tostring(ok and why or id)
            report("hub: " .. error_text)
        end
    end
end

-- {ranked, unranked, crew, servers, at} once the hub answered, and the last error (nil when fine).
function hub.state() return data, error_text end

return hub
