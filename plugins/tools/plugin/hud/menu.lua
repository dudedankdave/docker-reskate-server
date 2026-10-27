-- The COJA menu (F5): our own screen in the HUD's look, used with the mouse. Its tabs (the stats card, the races,
-- the challenges, Track Studio) are built every frame from widgets (card, info, note, separator, button, dropdown,
-- slider, toggle, custom), the same calls whether they are drawn or not:
--
--   drawn, in the draw handler: the widgets are laid out and drawn, and a click on one is kept as {id = value};
--   applied, on the next tick: the tab is built again without drawing, and that widget returns the value.
--
-- So what a click does (send a message, teleport, open a dialog) runs in a tick, not while drawing.
local theme = require("lib/theme")
local draw = require("hud/draw")

local menu = {open = false}
local V = draw.V
local now = reskate.time.now
local SCREEN = "coja"
local W, PAD = 760, 26

local tabs = {}           -- {name, build(ui), shown()}
local current = nil
local pending = nil       -- {tab, set = {id = value}}: a click, applied on the next tick
local report = function() end
local mouse = {x = -1, y = -1, down = false, clicked = false}
local opened_at, drawn_by_screen = 0, -1000

-- A tab: build(ui) is called with the widgets; shown() (optional) says whether the tab is there.
function menu.tab(name, build, shown) tabs[#tabs + 1] = {name = name, build = build, shown = shown} end
-- What the game made of the screen and the cursor goes to report(text) (the races' line in the server's log).
function menu.on_report(fn) report = fn end

local function visible_tabs()
    local list = {}
    for _, t in ipairs(tabs) do
        if not t.shown or t.shown() then list[#list + 1] = t end
    end
    return list
end

local function tab_named(name)
    for _, t in ipairs(visible_tabs()) do
        if t.name == name then return t end
    end
end

-- ---- the widgets, applied (no drawing): each returns what the pending click set, else what it was given ----

local applied = {}
applied.__index = applied
function applied:card() end
function applied:info() end
function applied:note() end
function applied:separator() end
function applied:custom() end
function applied:button(id) return self.set[id] == true end
function applied:dropdown(id, _, choices, chosen)
    local v = self.set[id]
    if v ~= nil then return math.clamp(math.floor(v), 1, math.max(#choices, 1)) end
    return chosen
end
function applied:slider(id, _, value, lo, hi)
    local v = self.set[id]
    if v ~= nil then return math.clamp(v, lo, hi) end
    return value
end
function applied:toggle(id, _, on)
    local v = self.set[id]
    if v ~= nil then return v end
    return on
end

-- Builds tab `name` with the widgets `ui`; false when there is no such tab (or it is not shown).
function menu.build(name, ui)
    local t = tab_named(name)
    if not t then return false end
    t.build(ui)
    return true
end
-- Runs tab `name` with `set` ({id = value}) as if those widgets had been clicked: how a click takes effect.
function menu.apply(name, set) menu.build(name, setmetatable({set = set or {}}, applied)) end
-- Shows tab `name` next time the menu is drawn.
function menu.show(name) current = name end

-- ---- the widgets, drawn ---------------------------------------------------------------------------------

local drawn = {}
drawn.__index = drawn

local function over(x, y, w, h) return mouse.x >= x and mouse.x <= x + w and mouse.y >= y and mouse.y <= y + h end

-- A click on a widget: kept for the next tick (only the first in a frame).
function drawn:click(id, value)
    if not self.clicked then
        self.clicked = true
        pending = {tab = self.tab, set = {[id] = value}}
        pcall(reskate.audio.play_ui, "select")
    end
end

-- Buttons sit side by side and wrap; any other widget starts a new line.
function drawn:line()
    if self.bx then
        self.y = self.y + 44
        self.bx = nil
    end
end

function drawn:card(title, subtitle)
    self:line()
    self.y = self.y + 6
    draw.text(self.d, self.x, self.y, string.upper(tostring(title)), 19, theme.ink, "left", "bold")
    self.y = self.y + 26
    if subtitle and subtitle ~= "" then
        draw.text(self.d, self.x, self.y, draw.fit(self.d, subtitle, 15, self.w), 15, theme.muted)
        self.y = self.y + 24
    end
    self.y = self.y + 6
end

function drawn:info(label, value)
    self:line()
    draw.text(self.d, self.x, self.y, draw.fit(self.d, label, 16, self.w * 0.45), 16, theme.muted)
    draw.text(self.d, self.x + self.w, self.y, draw.fit(self.d, value, 16, self.w * 0.55 - 10, "bold"), 16, theme.ink, "right", "bold")
    self.y = self.y + 28
end

function drawn:note(text)
    self:line()
    draw.text(self.d, self.x, self.y, draw.fit(self.d, text, 15, self.w), 15, theme.muted)
    self.y = self.y + 26
end

function drawn:separator()
    self:line()
    draw.rule(self.d, self.x - PAD + 8, self.y + 4, self.w + 2 * PAD - 16)
    self.y = self.y + 14
end

-- A box that lights up under the cursor; true when it was clicked.
local function box(self, x, y, w, h, label, accent)
    local hot = over(x, y, w, h)
    local d = self.d
    d:rect(V(x, y), V(x + w, y + h), {colour = theme.a(accent or theme.sky, hot and 0.22 or 0.08), fill = true, rounding = 6 * draw.u})
    d:rect(V(x, y), V(x + w, y + h), {colour = theme.a(accent or theme.sky, hot and 0.8 or 0.35), fill = false, thickness = 1, rounding = 6 * draw.u})
    draw.text(d, x + w / 2, y + (h - 16) / 2 - 1, label, 16, hot and theme.ink or theme.a(theme.ink, 0.9), "centre", "bold")
    return hot and mouse.clicked
end

function drawn:button(id, text)
    local w = draw.width(self.d, text, 16, "bold") + 32
    if not self.bx or self.bx + w > self.x + self.w then
        self:line()
        self.bx = self.x
    end
    if box(self, self.bx, self.y, w, 36, text) then self:click(id, true) end
    self.bx = self.bx + w + 10
    return false
end

-- A choice: its label on the left, < the chosen one > on the right.
function drawn:dropdown(id, text, choices, chosen)
    self:line()
    local n = math.max(#choices, 1)
    chosen = math.clamp(chosen or 1, 1, n)
    local right, bw = self.x + self.w, 36
    draw.text(self.d, self.x, self.y + 8, draw.fit(self.d, text, 16, self.w * 0.35), 16, theme.muted)
    if box(self, right - bw, self.y, bw, 34, ">") then self:click(id, chosen % n + 1) end
    local cw = self.w * 0.6 - 2 * bw - 16
    local cx = right - bw - 8 - cw
    self.d:rect(V(cx, self.y), V(cx + cw, self.y + 34), {colour = theme.a(theme.ink, 0.05), fill = true, rounding = 6 * draw.u})
    draw.text(self.d, cx + cw / 2, self.y + 8, draw.fit(self.d, choices[chosen] or "-", 16, cw - 16, "bold"), 16, theme.sun, "centre", "bold")
    if box(self, cx - 8 - bw, self.y, bw, 34, "<") then self:click(id, (chosen - 2) % n + 1) end
    self.y = self.y + 44
    return chosen
end

-- A number: its label, - the value + (steps of one).
function drawn:slider(id, text, value, lo, hi, format)
    self:line()
    local right, bw = self.x + self.w, 36
    draw.text(self.d, self.x, self.y + 8, draw.fit(self.d, text, 16, self.w * 0.5), 16, theme.muted)
    if box(self, right - bw, self.y, bw, 34, "+") then self:click(id, math.min(hi, value + 1)) end
    draw.text(self.d, right - bw - 40, self.y + 8, string.format(format or "%d", value), 16, theme.sun, "centre", "bold")
    if box(self, right - 2 * bw - 44, self.y, bw, 34, "-") then self:click(id, math.max(lo, value - 1)) end
    self.y = self.y + 44
    return value
end

function drawn:toggle(id, text, on)
    self:line()
    local s = 24
    if box(self, self.x, self.y, s, s, on and "x" or "", on and theme.mint or theme.sky) then self:click(id, not on) end
    draw.text(self.d, self.x + s + 12, self.y + 3, draw.fit(self.d, text, 16, self.w - s - 12), 16, theme.ink)
    self.y = self.y + 36
    return on
end

-- Something drawn by the tab itself: fn(d, x, y, w) draws it and returns its height (fn(nil, ...) only measures).
function drawn:custom(fn)
    self:line()
    local h = fn(self.d, self.x - PAD + 8, self.y, self.w + 2 * PAD - 16) or 0
    self.y = self.y + h + 10
end

-- ---- the screen ------------------------------------------------------------------------------------------

-- The cursor, in the 1080-line grid; clicked is true on the frame the left button goes down.
local cursor_told = false
local function read_mouse(d)
    local ok, c = pcall(reskate.input.cursor)
    if not cursor_told then
        cursor_told = true
        local ok_json, text = pcall(reskate.json.encode, c)
        report("input.cursor: " .. tostring(ok) .. " " .. (ok_json and text or tostring(c)))
    end
    mouse.clicked = false
    if not ok or type(c) ~= "table" then return end
    local x = tonumber(c.x or (type(c.position) == "table" and c.position.x) or c[1])
    local y = tonumber(c.y or (type(c.position) == "table" and c.position.y) or c[2])
    if not x or not y then return end
    local size = d:size()
    if x <= 1 and y <= 1 then x, y = x * size.x, y * size.y end   -- given as a share of the screen
    mouse.x, mouse.y = x / draw.u, y / draw.u
    local down = c.left == true or c.down == true or c.pressed == true
    mouse.clicked = down and not mouse.down
    mouse.down = down
end

local function render(d)
    draw.frame(d, 1)
    read_mouse(d)
    local list = visible_tabs()
    if #list == 0 then return end
    if not tab_named(current or "") then current = list[1].name end
    local x = draw.w / 2 - W / 2
    local y = 90
    local slide = (1 - draw.ease(now(), opened_at, 160)) * 30
    y = y + slide
    -- the tab first, measured: the card is drawn under it
    local body = setmetatable({d = d, tab = current, x = x + PAD, y = y + 118, w = W - 2 * PAD}, drawn)
    local h = draw.h - y - 40
    draw.card(d, x, y, W, h, 0.96)
    draw.logo(d, x + PAD, y + 22, 16)
    if box({d = d}, x + W - PAD - 34, y + 16, 34, 30, "X", theme.danger) then menu.close() end
    draw.text(d, x + W - PAD - 46, y + 24, "F5 / ESC CLOSE", 12, theme.muted, "right", "bold")
    -- the tabs
    local tx = x + PAD
    for _, t in ipairs(list) do
        local tw = draw.width(d, t.name, 15, "bold") + 30
        local active = t.name == current
        local hot = over(tx, y + 62, tw, 34)
        d:rect(V(tx, y + 62), V(tx + tw, y + 96), {colour = theme.a(active and theme.sun or theme.ink, active and 0.16 or (hot and 0.1 or 0.04)),
                                                     fill = true, rounding = 6 * draw.u})
        draw.text(d, tx + tw / 2, y + 71, t.name, 15, active and theme.sun or theme.ink, "centre", "bold")
        if hot and mouse.clicked and not active then
            current = t.name
            pcall(reskate.audio.play_ui, "tab")
        end
        tx = tx + tw + 8
    end
    draw.rule(d, x, y + 106, W)
    local ok, why = pcall(tab_named(current).build, body)
    if not ok then report("menu tab " .. tostring(current) .. ": " .. tostring(why)) end
end

-- The game's screen, when it opens one for us: a function it calls while the screen is open. If it hands us a
-- canvas the menu is drawn on it; else in the plugin's own draw handler.
local screen_told = false
local function on_screen(...)
    local first = ...
    if not screen_told then
        screen_told = true
        local ok, size = pcall(function() return first:size() end)
        report("ui.screen calls with " .. type(first) .. (ok and size and " (a canvas)" or "") .. ", " .. select("#", ...) .. " argument(s)")
    end
    local ok = pcall(function() return first:size() end)
    if ok and menu.open then
        drawn_by_screen = now()
        local fine, why = pcall(render, first)
        if not fine then report("menu draw: " .. tostring(why)) end
    end
end
local screen_ok, screen_why = pcall(reskate.ui.screen, SCREEN, on_screen)
pcall(reskate.on, "screen_closed", function(name)
    if name == nil or name == SCREEN then menu.open = false end
end)

function menu.close()
    if not menu.open then return end
    menu.open = false
    if screen_ok then pcall(reskate.ui.close, SCREEN) end
end

function menu.toggle()
    if menu.open then return menu.close() end
    menu.open, opened_at, mouse.down = true, now(), true    -- a button held while opening is no click
    if not screen_ok then
        report("ui.screen: " .. tostring(screen_why))
        return
    end
    local ok, opened, why = pcall(reskate.ui.open, SCREEN)
    if not ok or opened == false then report("ui.open: " .. tostring(ok and why or opened)) end
end

function menu.state() return {open = menu.open, tab = current} end

-- The menu's draw handler: added by menu.attach(), after the plugin's others, so it is drawn over them.
function menu.attach()
    reskate.on("draw", function(d)
        if not menu.open or now() - drawn_by_screen < 250 then return end
        local ok, why = pcall(render, d)
        if not ok then report("menu draw: " .. tostring(why)) end
    end)
end

reskate.on("tick", function()
    if pending then
        local p = pending
        pending = nil
        menu.apply(p.tab, p.set)
    end
    if menu.open then
        local ok, esc = pcall(reskate.input.key, 0x1B)
        if ok and esc == true then menu.close() end
    end
end)

return menu
