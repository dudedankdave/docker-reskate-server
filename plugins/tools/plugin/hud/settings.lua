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

local SIDES = {"left", "right"}
reskate.menu.page("COJA tools", function(ui)
    ui:card("COJA TOOLS", "The HUD and races of the COCOJAMBO servers")
    ui:info("Server", settings.status())
    ui:note("F4 opens the panel: your stats, the board and the server's links. Races: Throwdowns > Cocojambo Race; staff build tracks with F7.")
    ui:separator()
    ui:card("HUD", "What shows while you skate")
    local changed = false
    local corner = ui:toggle("corner", "Points chip (points, rank, time played)", settings.corner)
    if corner ~= settings.corner then settings.corner, changed = corner, true end
    local side = 1
    if settings.side == "right" then side = 2 end
    local pick = ui:dropdown("side", "Points chip on the (the panel opens on the other side)", {"Left", "Right"}, side)
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
end)

return settings
