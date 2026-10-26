-- The HUD while on a COCOJAMBO server: the corner tag (role, points and rank, time played), the panel (F6: your
-- stats, the board, the server's links and rules) and the notes the server sends, at the top of the screen.
local util = require("lib/util")
local draw = require("client/draw")
local settings = require("client/settings")

local hud = {top_y = 24}   -- top_y: where the notes start (the race HUD moves them below its clock)
local now = reskate.time.now
local V = draw.V

local info = nil        -- the server: {name, links = [{label, url}], rules = {..}}
local me = nil          -- {stats, played, role, at}
local top = nil         -- {board, rows = [{n, p}]}
local toasts = {}       -- {text, kind, until_ms, at}
local panel_open, panel_at = false, 0

local KIND = {info = draw.teal, good = draw.green, warn = draw.sun, error = draw.red, race = draw.coral}

function hud.reset() info, me, top, toasts, panel_open = nil, nil, nil, {}, false end
function hud.server_name() return info and info.name end
function hud.toggle_panel()
    panel_open = not panel_open
    panel_at = now()
end

-- a note at the top of the screen (from the server, or from the race HUD)
function hud.toast(text, kind, secs)
    if not settings.toasts then return end
    if #toasts >= 3 then table.remove(toasts, 1) end
    toasts[#toasts + 1] = {text = util.one_line(text, 160), kind = kind or "info", until_ms = now() + (secs or 5) * 1000, at = now()}
end

function hud.message(m)
    if m.t == "hi" then
        info = type(m.info) == "table" and m.info or {}
    elseif m.t == "you" then
        me = {stats = type(m.stats) == "table" and m.stats or nil, played = tonumber(m.played) or 0, role = m.role, at = now()}
    elseif m.t == "played" and me then
        me.played, me.at = tonumber(m.played) or me.played, now()
    elseif m.t == "top" then
        top = {board = m.board, rows = type(m.rows) == "table" and m.rows or {}}
    elseif m.t == "toast" and type(m.text) == "string" then
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

-- ---- the corner tag -----------------------------------------------------------------------------------
local function corner(d)
    local s = me and me.stats
    local w, h = 320, (s and s.points) and 116 or 84
    local x, y = 24, 24
    if settings.side == "right" then x = draw.w - w - 24 end
    draw.card(d, x, y, w, h, nil, 31)
    draw.tag(d, x + 12, y - 12, "COCOJAMBO", draw.coral, 16, 32)
    local role = me and me.role
    if role then draw.text(d, x + w - 14, y - 6, util.one_line(role, 18):upper(), 14, draw.teal, "right", "bold") end
    draw.text(d, x + 16, y + 26, util.one_line(my_name(), 22), 22, draw.paper)
    draw.text(d, x + 16, y + 56, util.duration(played()) .. " played", 16, draw.dim, "left", "bold")
    if s and s.points then
        local line = util.thousands(s.points) .. " PTS"
        draw.text(d, x + 16, y + 80, line, 22, draw.sun, "left", "title")
        if s.rank then
            draw.text(d, x + w - 16, y + 80, "#" .. tostring(s.rank) .. (s.of and (" / " .. tostring(s.of)) or ""), 20, draw.paper, "right", "title")
        end
    end
end

-- ---- the panel (F6) ---------------------------------------------------------------------------------------
local function section(d, x, y, w, title)
    draw.tag(d, x, y, title, draw.teal, 16, #title)
    return y + 40
end
local function row(d, x, y, w, label, value, colour)
    draw.text(d, x, y, label, 17, draw.dim, "left", "bold")
    draw.text(d, x + w, y, value, 18, colour or draw.paper, "right")
    return y + 28
end

local function panel(d)
    local w = 520
    local x = 24 + (1 - draw.ease(now(), panel_at, 180)) * -60
    if settings.side == "right" then x = draw.w - w - 24 + (1 - draw.ease(now(), panel_at, 180)) * 60 end
    local y = settings.corner and 170 or 40
    local s = me and me.stats or {}
    -- how tall: counted first, so the card is drawn under what goes on it
    local lines = 4 + (s.team and 1 or 0) + (type(s.lines) == "table" and #s.lines or 0)
    local rows = top and math.min(#top.rows, 10) or 0
    local links = info and type(info.links) == "table" and math.min(#info.links, 6) or 0
    local rules = info and type(info.rules) == "table" and math.min(#info.rules, 6) or 0
    local h = 70 + lines * 28 + 40 + (rows > 0 and (rows * 28 + 52) or 0) + ((links + rules) > 0 and ((links + rules) * 28 + 52) or 0) + 30
    if y + h > draw.h - 20 then y = math.max(20, draw.h - 20 - h) end
    draw.card(d, x, y, w, h, "#15121ff0", 41)
    draw.tag(d, x + 16, y - 16, (info and info.name) and util.one_line(info.name, 30):upper() or "COCOJAMBO", draw.coral, 20, 42)
    draw.text(d, x + w - 18, y + 8, "F6 CLOSES", 13, draw.dim, "right", "bold")
    local ix, iw = x + 24, w - 48
    local cy = section(d, ix, y + 40, iw, "YOU")
    cy = row(d, ix, cy, iw, "Time played here", util.duration(played()))
    cy = row(d, ix, cy, iw, "Role", me and me.role or "-", draw.teal)
    cy = row(d, ix, cy, iw, "Points" .. (s.board and (" (" .. util.one_line(s.board, 20) .. ")") or ""),
             s.points and util.thousands(s.points) or "-", draw.sun)
    cy = row(d, ix, cy, iw, "Rank", s.rank and ("#" .. tostring(s.rank) .. (s.of and (" of " .. tostring(s.of)) or "")) or "-")
    if s.team then cy = row(d, ix, cy, iw, "Team", util.one_line(s.team, 28)) end
    if type(s.lines) == "table" then
        for _, l in ipairs(s.lines) do
            if type(l) == "table" then cy = row(d, ix, cy, iw, util.one_line(l.l or l.label, 30), util.one_line(l.v or l.value, 24)) end
        end
    end
    if rows > 0 then
        cy = section(d, ix, cy + 12, iw, "BOARD" .. (top.board and ("  " .. util.one_line(top.board, 24):upper()) or ""))
        local mine = my_name()
        for i = 1, rows do
            local r = top.rows[i]
            local colour = draw.medals[i] or draw.paper
            if r.n == mine then d:rect(V(ix - 8, cy - 2), V(ix + iw + 8, cy + 26), {colour = "#ffd23f26", fill = true}) end
            draw.text(d, ix, cy, tostring(i) .. ".", 18, colour, "left", "title")
            draw.text(d, ix + 40, cy, util.one_line(r.n, 24), 18, r.n == mine and draw.sun or draw.paper)
            draw.text(d, ix + iw, cy, util.thousands(r.p or 0), 18, colour, "right", "title")
            cy = cy + 28
        end
    end
    if links + rules > 0 then
        cy = section(d, ix, cy + 12, iw, "SERVER")
        for i = 1, rules do
            draw.text(d, ix, cy, util.one_line(info.rules[i], 60), 16, draw.paper, "left", "bold")
            cy = cy + 28
        end
        for i = 1, links do
            local l = info.links[i]
            if type(l) == "table" then cy = row(d, ix, cy, iw, util.one_line(l.label, 20), util.one_line(l.url, 40), draw.teal) end
        end
    end
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
        local w = draw.width(d, n.text, 22) + 56
        local x = draw.w / 2 - w / 2
        local yy = y - (1 - fade) * 30
        draw.card(d, x, yy, w, 50, "#15121fe8", 51)
        d:rect(V(x + 10, yy + 12), V(x + 16, yy + 38), {colour = KIND[n.kind] or draw.teal, fill = true})
        draw.text(d, x + 30, yy + 12, n.text, 22, draw.paper)
        y = y + 62
    end
end

function hud.draw(d)
    if not info then return end
    if settings.corner then corner(d) end
    if panel_open then panel(d) end
    if #toasts > 0 then notes(d) end
end

return hud
