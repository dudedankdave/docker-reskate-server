-- Server calls (say, tell, a player's role) are limited to about 20 a second for a
-- plugin, so they wait their turn here: at most 15 a second, which leaves room for the few made at once.
-- server.say has its own clock, one line a second, so its lines wait in a line of their own.
local calls = {}

local queue, said = {}, {}
local window, used, said_at = 0, 0, 0
local now = reskate.time.now

function calls.budget(cost)
    local t = now()
    if t - window >= 1000 then window, used = t, 0 end
    if used + cost > 15 then return false end
    used = used + cost
    return true
end

function calls.later(cost, fn) queue[#queue + 1] = {cost = cost, fn = fn} end
function calls.say(text) said[#said + 1] = text end
function calls.tell(id, text) calls.later(1, function() reskate.server.tell(id, text) end) end

function calls.players()
    local out = {}
    for _, p in ipairs(reskate.session.players()) do
        if not p.server then out[p.id] = p end
    end
    return out
end

-- A player's role ({name, rank, staff}), read at once: a server call, counted against the budget.
function calls.role(p)
    if not p then return nil end
    calls.budget(1)
    local ok, role = pcall(function() return p.role end)
    if ok then return role end
    return nil
end

reskate.on("tick", function()
    while queue[1] and calls.budget(queue[1].cost) do
        local ok, why = pcall(table.remove(queue, 1).fn)
        if not ok then reskate.log(tostring(why), "warn") end
    end
    if said[1] and now() - said_at >= 1050 and calls.budget(1) then
        local ok = pcall(reskate.server.say, table.remove(said, 1))
        said_at = now()
        if not ok then reskate.log("a chat line could not be said", "warn") end
    end
end)

return calls
