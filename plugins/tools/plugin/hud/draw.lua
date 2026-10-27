-- Drawing helpers for the HUD, in the COCOJAMBO look (lib/theme.lua): positions in pixels of a 1080-line
-- screen (draw.u turns them into the window's); draw.frame(d) sets the scale each frame.
local theme = require("lib/theme")

local draw = {u = 1, scale = 1}
draw.LOGO = "images/logo.png"   -- the website's logo, 1024 x 143
draw.LOGO_RATIO = 1024 / 143

function draw.frame(d, scale)
    draw.scale = scale or draw.scale
    local screen = d:size()
    draw.u = screen.y / 1080 * draw.scale
    draw.w, draw.h = screen.x / draw.u, screen.y / draw.u   -- the screen in the 1080-line grid
    return screen
end

function draw.V(x, y) return vector.create(x * draw.u, y * draw.u, 0) end
local V = draw.V

-- A card as on the website: a near-black surface, 8 px corners, a thin light border. alpha: the surface's.
function draw.card(d, x, y, w, h, alpha, radius)
    local r = (radius or theme.radius) * draw.u
    d:rect(V(x, y), V(x + w, y + h), {colour = theme.a(theme.surface, alpha or 0.92), fill = true, rounding = r})
    d:rect(V(x, y), V(x + w, y + h), {colour = theme.line_strong, fill = false, thickness = 1, rounding = r})
end
-- A thin line across a card, between its parts.
function draw.rule(d, x, y, w)
    d:rect(V(x, y), V(x + w, y + 1), {colour = theme.line_strong, fill = true})
end
function draw.text(d, x, y, text, size, colour, align, font)
    d:text(V(x, y), tostring(text), {size = size * draw.scale, colour = colour or theme.ink, align = align or "left",
                                     font = font or "body", shadow = false})
end
-- A small uppercase grey label over a part of a card, as the website's section heads.
function draw.label(d, x, y, text, colour)
    draw.text(d, x, y, string.upper(text), 13, colour or theme.muted, "left", "bold")
end
function draw.width(d, text, size, font)
    return d:measure(tostring(text), {size = size * draw.scale, font = font or "body"}).x / draw.u
end
-- A pill with a coloured dot: the role, a note's kind.
function draw.pill(d, x, y, text, dot, size)
    size = size or 13
    local w = draw.width(d, text, size, "bold") + 30
    local h = size + 12
    d:rect(V(x, y), V(x + w, y + h), {colour = theme.a(theme.ink, 0.06), fill = true, rounding = h / 2 * draw.u})
    d:rect(V(x, y), V(x + w, y + h), {colour = theme.line_strong, fill = false, thickness = 1, rounding = h / 2 * draw.u})
    d:circle(V(x + 11, y + h / 2), 3.5 * draw.u, {colour = dot or theme.mint, fill = true})
    draw.text(d, x + 20, y + 5, text, size, theme.ink, "left", "bold")
    return w
end
function draw.logo(d, x, y, h, alpha)
    d:image(draw.LOGO, V(x, y), V(x + h * draw.LOGO_RATIO, y + h), {colour = theme.a("#ffffff", alpha or 1)})
end
-- 0..1 over the first `ms` after `at`, easing out: for sliding in.
function draw.ease(now, at, ms)
    local t = (now - (at or 0)) / ms
    if t < 0 then t = 0 elseif t > 1 then t = 1 end
    return 1 - (1 - t) * (1 - t)
end
-- Text cut to fit `w` (in the grid), with a dot at the end when cut.
function draw.fit(d, text, size, w, font)
    text = tostring(text)
    if draw.width(d, text, size, font) <= w then return text end
    while #text > 1 and draw.width(d, text .. ".", size, font) > w do text = text:sub(1, -2) end
    return text .. "."
end

return draw
