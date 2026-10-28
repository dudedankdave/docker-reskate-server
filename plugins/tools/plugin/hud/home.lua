-- The COJA menu's HOME tab: the player's profile at a glance, in the COCOJAMBO look. Their stats on this server
-- come from the server's tools (the HUD's `coco player`); their line on both leagues' All Time boards, their crew
-- and the servers from the hub (hud/hub.lua).
local theme = require("lib/theme")
local draw = require("hud/draw")
local util = require("lib/util")

local home = {}
local V = draw.V

local function place(row)
    if not row or not row.rank then return "unranked" end
    return "#" .. tostring(row.rank) .. (row.total and (" of " .. tostring(row.total)) or "")
end

-- A stat tile: a small grey label over a big value.
local function tile(d, x, y, w, label, value, colour)
    d:rect(V(x, y), V(x + w, y + 74), {colour = theme.a(theme.ink, 0.04), fill = true, rounding = 8 * draw.u})
    d:rect(V(x, y), V(x + w, y + 74), {colour = theme.line_strong, fill = false, thickness = 1, rounding = 8 * draw.u})
    draw.label(d, x + 14, y + 12, label)
    draw.text(d, x + 14, y + 34, draw.fit(d, value, 24, w - 28, "bold"), 24, colour or theme.ink, "left", "bold")
end

-- A league's line from the hub: rank, points, wins of played.
local function league(d, x, y, w, name, row, colour)
    draw.label(d, x, y, name)
    local text
    if row and row.rank then
        text = string.format("%s  ·  %s pts  ·  %s wins of %s", place(row), util.thousands(row.points or 0),
            tostring(row.wins or 0), tostring(row.played or 0))
    else
        text = "not on the board yet"
    end
    draw.text(d, x + w, y - 2, draw.fit(d, text, 16, w * 0.7, "bold"), 16, row and row.rank and colour or theme.muted, "right", "bold")
end

-- Draws the tab at x, y, w (the menu's custom widget) and returns its height. hud: hud/client.lua; hub: hud/hub.lua.
function home.draw(d, x, y, w, hud, hub)
    local s = hud.stats_lines()
    local data, problem = hub.state()
    local ix, iw = x + 22, w - 44
    local top = y
    -- the logo, the player and their crew
    draw.logo(d, ix, y + 8, 30)
    y = y + 54
    draw.text(d, ix, y, draw.fit(d, hud.my_name(), 30, iw - 160, "bold"), 30, theme.ink, "left", "bold")
    local crew = data and data.crew
    local pills = {}
    if s.role then pills[#pills + 1] = {s.role, theme.iris} end
    if crew then pills[#pills + 1] = {"[" .. tostring(crew.tag) .. "] " .. tostring(crew.role or ""), theme.sun} end
    local px = ix + iw
    for i = #pills, 1, -1 do
        local width = draw.width(d, pills[i][1], 13, "bold") + 30
        px = px - width
        draw.pill(d, px, y + 6, pills[i][1], pills[i][2], 13)
        px = px - 8
    end
    y = y + 42
    draw.text(d, ix, y, draw.fit(d, s.name, 15, iw), 15, theme.muted)
    y = y + 30
    -- this server: the HUD's numbers
    local rows = {}
    for _, r in ipairs(s.rows) do rows[r[1]:match("^[^,]+")] = r end
    local tw = (iw - 3 * 12) / 4
    local points = rows.Points
    tile(d, ix, y, tw, "Points", points and points[2] or "-", theme.sun)
    tile(d, ix + (tw + 12), y, tw, "Rank", rows.Rank and rows.Rank[2] or "-")
    tile(d, ix + 2 * (tw + 12), y, tw, "Played here", rows["Time played here"] and rows["Time played here"][2] or "-")
    local week = nil
    for _, r in ipairs(s.rows) do
        if r[1]:match("^Week") then week = r end
    end
    tile(d, ix + 3 * (tw + 12), y, tw, week and week[1] or "This week", week and week[2] or "-", theme.mint)
    y = y + 94
    -- the hub
    draw.rule(d, x, y, w)
    y = y + 16
    draw.text(d, ix, y, "ON THE HUB", 15, theme.sky, "left", "bold")
    draw.text(d, ix + iw, y, "reskate.cocojambo.net", 13, theme.muted, "right", "bold")
    y = y + 32
    if data then
        league(d, ix, y, iw, "Ranked", data.ranked, theme.sun)
        y = y + 30
        league(d, ix, y, iw, "Unranked", data.unranked, theme.mint)
        y = y + 30
        draw.label(d, ix, y, "Crew")
        local crew_text = crew and string.format("[%s] %s  ·  %d members  ·  %s pts", tostring(crew.tag), tostring(crew.name),
            crew.members, util.thousands((crew.ranked or 0) + (crew.unranked or 0))) or "no crew yet"
        draw.text(d, ix + iw, y - 2, draw.fit(d, crew_text, 16, iw * 0.7, "bold"), 16, crew and theme.ink or theme.muted, "right", "bold")
        y = y + 30
        if data.servers then
            draw.label(d, ix, y, "Servers")
            draw.text(d, ix + iw, y - 2, string.format("%d of %d online  ·  %d skating", data.servers.online, data.servers.total,
                data.servers.players), 16, theme.ink, "right", "bold")
            y = y + 30
        end
    else
        draw.text(d, ix, y, problem and ("The hub is not reachable: " .. problem) or "Asking the hub...", 15, theme.muted)
        y = y + 30
    end
    -- the server's own lines (rules, links) as on the stats card
    if #s.rules + #s.links > 0 then
        draw.rule(d, x, y, w)
        y = y + 16
        draw.label(d, ix, y, "Server")
        y = y + 28
        for _, rule in ipairs(s.rules) do
            draw.text(d, ix, y, draw.fit(d, rule, 16, iw), 16, theme.ink)
            y = y + 26
        end
        for _, l in ipairs(s.links) do
            draw.text(d, ix, y, l[1], 16, theme.muted)
            draw.text(d, ix + iw, y, draw.fit(d, l[2], 16, iw * 0.7), 16, theme.sky, "right")
            y = y + 26
        end
    end
    return y - top + 10
end

-- The same as widget lines, where the menu is built without drawing (the tests' widgets).
function home.lines(ui, hud, hub)
    local s = hud.stats_lines()
    local data = hub.state()
    ui:card(hud.my_name(), s.name)
    for _, r in ipairs(s.rows) do ui:info(r[1], r[2]) end
    if data then
        ui:info("Ranked", place(data.ranked))
        ui:info("Unranked", place(data.unranked))
        ui:info("Crew", data.crew and ("[" .. tostring(data.crew.tag) .. "] " .. tostring(data.crew.name)) or "no crew yet")
    end
end

return home
