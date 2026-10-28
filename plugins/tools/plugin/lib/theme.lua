-- The COCOJAMBO look, as on cocojambo's website (website-cocojambo, src/app/globals.css): near-black surfaces
-- with thin light borders and 8 px corners, warm off-white text, muted grey labels and the logo's pastel colours
-- as accents. Colours are "#RRGGBB" or "#RRGGBBAA"; theme.a(colour, alpha) adds an alpha (0..1).
local theme = {
    bg = "#08080a",
    surface = "#111114",
    ink = "#f2f1ee",
    muted = "#8b8a93",
    line = "#ffffff14",          -- rgba(255, 255, 255, 0.075)
    line_strong = "#ffffff29",   -- rgba(255, 255, 255, 0.16)
    sky = "#87cfe4",
    sun = "#ffd972",
    mint = "#b3d7ac",
    rose = "#eec6df",
    clay = "#d4a373",
    sage = "#dbe1bc",
    lilac = "#ac88bd",
    iris = "#808ec7",
    danger = "#ff6b5e",
    radius = 8,
}

function theme.a(colour, alpha)
    local n = math.floor(math.max(0, math.min(1, alpha)) * 255 + 0.5)
    return colour:sub(1, 7) .. string.format("%02x", n)
end

-- Gold, silver and bronze for the first three places, in the logo's tones.
theme.places = {theme.sun, "#d9dce3", theme.clay}

return theme
