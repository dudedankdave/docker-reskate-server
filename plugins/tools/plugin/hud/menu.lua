-- The COJA menu (F5): our own panel in the HUD's look, drawn over the game like the F4 card (no game screen, which
-- would cover it). Used with the keyboard or a controller, and with the mouse where the game lends us a cursor:
--
--   keyboard    arrows move (left/right on the tab row switch tabs, on a choice change it), Enter or Space presses,
--               Tab switches tabs, Esc or F5 closes
--   controller  d-pad moves, A presses, LB/RB switch tabs, B closes
--   mouse       hover and click, where reskate.input.cursor (or .mouse) gives a position
--
-- Its tabs (the stats card, the races, the challenges, Track Studio) are built every frame from widgets (card,
-- info, note, separator, button, dropdown, slider, toggle, custom), the same calls whether drawn or not:
--   drawn, in the draw handler: laid out and drawn; a click or a key on one is kept as {id = value};
--   applied, on the next tick: the tab is built again without drawing, and that widget returns the value.
-- So what a press does (send a message, teleport, open a dialog) runs in a tick, not while drawing.
local theme = require("lib/theme")
local draw = require("hud/draw")

local menu = {open = false}
local V = draw.V
local now = reskate.time.now
local W, PAD = 760, 26

local tabs = {}           -- {name, build(ui), shown()}
local current = nil
local pending = nil       -- {tab, set = {id = value}}: a press, applied on the next tick
local report = function() end
local mouse = {x = -1, y = -1, down = false, clicked = false, seen = false}
local focus, focusables = 0, 0   -- 0: the tab row; 1..n: the tab's widgets that take a press, in order
local nav = nil                  -- "left", "right" or "press": for the focused widget, in the next frame
local opened_at = 0

-- A tab: build(ui) is called with the widgets; shown() (optional) says whether the tab is there.
function menu.tab(name, build, shown) tabs[#tabs + 1] = {name = name, build = build, shown = shown} end
-- What the game made of the cursor, the controller and keeping input from the skater goes to report(text) (the
-- races' line in the server's log).
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

local function step_tab(by)
    local list = visible_tabs()
    if #list == 0 then return end
    local at = 1
    for i, t in ipairs(list) do
        if t.name == current then at = i end
    end
    current = list[(at - 1 + by) % #list + 1].name
    focus = 0
    pcall(reskate.audio.play_ui, "tab")
end

-- ---- the widgets, applied (no drawing): each returns what the pending press set, else what it was given ----

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
-- Runs tab `name` with `set` ({id = value}) as if those widgets had been pressed: how a press takes effect.
function menu.apply(name, set) menu.build(name, setmetatable({set = set or {}}, applied)) end
-- Shows tab `name` next time the menu is drawn.
function menu.show(name)
    current = name
    focus = 0
end

-- ---- the widgets, drawn ---------------------------------------------------------------------------------

local drawn = {}
drawn.__index = drawn

local function over(x, y, w, h) return mouse.seen and mouse.x >= x and mouse.x <= x + w and mouse.y >= y and mouse.y <= y + h end

-- A press on a widget: kept for the next tick (only the first in a frame).
function drawn:press(id, value)
    if not self.pressed then
        self.pressed = true
        pending = {tab = self.tab, set = {[id] = value}}
        pcall(reskate.audio.play_ui, "select")
    end
end

-- The next widget that takes a press: whether it has the focus, and what the keys did to it.
function drawn:next_focus()
    self.n = self.n + 1
    if self.n ~= focus then return false, nil end
    local act = nav
    nav = nil
    return true, act
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

-- A box that lights up under the cursor or with the focus; true when the mouse clicked it.
local function box(d, x, y, w, h, label, accent, focused)
    local hot = over(x, y, w, h)
    local lit = hot or focused
    accent = accent or theme.sky
    d:rect(V(x, y), V(x + w, y + h), {colour = theme.a(accent, lit and 0.22 or 0.08), fill = true, rounding = 6 * draw.u})
    d:rect(V(x, y), V(x + w, y + h), {colour = focused and theme.sun or theme.a(accent, lit and 0.8 or 0.35), fill = false,
                                     thickness = focused and 2 or 1, rounding = 6 * draw.u})
    draw.text(d, x + w / 2, y + (h - 16) / 2 - 1, label, 16, lit and theme.ink or theme.a(theme.ink, 0.9), "centre", "bold")
    return hot and mouse.clicked
end

function drawn:button(id, text)
    local w = draw.width(self.d, text, 16, "bold") + 32
    if not self.bx or self.bx + w > self.x + self.w then
        self:line()
        self.bx = self.x
    end
    local focused, act = self:next_focus()
    if box(self.d, self.bx, self.y, w, 36, text, nil, focused) or act == "press" then self:press(id, true) end
    self.bx = self.bx + w + 10
    return false
end

-- A choice: its label on the left, < the chosen one > on the right.
function drawn:dropdown(id, text, choices, chosen)
    self:line()
    local n = math.max(#choices, 1)
    chosen = math.clamp(chosen or 1, 1, n)
    local focused, act = self:next_focus()
    local right, bw = self.x + self.w, 36
    draw.text(self.d, self.x, self.y + 8, draw.fit(self.d, text, 16, self.w * 0.35), 16, focused and theme.sun or theme.muted)
    if box(self.d, right - bw, self.y, bw, 34, ">", nil, false) or act == "right" or act == "press" then self:press(id, chosen % n + 1) end
    local cw = self.w * 0.6 - 2 * bw - 16
    local cx = right - bw - 8 - cw
    self.d:rect(V(cx, self.y), V(cx + cw, self.y + 34), {colour = theme.a(theme.ink, focused and 0.1 or 0.05), fill = true, rounding = 6 * draw.u})
    if focused then
        self.d:rect(V(cx, self.y), V(cx + cw, self.y + 34), {colour = theme.sun, fill = false, thickness = 2, rounding = 6 * draw.u})
    end
    draw.text(self.d, cx + cw / 2, self.y + 8, draw.fit(self.d, choices[chosen] or "-", 16, cw - 16, "bold"), 16, theme.sun, "centre", "bold")
    if box(self.d, cx - 8 - bw, self.y, bw, 34, "<", nil, false) or act == "left" then self:press(id, (chosen - 2) % n + 1) end
    self.y = self.y + 44
    return chosen
end

-- A number: its label, - the value + (steps of one).
function drawn:slider(id, text, value, lo, hi, format)
    self:line()
    local focused, act = self:next_focus()
    local right, bw = self.x + self.w, 36
    draw.text(self.d, self.x, self.y + 8, draw.fit(self.d, text, 16, self.w * 0.5), 16, focused and theme.sun or theme.muted)
    if box(self.d, right - bw, self.y, bw, 34, "+", nil, false) or act == "right" then self:press(id, math.min(hi, value + 1)) end
    draw.text(self.d, right - bw - 40, self.y + 8, string.format(format or "%d", value), 16, theme.sun, "centre", "bold")
    if box(self.d, right - 2 * bw - 44, self.y, bw, 34, "-", nil, false) or act == "left" then self:press(id, math.max(lo, value - 1)) end
    if focused then
        self.d:rect(V(right - 2 * bw - 50, self.y - 4), V(right + 6, self.y + 38), {colour = theme.sun, fill = false, thickness = 2, rounding = 8 * draw.u})
    end
    self.y = self.y + 44
    return value
end

function drawn:toggle(id, text, on)
    self:line()
    local focused, act = self:next_focus()
    local s = 24
    if box(self.d, self.x, self.y, s, s, on and "x" or "", on and theme.mint or theme.sky, focused) or act == "press" then self:press(id, not on) end
    draw.text(self.d, self.x + s + 12, self.y + 3, draw.fit(self.d, text, 16, self.w - s - 12), 16, focused and theme.sun or theme.ink)
    self.y = self.y + 36
    return on
end

-- Something drawn by the tab itself: fn(d, x, y, w) draws it and returns its height.
function drawn:custom(fn)
    self:line()
    local h = fn(self.d, self.x - PAD + 8, self.y, self.w + 2 * PAD - 16) or 0
    self.y = self.y + h + 10
end

-- ---- input: the mouse where the game gives one, the keyboard and the controller ----------------------------

local told = {}
local function tell(key, text)
    if told[key] then return end
    told[key] = true
    report(text)
end
local function json(value)
    local ok, text = pcall(reskate.json.encode, value)
    return ok and text or tostring(value)
end

-- The cursor, in the 1080-line grid; clicked is true on the frame the left button goes down.
local function read_mouse(d)
    mouse.clicked = false
    local ok, c = pcall(reskate.input.cursor)
    if not ok or type(c) ~= "table" then ok, c = pcall(reskate.input.mouse) end
    tell("cursor", "cursor: " .. tostring(ok) .. " " .. json(c))
    if not ok or type(c) ~= "table" then
        mouse.seen = false
        return
    end
    local x = tonumber(c.x or (type(c.position) == "table" and c.position.x) or c[1])
    local y = tonumber(c.y or (type(c.position) == "table" and c.position.y) or c[2])
    if not x or not y or c.over_screen == false then
        mouse.seen = false
        return
    end
    local size = d:size()
    if x <= 1 and y <= 1 then x, y = x * size.x, y * size.y end   -- given as a share of the screen
    mouse.x, mouse.y, mouse.seen = x / draw.u, y / draw.u, true
    local down = c.left == true or c.down == true or c.pressed == true
    mouse.clicked = down and not mouse.down
    mouse.down = down
end

-- Keys (Windows virtual-key codes) and the controller's buttons (XInput bits from reskate.input.pad_bits).
local KEYS = {up = 0x26, down = 0x28, left = 0x25, right = 0x27, press = 0x0D, space = 0x20, tab = 0x09, close = 0x1B}
local PAD_BITS = {up = 0x0001, down = 0x0002, left = 0x0004, right = 0x0008, prev_tab = 0x0100, next_tab = 0x0200,
                  press = 0x1000, close = 0x2000}
local was = {}

local function held()
    local now_down = {}
    for name, code in pairs(KEYS) do
        local ok, down = pcall(reskate.input.key, code)
        if ok and down == true then now_down[name == "space" and "press" or name] = true end
    end
    local ok, bits = pcall(reskate.input.pad_bits)
    if ok and type(bits) ~= "number" then
        local ok_pad, pad = pcall(reskate.input.gamepad)
        bits = ok_pad and type(pad) == "table" and tonumber(pad.buttons or pad.bits) or nil
        tell("pad", "pad_bits: " .. json(bits) .. ", gamepad: " .. json(pad))
    end
    if type(bits) == "number" then
        for name, bit in pairs(PAD_BITS) do
            if (bits // bit) % 2 == 1 then now_down[name] = true end
        end
    end
    return now_down
end

local function navigate()
    local down = held()
    local pressed = {}
    for name in pairs(down) do
        if not was[name] then pressed[name] = true end
    end
    was = down
    if pressed.close then return menu.close() end
    if pressed.tab or pressed.next_tab then step_tab(1) end
    if pressed.prev_tab then step_tab(-1) end
    if pressed.up then
        focus = math.max(0, focus - 1)
        pcall(reskate.audio.play_ui, "navigate")
    end
    if pressed.down then
        focus = math.min(focusables, focus + 1)
        pcall(reskate.audio.play_ui, "navigate")
    end
    if focus == 0 then
        if pressed.left then step_tab(-1) end
        if pressed.right then step_tab(1) end
        if pressed.press then focus = math.min(focusables, 1) end
    elseif pressed.left then nav = "left"
    elseif pressed.right then nav = "right"
    elseif pressed.press then nav = "press" end
end

-- While open, the menu's keys and buttons are kept from the skater, and the game is asked for a cursor.
local function hold_input(on)
    local results = {}
    local function try(name, ...)
        local fn = reskate.input[name]
        local ok, a, b = pcall(fn, ...)
        results[#results + 1] = name .. "=" .. tostring(ok) .. (ok and "" or (":" .. tostring(a))) ..
                                ((ok and (a ~= nil or b ~= nil)) and (" " .. tostring(a) .. " " .. tostring(b)) or "")
    end
    try("capture", on)
    if on then
        try("game_keys", {0x26, 0x28, 0x25, 0x27, 0x0D, 0x20, 0x09, 0x1B})
        try("game_buttons", 0x0001 + 0x0002 + 0x0004 + 0x0008 + 0x0100 + 0x0200 + 0x1000 + 0x2000)
    else
        try("game_keys", {})
        try("game_buttons", 0)
    end
    tell("hold " .. tostring(on), "hold input " .. tostring(on) .. ": " .. table.concat(results, ", "))
end

-- ---- the panel ---------------------------------------------------------------------------------------------

local function render(d)
    draw.frame(d, 1)
    read_mouse(d)
    local list = visible_tabs()
    if #list == 0 then return end
    if not tab_named(current or "") then current = list[1].name end
    local x = draw.w / 2 - W / 2
    local y = 90 + (1 - draw.ease(now(), opened_at, 160)) * 30
    draw.card(d, x, y, W, draw.h - y - 40, 0.96)
    draw.logo(d, x + PAD, y + 22, 16)
    if box(d, x + W - PAD - 34, y + 16, 34, 30, "X", theme.danger, false) then menu.close() end
    draw.text(d, x + W - PAD - 46, y + 18, "F5 / ESC CLOSES", 12, theme.muted, "right", "bold")
    draw.text(d, x + W - PAD - 46, y + 34, "ARROWS · ENTER  |  D-PAD · A · LB/RB", 12, theme.muted, "right", "bold")
    -- the tab row
    local tx = x + PAD
    for _, t in ipairs(list) do
        local tw = draw.width(d, t.name, 15, "bold") + 30
        local active = t.name == current
        local hot = over(tx, y + 62, tw, 34)
        d:rect(V(tx, y + 62), V(tx + tw, y + 96), {colour = theme.a(active and theme.sun or theme.ink, active and 0.16 or (hot and 0.1 or 0.04)),
                                                     fill = true, rounding = 6 * draw.u})
        if active and focus == 0 then
            d:rect(V(tx, y + 62), V(tx + tw, y + 96), {colour = theme.sun, fill = false, thickness = 2, rounding = 6 * draw.u})
        end
        draw.text(d, tx + tw / 2, y + 71, t.name, 15, active and theme.sun or theme.ink, "centre", "bold")
        if hot and mouse.clicked and not active then
            current = t.name
            focus = 0
            pcall(reskate.audio.play_ui, "tab")
        end
        tx = tx + tw + 8
    end
    draw.rule(d, x, y + 106, W)
    local body = setmetatable({d = d, tab = current, x = x + PAD, y = y + 118, w = W - 2 * PAD, n = 0}, drawn)
    local ok, why = pcall(tab_named(current).build, body)
    if not ok then tell("tab " .. tostring(current), "menu tab " .. tostring(current) .. ": " .. tostring(why)) end
    focusables = body.n
    if focus > focusables then focus = focusables end
    nav = nil   -- a key for a widget that is not there any more
end

function menu.close()
    if not menu.open then return end
    menu.open = false
    hold_input(false)
end

function menu.toggle()
    if menu.open then return menu.close() end
    menu.open, opened_at, focus, nav = true, now(), 0, nil
    mouse.down = true      -- a button held while opening is no click
    was = held()           -- nor a key held while opening
    hold_input(true)
end

function menu.state() return {open = menu.open, tab = current, focus = focus} end

-- The menu's draw handler: added by menu.attach(), after the plugin's others, so it is drawn over them.
function menu.attach()
    reskate.on("draw", function(d)
        if not menu.open then return end
        local ok, why = pcall(render, d)
        if not ok then tell("draw", "menu draw: " .. tostring(why)) end
    end)
end

reskate.on("tick", function()
    if pending then
        local p = pending
        pending = nil
        menu.apply(p.tab, p.set)
    end
    if menu.open then navigate() end
end)

return menu
