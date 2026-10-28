-- COJA tools. ReSkate gives a plugin's load one call of 50 ms, and loads plugins while the game loads its level: on
-- a busy start this plugin's load once ran over it and was stopped ("plugin/main.lua ran longer than 50 ms": no F4,
-- no F5 until the game restarted). So the load only queues the plugin's parts: each is required in a call of its own
-- on the next ticks (require keeps what it loaded, so races.lua's own requires of them are then free).
local parts = reskate.side == "server"
    and {"lib/util", "lib/base64", "hud/calls", "hud/server", "races"}
    or {"lib/theme", "lib/util", "hud/draw", "hud/settings", "hud/client", "hud/menu", "hud/hub", "hud/home", "races"}

local function next_part(i)
    if not parts[i] then return end
    reskate.time.after(1, function()
        require(parts[i])
        next_part(i + 1)
    end)
end
next_part(1)
