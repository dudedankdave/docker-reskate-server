-- The HUD's server copy: who has the HUD, what each player's HUD shows and the notes it pops up. Its messages
-- all have a "t" starting "hud_", so the races' messages on the same channel never meet them.
--
-- The server's own tools (the leaderboard, teams, the Discord bot) know a player's points and team; they hand
-- them in on the console, as base64 JSON because a console line splits on spaces:
--   coco player <SteamID64> <base64 json>   a player's stats for their HUD (plugins/tools/README.md)
--   coco top <base64 json>                  the board on the panel (F4): {board, rows = [{n, p}]}
--   coco info <base64 json>                 the server's name, links and rules on the panel; kept
--   coco toast <SteamID64|all> <base64 json> a note at the top of the screen: {text, kind, secs}
--   coco status                             who has the HUD
-- Time played is counted here, per server (storage "hud:t:<SteamID64>", seconds).
local util = require("lib/util")
local decode64 = require("lib/base64")
local calls = require("hud/calls")

local core = {}
local VERSION = require("version")
local now = reskate.time.now
local json = reskate.json

local hud = {}          -- SteamID64 -> version of the client copy that said hello
local stats = {}        -- SteamID64 -> the stats the tools sent (a table)
local roles = {}        -- SteamID64 -> {name, rank, staff}
local joined = {}       -- SteamID64 -> ms they joined at (for the time played this visit)
local info = reskate.storage.get("hud:info") or {}
local top = reskate.storage.get("hud:top")
local SAVE_EVERY = 60000

local function send(to, message)
    local ok, sent = pcall(reskate.net.send, to, json.encode(message))
    return ok and sent
end
core.send = send

function core.has_hud(id) return hud[id] ~= nil end

local function played(id)
    local total = reskate.storage.get("hud:t:" .. id) or 0
    if joined[id] then total = total + (now() - joined[id]) / 1000 end
    return math.floor(total)
end
-- The time played so far goes into storage, and the visit counts on from now. (Storage holds 1 MB: full,
-- the time is logged and the HUD keeps counting this visit.)
local function bank(id)
    if not joined[id] then return end
    local ok, why = pcall(reskate.storage.set, "hud:t:" .. id, played(id))
    if not ok then return reskate.log("time played not saved: " .. tostring(why), "warn") end
    joined[id] = now()
end

local function you(id)
    local role = roles[id]
    return {t = "hud_you", stats = stats[id], played = played(id), role = role and role.name or nil}
end

local function welcome(id)
    send(id, {t = "hud_hi", v = VERSION, info = info})
    send(id, you(id))
    if top then send(id, {t = "hud_top", board = top.board, rows = top.rows}) end
end

reskate.on("message", function(from, text)
    if from.server then return end
    local m = util.decode(json, text)
    if not m or m.t ~= "hud_hello" then return end
    local first = hud[from.id] == nil
    hud[from.id] = tostring(m.v or "?")
    if not joined[from.id] then joined[from.id] = now() end
    if first and not roles[from.id] then
        calls.later(1, function()
            roles[from.id] = calls.role(calls.players()[from.id])
            send(from.id, you(from.id))
        end)
    end
    welcome(from.id)
end)

reskate.on("player_joined", function(p)
    joined[p.id] = now()
    -- ask: the game loads its plugins first; a client that is up already answers at once
    reskate.time.after(4000, function() send(p.id, {t = "hud_ask"}) end)
end)

reskate.on("player_left", function(p)
    bank(p.id)
    reskate.console.print("[coco] left " .. p.id .. " played " .. played(p.id))
    hud[p.id], stats[p.id], roles[p.id], joined[p.id] = nil, nil, nil, nil
end)

reskate.time.every(SAVE_EVERY, function()
    for id in pairs(joined) do bank(id) end
end)
-- Everyone's time played on their HUD now and then (the HUD counts on by itself between).
reskate.time.every(SAVE_EVERY * 5, function()
    for id in pairs(hud) do send(id, {t = "hud_played", played = played(id)}) end
end)

-- ---- the console: what the server's tools hand in ---------------------------------------------------
local function payload(text, what)
    local raw = decode64(text)
    local value = raw and util.decode(json, raw)
    if not value then reskate.console.print("error: coco " .. what .. ": not base64 JSON of an object") end
    return value
end

local function present(id)
    return calls.players()[id] ~= nil
end

local commands = {}
function commands.player(args)
    local id = args[2]
    if not (id and id:match(util.STEAMID)) then return reskate.console.print("error: coco player <SteamID64> <base64 json>") end
    local value = payload(args[3], "player")
    if not value or not present(id) then return end
    stats[id] = value
    send(id, you(id))
end
function commands.top(args)
    local value = payload(args[2], "top")
    if not value then return end
    top = {board = value.board, rows = value.rows}
    reskate.storage.set("hud:top", top)
    send("all", {t = "hud_top", board = top.board, rows = top.rows})
end
function commands.info(args)
    local value = payload(args[2], "info")
    if not value then return end
    info = value
    reskate.storage.set("hud:info", info)
    send("all", {t = "hud_hi", v = VERSION, info = info})
end
function commands.toast(args)
    local to = args[2]
    if not (to == "all" or (to and to:match(util.STEAMID))) then
        return reskate.console.print("error: coco toast <SteamID64|all> <base64 json>")
    end
    local value = payload(args[3], "toast")
    if not value or type(value.text) ~= "string" then return end
    core.toast(to, value.text, value.kind, value.secs)
end
function commands.status()
    local n, list = 0, {}
    for id, v in pairs(hud) do
        n = n + 1
        list[#list + 1] = id .. " v" .. v
    end
    reskate.console.print("coco " .. VERSION .. ": " .. n .. " player(s) with the HUD" .. (n > 0 and (": " .. table.concat(list, ", ")) or ""))
end

-- A note at the top of a player's screen (or everyone's); players without the HUD get it in chat.
function core.toast(to, text, kind, secs)
    text = util.one_line(text, 160)
    local message = {t = "hud_toast", text = text, kind = kind or "info", secs = util.clamp(tonumber(secs) or 5, 2, 15)}
    if to == "all" then
        send("all", message)
        for id in pairs(calls.players()) do
            if not hud[id] then calls.tell(id, text) end
        end
    elseif hud[to] then
        send(to, message)
    elseif present(to) then
        calls.tell(to, text)
    end
end

reskate.console.command("coco", "COJA tools: the HUD's stats, board, server info and notes; the server's tools run it", function(args)
    local fn = commands[args[1] or ""]
    if fn then return fn(args) end
    reskate.console.print("coco player <SteamID64> <base64 json> | coco top <base64 json> | coco info <base64 json> | "
        .. "coco toast <SteamID64|all> <base64 json> | coco status")
end, {
    {name = "player", args = "<SteamID64> <base64 json>", help = "a player's stats for their HUD"},
    {name = "top", args = "<base64 json>", help = "the board on the panel: {board, rows = [{n, p}]}"},
    {name = "info", args = "<base64 json>", help = "the server's name, links and rules on the panel"},
    {name = "toast", args = "<SteamID64|all> <base64 json>", help = "a note at the top of the screen: {text, kind, secs}"},
    {name = "status", args = "", help = "who has the HUD"},
})

-- A reload of the plugin mid-session: the players already here count from now, and their HUDs start over.
for id in pairs(calls.players()) do joined[id] = now() end
reskate.time.after(0, function() send("all", {t = "hud_ask"}) end)

return core
