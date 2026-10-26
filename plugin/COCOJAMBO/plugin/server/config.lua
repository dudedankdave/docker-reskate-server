-- The server copy's settings: config.json in the package, each missing key its default here. An operator
-- changes them in the package's config.json on the server (a change reloads the plugin).
local DEFAULTS = {
    -- who may build tracks and stop others' races: the server's staff roles, a rank at least this, or admins
    staff_rank = 50,
    admins = {},
    -- races
    signup_s = 30,          -- sign-up after /race or a vote
    solo_signup_s = 5,      -- when only one player is on the server
    poll_s = 20,            -- the vote on the track
    poll_options = 5,
    countdown_s = 5,
    cooldown_s = 20,        -- between races
    hold_radius = 8,        -- before GO: further off their spot than this and a racer goes back
    false_start_s = 5,      -- time added for rolling before GO
    max_racers = 16,
    checkpoint_radius = 8,  -- a new checkpoint's radius in metres
    locks = {"noclip", "boosts"},   -- tools racers may not use while racing: noclip, nobail, boosts, tuning, objects, ollies
    icon = ":ReSkate:",     -- before the race's chat lines ("" for none)
}

local config = {}
local ok, file = pcall(reskate.package.read_json, "config.json")
if not ok or type(file) ~= "table" then
    if not ok then reskate.log("config.json: " .. tostring(file) .. " (using the defaults)", "warn") end
    file = {}
end
for k, v in pairs(DEFAULTS) do
    if file[k] == nil then config[k] = v else config[k] = file[k] end
end
for k, v in pairs(file) do
    if DEFAULTS[k] == nil then config[k] = v end
end
return config
