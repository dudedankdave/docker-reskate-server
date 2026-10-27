-- The COJA menu (F5): a screen of its own (reskate.ui.screen), not in the Insert menu. The game draws it and hands
-- its function the same widgets as the plugin's page (tabs, card, info, note, separator, button, dropdown, slider,
-- toggle), used with the mouse; the tabs (stats, races, challenges, Track Studio) build themselves with them.
local menu = {}
local SCREEN = "coja"
local now = reskate.time.now
local built_at = -1000    -- the game calls the screen's function every frame it is open

local tabs = {}           -- {name, build(ui), shown()}
local current = nil
local report = function() end

-- A tab: build(ui) is called with the widgets; shown() (optional) says whether the tab is there.
function menu.tab(name, build, shown) tabs[#tabs + 1] = {name = name, build = build, shown = shown} end
-- What the game made of the screen goes to report(text) (the races' line in the server's log).
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

-- Builds tab `name` with the widgets `ui`; false when there is no such tab (or it is not shown).
function menu.build(name, ui)
    local t = tab_named(name)
    if not t then return false end
    t.build(ui)
    return true
end
-- Shows tab `name` next time the menu is built.
function menu.show(name) current = name end

-- The screen's function: the tab row, then the chosen tab.
local told = false
local function on_screen(ui)
    built_at = now()
    if not told then
        told = true
        local names = {}
        local ok_meta, meta = pcall(getmetatable, ui)
        local index = ok_meta and type(meta) == "table" and type(meta.__index) == "table" and meta.__index or ui
        if type(index) == "table" then
            for k in pairs(index) do names[#names + 1] = tostring(k) end
        end
        table.sort(names)
        report("ui.screen widgets: " .. table.concat(names, " "))
    end
    local list = visible_tabs()
    if #list == 0 then return end
    if not tab_named(current or "") then current = list[1].name end
    local names, at = {}, 1
    for i, t in ipairs(list) do
        names[i] = t.name
        if t.name == current then at = i end
    end
    current = names[ui:tabs("coja_tabs", names, at)] or current
    local ok, why = pcall(tab_named(current).build, ui)
    if not ok then report("tab " .. tostring(current) .. ": " .. tostring(why)) end
end

local screen_ok, screen_why = pcall(reskate.ui.screen, SCREEN, on_screen)

-- Open: built within the last 300 ms (Esc closes it in the game itself).
function menu.is_open() return now() - built_at < 300 end

function menu.close()
    built_at = -1000
    pcall(reskate.ui.close, SCREEN)
end

function menu.toggle()
    if menu.is_open() then return menu.close() end
    if not screen_ok then
        report("ui.screen: " .. tostring(screen_why))
        return false
    end
    local ok, opened, why = pcall(reskate.ui.open, SCREEN)
    if not ok or opened == false then
        report("ui.open: " .. tostring(ok and why or opened))
        return false
    end
    return true
end

function menu.state() return {open = menu.is_open(), tab = current} end

return menu
