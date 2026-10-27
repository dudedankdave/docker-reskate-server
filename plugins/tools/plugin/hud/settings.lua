-- The player's settings, on the plugin's page in the ReSkate menu (Insert > PLUGINS > COJA tools), kept in storage.
local settings = {}

local DEFAULTS = {corner = true, side = "left", scale = 1, toasts = true}   -- side: the chip's; the panel opens on the other
local saved = reskate.storage.get("hud:settings")
if type(saved) ~= "table" then saved = {} end
for k, v in pairs(DEFAULTS) do
    if saved[k] == nil then settings[k] = v else settings[k] = saved[k] end
end

local function save()
    local out = {}
    for k in pairs(DEFAULTS) do out[k] = settings[k] end
    pcall(reskate.storage.set, "hud:settings", out)
end

-- what the page shows about the server: set by the HUD (hud/client.lua)
settings.status = function() return "Not on a COCOJAMBO server" end

-- More tabs from the rest of the plugin (the races): {name, build(ui), shown()}, between the dashboard and
-- the controls. settings.section adds one.
local sections = {}
function settings.section(name, build, shown)
    sections[#sections + 1] = {name = name, build = build, shown = shown}
end

local SIDES = {"left", "right"}
local current = "DASHBOARD"
local function page(ui)
    local names, builds = {"DASHBOARD"}, {}
    for _, s in ipairs(sections) do
        if not s.shown or s.shown() then
            names[#names + 1] = s.name
            builds[s.name] = s.build
        end
    end
    names[#names + 1] = "CONTROLS"
    names[#names + 1] = "SETTINGS"
    local at = 1
    for i, name in ipairs(names) do
        if name == current then at = i end
    end
    current = names[ui:tabs("sections", names, at)] or "DASHBOARD"
    if builds[current] then
        builds[current](ui)
    elseif current == "DASHBOARD" then
        local dash = settings.dashboard and settings.dashboard() or {}
        ui:card("COJA TOOLS", "Your COCOJAMBO server at a glance")
        ui:info("Server", dash.server or settings.status())
        ui:info("Player", dash.player or "Waiting for the server")
        ui:info("Points", dash.points or "—")
        ui:info("Rank", dash.rank or "—")
        ui:info("Playtime", dash.played or "—")
        ui:info("Leaderboard", dash.board or "Waiting for the server")
        ui:note("F4 opens your full stats, the top 10, server rules and links.")
        ui:separator()
        ui:card("RACES", "The RACE, CHALLENGES and TRACK STUDIO tabs; F5 opens this menu")
        ui:info("Multiplayer", "RACE tab, or Throwdowns > Cocojambo Deathraces")
        ui:info("Solo", "CHALLENGES tab, or skate to a yellow challenge beacon and press F8")
    elseif current == "CONTROLS" then
        ui:card("RACE CONTROLS", "Controller combos are rebindable in Binds")
        ui:info("F5", "This menu: races, challenges and Track Studio with the mouse")
        ui:info("F7", "Open Track Studio (staff)")
        ui:info("PgUp / PgDn, F6 · LB+←/→", "Select a track or gate")
        ui:info("F8 · LB+RB+A", "Start a nearby challenge; in Track Studio, create a track or place a gate")
        ui:info("F9 · LB+RB+B", "Delete the selected gate, or track from Track Studio")
        ui:info("6 · LB+RB+X", "Edit the selected track, or move the selected gate 3 m ahead")
        ui:info("7 · LB+RB+Start", "Toggle challenge in the library; circuit/sprint while editing")
        ui:info("1–5 · LB+RB+Y, RB+D-pad", "Choose an edit mode and adjust the selected gate")
        ui:info("F10", "Close Track Studio, leave a race, or close results")
        ui:separator()
        ui:card("HUD CONTROLS", "")
        ui:info("F4", "Open or close your stats and leaderboard panel")
        ui:info("F8", "Start a marked solo challenge near its beacon")
    else
        ui:card("HUD SETTINGS", "Personal display preferences")
        local changed = false
        local corner = ui:toggle("corner", "Points chip (points, rank, time played)", settings.corner)
        if corner ~= settings.corner then settings.corner, changed = corner, true end
        local side = 1
        if settings.side == "right" then side = 2 end
        local pick = ui:dropdown("side", "Points chip on the (panel opens on the other side)", {"Left", "Right"}, side)
        if SIDES[pick] ~= settings.side then settings.side, changed = SIDES[pick], true end
        local toasts = ui:toggle("toasts", "Notes from the server at the top of the screen", settings.toasts)
        if toasts ~= settings.toasts then settings.toasts, changed = toasts, true end
        local scale = ui:slider("scale", "HUD size", settings.scale, 0.6, 1.4, "%.1f")
        if math.abs(scale - settings.scale) > 0.001 then settings.scale, changed = scale, true end
        if ui:button("reset", "Back to the defaults") then
            for k, v in pairs(DEFAULTS) do settings[k] = v end
            changed = true
        end
        if changed then save() end
    end
end
reskate.menu.page("COJA tools", page)

-- F5: the same page as a screen of its own, where the game lets a plugin open one from a key. What the game
-- said is kept for the server's log (settings.report, set by the races).
-- Guarded: a screen the game builds some other way must not raise errors every frame.
local screen_error = nil
local screen, screen_why = pcall(reskate.ui.screen, "coja", {title = "COJA tools"}, function(ui)
    local ok, why = pcall(page, ui)
    if not ok then screen_error = tostring(why) end
end)
function settings.open()
    if not screen then return false, "ui.screen: " .. tostring(screen_why) end
    if screen_error then return false, "the screen's page: " .. screen_error end
    local ok, opened, why = pcall(reskate.ui.open, "coja")
    if not ok then return false, "ui.open: " .. tostring(opened) end
    if opened == false then return false, "ui.open: " .. tostring(why) end
    return true
end

return settings
