-- Drawing helpers for the HUD: positions in pixels of a 1080-line screen (draw.u turns them into the
-- window's), the COCOJAMBO colours and skate.'s hand-cut tiles. draw.frame(d) sets the scale each frame.
local draw = {u = 1, scale = 1}

-- The overlay shows colours a little darker than given on some setups: they are bright, and text is light on
-- dark or dark on a bright colour.
draw.ink, draw.paper, draw.dim = "#0d0b14", "#ffffff", "#c6c2d6"
draw.coral, draw.teal, draw.sun, draw.red, draw.green = "#ff4f7b", "#29d3c2", "#ffd23f", "#ff5a4a", "#4dff7c"
draw.medals = {"#ffd84a", "#eef1f5", "#f0a266"}

function draw.frame(d, scale)
    draw.scale = scale or draw.scale
    local screen = d:size()
    draw.u = screen.y / 1080 * draw.scale
    draw.w, draw.h = screen.x / draw.u, screen.y / draw.u   -- the screen in the 1080-line grid
    return screen
end

function draw.V(x, y) return vector.create(x * draw.u, y * draw.u, 0) end
local V = draw.V

-- A dark tile with hand-cut edges and its shadow; seed picks the edge.
function draw.card(d, x, y, w, h, colour, seed)
    d:rough_rect(V(x + 5, y + 7), V(x + w + 5, y + h + 7), {colour = "#00000060", seed = (seed or 3) + 1})
    d:rough_rect(V(x, y), V(x + w, y + h), {colour = colour or "#15121fe0", seed = seed or 3})
end
-- A strip of colour with dark text on it: a card's label.
function draw.tag(d, x, y, text, colour, size, seed)
    size = size or 20
    local w = d:measure(text, {size = size * draw.scale, font = "heading"}).x / draw.u + 28
    d:rough_rect(V(x, y), V(x + w, y + size + 14), {colour = colour or draw.coral, seed = seed or 7})
    draw.text(d, x + 14, y + 5, text, size, draw.ink)
    return w
end
function draw.text(d, x, y, text, size, colour, align, font)
    d:text(V(x, y), tostring(text), {size = size * draw.scale, colour = colour or draw.paper, align = align or "left",
                                     font = font or "heading", shadow = false})
end
function draw.width(d, text, size, font)
    return d:measure(tostring(text), {size = size * draw.scale, font = font or "heading"}).x / draw.u
end
-- A bar that runs down: left 0..1 of w.
function draw.bar(d, x, y, w, left, colour)
    if left <= 0 then return end
    if left > 1 then left = 1 end
    d:rect(V(x, y), V(x + w * left, y + 6), {colour = colour or draw.sun, fill = true, rounding = 3 * draw.u})
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
