-- The HUD's player copy, while on a COCOJAMBO server (one whose server copy answers): the corner tag (role, points
-- and rank, time played), the panel (F4: your stats, the board, the server's links and rules) and the notes the
-- server sends, at the top of the screen.
local util = require("lib/util")
local theme = require("lib/theme")
local draw = require("hud/draw")
local settings = require("hud/settings")

local hud = {top_y = 24}   -- top_y: where the notes start
local now = reskate.time.now
local V = draw.V

local info = nil        -- the server: {name, links = [{label, url}], rules = {..}}
local me = nil          -- {stats, played, role, at}
local top = nil         -- {board, rows = [{n, p}]}
local toasts = {}       -- {text, kind, until_ms, at}
local panel_open, panel_at = false, 0
local hidden = false    -- a menu or another screen is open: the HUD steps aside

local KIND = {info = theme.sky, good = theme.mint, warn = theme.sun, error = theme.danger, race = theme.rose}
local TOP_Y = 150       -- the chip and the panel start below the game's own notices in the top left

function hud.reset() info, me, top, toasts, panel_open = nil, nil, nil, {}, false end
-- What the HUD holds now (tests/test_tools_plugin.py reads it).
function hud.state() return {info = info, me = me, top = top, toasts = toasts, panel = panel_open, hidden = hidden} end
function hud.server_name() return info and info.name end
function hud.toggle_panel()
    panel_open = not panel_open
    panel_at = now()
end

-- a note at the top of the screen
function hud.toast(text, kind, secs)
    if not settings.toasts then return end
    if #toasts >= 3 then table.remove(toasts, 1) end
    toasts[#toasts + 1] = {text = util.one_line(text, 160), kind = kind or "info", until_ms = now() + (secs or 5) * 1000, at = now()}
end

function hud.message(m)
    if m.t == "hud_hi" then
        info = type(m.info) == "table" and m.info or {}
    elseif m.t == "hud_you" then
        me = {stats = type(m.stats) == "table" and m.stats or nil, played = tonumber(m.played) or 0, role = m.role, at = now()}
    elseif m.t == "hud_played" and me then
        me.played, me.at = tonumber(m.played) or me.played, now()
    elseif m.t == "hud_top" then
        top = {board = m.board, rows = type(m.rows) == "table" and m.rows or {}}
    elseif m.t == "hud_toast" and type(m.text) == "string" then
        hud.toast(m.text, m.kind, m.secs)
    end
end

local function played()
    if not me then return 0 end
    return me.played + (now() - me.at) / 1000
end
local function my_name()
    local ok, p = pcall(reskate.session.me)
    if ok and p then return p.name end
    return "you"
end

-- ---- the chip: small and faint, the logo and one line (points, rank, time played) ------------------------
local function chip(d)
    local s = me and me.stats
    local parts = {}
    if s and s.points then parts[#parts + 1] = util.thousands(s.points) .. " pts" end
    if s and s.rank then parts[#parts + 1] = "#" .. tostring(s.rank) end
    parts[#parts + 1] = util.duration(played())
    local line = table.concat(parts, "  |  ")
    local logo_h = 11
    local w = math.max(logo_h * draw.LOGO_RATIO, draw.width(d, line, 14, "bold")) + 20
    local h = 46
    local x = 16
    if settings.side == "right" then x = draw.w - w - 16 end
    draw.card(d, x, TOP_Y, w, h, 0.55, 6)
    draw.logo(d, x + 10, TOP_Y + 8, logo_h, 0.85)
    draw.text(d, x + 10, TOP_Y + 23, line, 14, theme.a(theme.ink, 0.8), "left", "bold")
end

-- ---- the panel (F4), on the other side from the chip -------------------------------------------------------
local function row(d, x, y, w, label, value, colour)
    draw.text(d, x, y, label, 16, theme.muted)
    draw.text(d, x + w, y, value, 16, colour or theme.ink, "right", "bold")
    return y + 26
end

-- The stats card: the F4 panel, and the STATS tab of the COJA menu (F5). Without d it only says how tall it is.
local function stats_card(d, x, y, w, corner)
    local s = me and me.stats or {}
    local extra = type(s.lines) == "table" and s.lines or {}
    local rows = top and math.min(#top.rows, 10) or 0
    local links = info and type(info.links) == "table" and math.min(#info.links, 6) or 0
    local rules = info and type(info.rules) == "table" and math.min(#info.rules, 6) or 0
    -- how tall: counted first, so the card is drawn under what goes on it
    local h = 64 + 30 + (4 + (s.team and 1 or 0) + #extra) * 26
    if rows > 0 then h = h + 24 + 30 + rows * 26 end
    if links + rules > 0 then h = h + 24 + 30 + (links + rules) * 26 end
    h = h + 14
    if not d then return h end
    draw.card(d, x, y, w, h, 0.94)
    local ix, iw = x + 22, w - 44
    draw.logo(d, ix, y + 18, 14)
    if corner then draw.text(d, x + w - 20, y + 18, corner, 12, theme.muted, "right", "bold") end
    draw.text(d, ix, y + 38, draw.fit(d, (info and info.name ~= "" and info.name) or "COCOJAMBO", 17, iw, "bold"), 17, theme.ink, "left", "bold")
    local cy = y + 64
    draw.rule(d, x, cy, w)
    draw.label(d, ix, cy + 12, "You")
    if me and me.role then draw.pill(d, x + w - 22 - draw.width(d, me.role, 12, "bold") - 30, cy + 8, me.role, theme.iris, 12) end
    cy = cy + 40
    cy = row(d, ix, cy, iw, "Points" .. (s.board and (", " .. util.one_line(s.board, 28)) or ""), s.points and util.thousands(s.points) or "-", theme.sun)
    cy = row(d, ix, cy, iw, "Rank", s.rank and ("#" .. tostring(s.rank) .. (s.of and (" of " .. tostring(s.of)) or "")) or "-")
    cy = row(d, ix, cy, iw, "Time played here", util.duration(played()))
    if s.team then cy = row(d, ix, cy, iw, "Team", util.one_line(s.team, 28)) end
    for _, l in ipairs(extra) do
        if type(l) == "table" then cy = row(d, ix, cy, iw, util.one_line(l.l or l.label, 30), util.one_line(l.v or l.value, 24)) end
    end
    cy = cy + 4
    if rows > 0 then
        cy = cy + 20
        draw.rule(d, x, cy - 10, w)
        draw.label(d, ix, cy, "Board" .. (top.board and ("  " .. util.one_line(top.board, 30)) or ""))
        cy = cy + 30
        local mine = my_name()
        for i = 1, rows do
            local r = top.rows[i]
            local colour = theme.places[i] or theme.ink
            if r.n == mine then d:rect(V(x + 8, cy - 3), V(x + w - 8, cy + 23), {colour = theme.a(theme.sun, 0.12), fill = true, rounding = 4 * draw.u}) end
            draw.text(d, ix, cy, tostring(i), 16, colour, "left", "bold")
            draw.text(d, ix + 30, cy, util.one_line(r.n, 26), 16, r.n == mine and theme.sun or theme.ink)
            draw.text(d, ix + iw, cy, util.thousands(r.p or 0), 16, colour, "right", "bold")
            cy = cy + 26
        end
    end
    if links + rules > 0 then
        cy = cy + 20
        draw.rule(d, x, cy - 10, w)
        draw.label(d, ix, cy, "Server")
        cy = cy + 30
        for i = 1, rules do
            draw.text(d, ix, cy, util.one_line(info.rules[i], 56), 16, theme.ink)
            cy = cy + 26
        end
        for i = 1, links do
            local l = info.links[i]
            if type(l) == "table" then cy = row(d, ix, cy, iw, util.one_line(l.label, 20), util.one_line(l.url, 40), theme.sky) end
        end
    end
    return h
end
function hud.stats_card(d, x, y, w) return stats_card(d, x, y, w) end
function hud.on_server() return info ~= nil end

local function panel(d)
    local w = 460
    local slide = (1 - draw.ease(now(), panel_at, 180)) * 40
    local x = draw.w - w - 24 + slide
    if settings.side == "right" then x = 24 - slide end
    local y = TOP_Y
    local h = stats_card(nil, x, y, w)
    if y + h > draw.h - 20 then y = math.max(20, draw.h - 20 - h) end
    stats_card(d, x, y, w, "F4 CLOSES")
end

-- ---- the notes at the top ----------------------------------------------------------------------------------
local function notes(d)
    local t = now()
    local y = hud.top_y
    local keep = {}
    for _, n in ipairs(toasts) do
        if t < n.until_ms then keep[#keep + 1] = n end
    end
    toasts = keep
    for _, n in ipairs(toasts) do
        local fade = math.min(draw.ease(t, n.at, 200), (n.until_ms - t) / 250)
        if fade > 1 then fade = 1 end
        local w = draw.width(d, n.text, 17, "bold") + 52
        local x = draw.w / 2 - w / 2
        local yy = y - (1 - fade) * 24
        draw.card(d, x, yy, w, 40, 0.92, 20)
        d:circle(V(x + 22, yy + 20), 4 * draw.u, {colour = KIND[n.kind] or theme.sky, fill = true})
        draw.text(d, x + 36, yy + 10, n.text, 17, theme.ink, "left", "bold")
        y = y + 50
    end
end

function hud.draw(d)
    if not info then return end
    draw.frame(d, settings.scale)
    if not hidden then
        if settings.corner then chip(d) end
        if panel_open then panel(d) end
    end
    if #toasts > 0 then notes(d) end
end

-- A menu (the pause menu, ReSkate's, Throwdowns ...) or another screen open: the chip and the panel step aside.
local function follow()
    local ok, game = pcall(reskate.menu.game)
    local ok2, focus = pcall(reskate.input.focus)
    if not (ok and ok2 and game and focus) then
        hidden = false
        return
    end
    hidden = game.open or not focus.gameplay or focus.interface_open or false
end
reskate.time.every(250, follow)

-- ---- the handshake: the client says hello until the server's copy answers; a server without it never does ---
local HELLO_EVERY = 10000
local VERSION = require("version")
local last_hello, where = -HELLO_EVERY, nil

local function hello()
    last_hello = now()
    pcall(reskate.net.send, "server", reskate.json.encode({t = "hud_hello", v = VERSION}))
end
local function on_server()
    local ok, active = pcall(reskate.session.active)
    if not ok or not active then return false end
    local ok2, dedicated = pcall(reskate.session.is_dedicated)
    return ok2 and dedicated
end

reskate.on("message", function(from, text)
    if not from.server then return end
    local m = util.decode(reskate.json, text)
    if not m or type(m.t) ~= "string" or m.t:sub(1, 4) ~= "hud_" then return end
    if m.t == "hud_ask" then return hello() end
    hud.message(m)
end)

reskate.on("tick", function()
    -- a new session or map: what the last server said no longer holds
    local ok, world = pcall(reskate.session.world)
    local here = (ok and world and (tostring(world.world) .. "/" .. tostring(world.map))) or nil
    if not on_server() then here = nil end
    if here ~= where then
        where = here
        hud.reset()
        last_hello = -HELLO_EVERY
    end
    if here and not info and now() - last_hello >= HELLO_EVERY then hello() end
end)

reskate.input.bind("hud_panel", "F4")
reskate.on("action", function(name)
    if name == "hud_panel" and info then hud.toggle_panel() end
end)
reskate.on("draw", function(d) hud.draw(d) end)

settings.status = function()
    if info then return util.one_line(info.name or "COCOJAMBO", 40) end
    return "Not on a COCOJAMBO server"
end

settings.dashboard = function()
    local stats = me and me.stats or {}
    local rank = stats.rank and ("#" .. tostring(stats.rank) .. (stats.of and (" / " .. tostring(stats.of)) or "")) or "—"
    return {
        server = settings.status(),
        player = me and my_name() or "Waiting for the server",
        points = stats.points and util.thousands(stats.points) or "—",
        rank = rank,
        played = me and util.duration(played()) or "—",
        board = top and util.one_line(top.board or "Current board", 32) or "Waiting for the server",
    }
end

return hud
