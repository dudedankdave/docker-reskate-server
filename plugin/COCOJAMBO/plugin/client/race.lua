-- The race HUD: what a racer sees. The server copy runs the race and tells this copy about the vote, sign-up,
-- GO, checkpoints, standings, the finish and the results, and a builder's editor; this copy draws them.
local util = require("lib/util")
local route = require("lib/route")
local draw = require("client/draw")
local settings = require("client/settings")

local race = {}
local now = reskate.time.now
local V = draw.V

local state = nil       -- the race this player is in
local shown = nil       -- the results, while they show
local note = nil        -- a callout low in the middle
local poll = nil        -- the vote on the track
local edit = nil        -- the editor, while this player builds a track
local pressed = nil     -- the editor key just pressed: {k, at}

function race.reset() state, shown, note, poll, edit = nil, nil, nil, nil, nil end
-- The race HUD has the top of the screen: the notes go below it.
function race.busy() return state ~= nil or shown ~= nil end
function race.voting() return poll ~= nil and now() <= poll.until_at end
function race.editing() return edit ~= nil end

local function flash(text, colour, ms, sub, sub_colour)
    note = {text = text, colour = colour or draw.sun, until_ms = now() + (ms or 2200), at = now(), sub = sub, sub_colour = sub_colour}
end

local function vec(p) return vector.create(p.x, p.y, p.z) end
local function zones_of(cps, laps)
    local list = {}
    for i, cp in ipairs(cps or {}) do list[i] = {c = route.vec(cp.c), r = cp.r} end
    return route.build({checkpoints = list, laps = laps or 1})
end

function race.message(m)
    if m.t == "poll" then
        poll = {options = m.options or {}, until_at = now() + (m.until_ms or 0), by = m.by, mine = poll and poll.mine,
                total = (poll and poll.total) or m.until_ms or 1, at = (poll and poll.at) or now()}
    elseif m.t == "voted" then
        if poll then poll.mine = m.n end
    elseif m.t == "poll_end" then
        poll = nil
        if m.winner then flash(string.upper(m.winner), draw.sun, 2500) end
    elseif m.t == "edit" then
        edit = m
        edit.zones = zones_of(m.cps, 1)
    elseif m.t == "edit_end" then
        edit = nil
    elseif m.t == "signup" then
        shown = nil
        state = {phase = "signup", title = m.title, signup_end = now() + (m.until_ms or 0), signup_ms = math.max(1, m.until_ms or 1)}
    elseif m.t == "race" then
        state = {phase = "countdown", title = m.title, laps = m.laps or 1, limit = m.limit, zones = zones_of(m.cps, m.laps),
                 cps = #(m.cps or {}), next = 1, lap = 1, go_at = now() + (m.go_in_ms or 0), racers = m.racers, best = m.best, trial = m.trial}
        shown = nil
    elseif not state then
        if m.t == "end" and not m.cancelled and m.rows and #m.rows > 0 then
            shown = {rows = m.rows, until_ms = now() + 12000, record = m.record, at = now()}
        end
    elseif m.t == "go" then
        state.phase, state.go_at, state.go_flash = "running", now(), now()
    elseif m.t == "false" then
        state.penalty = m.s
        flash("FALSE START  +" .. tostring(m.s) .. " S", draw.red, 3000)
    elseif m.t == "pos" then
        state.place, state.of = m.place, m.of
    elseif m.t == "board" and type(m.rows) == "table" then
        state.board = m.rows
    elseif m.t == "cp" then
        local zone = state.zones[m.k]
        state.next = m.k + 1
        local sub, colour = "FIRST", draw.green
        if type(m.gap) == "number" then sub, colour = util.gap(m.gap), draw.red end
        if zone and zone.lap then
            state.lap = state.lap + 1
            local text = "LAP " .. state.lap .. "/" .. state.laps
            if state.lap == state.laps then text = "FINAL LAP" end
            flash(text, draw.sun, 2200, sub, colour)
        else
            flash("CHECKPOINT  " .. util.clock(m.ms), draw.teal, 1800, sub, colour)
        end
    elseif m.t == "fin" then
        state.place, state.final, state.done_ms = m.place, true, m.ms
        state.next = #state.zones + 1
        flash(string.upper(util.ordinal(m.place)) .. "  " .. util.clock(m.ms), draw.medals[m.place] or draw.paper, 4000)
    elseif m.t == "end" then
        if m.cancelled then
            flash("RACE STOPPED", draw.dim, 2500)
        elseif m.rows and #m.rows > 0 then
            shown = {rows = m.rows, until_ms = now() + 12000, record = m.record, at = now()}
        end
        state = nil
    end
end

-- ---- keys: 1-5 vote (or, in the editor, its keys); Ctrl+S, Ctrl+Z and X in the editor -------------------
local EDIT_KEYS = {"cp", "del", "insert", "start", "try"}
function race.key(k, send)
    if edit and not race.voting() then
        local verb = EDIT_KEYS[k]
        if verb then
            send({t = "edit_key", verb = verb})
            pressed = {k = tostring(k), at = now()}
        end
        return
    end
    if poll and poll.options[k] and race.voting() then send({t = "vote", n = k}) end
end
function race.edit_key(verb, label, send)
    if not edit then return end
    send({t = "edit_key", verb = verb})
    pressed = {k = label, at = now()}
end

-- ---- in the world: a gate is a beacon going up, and on the screen a diamond with its label --------------
local function diamond(d, at, size, colour, filled)
    local pts = {vector.create(at.x, at.y - size, 0), vector.create(at.x + size, at.y, 0), vector.create(at.x, at.y + size, 0),
                 vector.create(at.x - size, at.y, 0)}
    if filled then d:poly(pts, {colour = draw.ink .. "c0", fill = true}) end
    d:poly(pts, {colour = colour, fill = filled, thickness = 3})
end
local function gate(d, c, colour, label, me, big)
    if not settings.gates then return end
    d:world_line(vec(c), vector.create(c.x, c.y + 28, c.z), {colour = colour .. (big and "a0" or "50"), thickness = (big and 4) or 2})
    local on = d:world_to_screen(vector.create(c.x, c.y + 1.5, c.z))
    if not on then return end
    -- a point just beside the camera comes back far off the screen, and the overlay refuses those
    local screen = d:size()
    if math.abs(on.x - screen.x / 2) > screen.x * 1.5 or math.abs(on.y - screen.y / 2) > screen.y * 1.5 then return end
    local u = draw.u
    diamond(d, on, ((big and 16) or 10) * u, colour, big)
    if label then
        local t = label
        if me then t = t .. string.format("   %d M", math.floor(route.distance(c, me.position) + 0.5)) end
        local w = d:measure(t, {size = 20 * draw.scale, font = "heading"})
        d:rough_rect(vector.create(on.x - w.x / 2 - 12 * u, on.y - 58 * u, 0), vector.create(on.x + w.x / 2 + 12 * u, on.y - 26 * u, 0),
                     {colour = draw.ink .. "d8", seed = 9})
        d:text(vector.create(on.x, on.y - 55 * u, 0), t, {size = 20 * draw.scale, colour = colour, align = "centre", font = "heading", shadow = false})
    end
end

-- Which way the next gate is from the skater, as an arrow (up: ahead).
local function compass(d, cx, y, c, me, colour)
    if not me then return end
    local u = draw.u
    cx, y = cx * u, y * u
    local dx, dz = c.x - me.position.x, c.z - me.position.z
    local h = me.heading or 0
    local ahead = dx * math.sin(h) + dz * math.cos(h)
    local right = dz * math.sin(h) - dx * math.cos(h)
    local len = math.sqrt(ahead * ahead + right * right)
    if len < 0.5 then return end
    local sx, sy = right / len, -ahead / len
    local px, py = -sy, sx
    local function arrow(size, col)
        local tip = vector.create(cx + sx * 26 * size * u, y + sy * 26 * size * u, 0)
        local l = vector.create(cx - sx * 18 * size * u + px * 20 * size * u, y - sy * 18 * size * u + py * 20 * size * u, 0)
        local r = vector.create(cx - sx * 18 * size * u - px * 20 * size * u, y - sy * 18 * size * u - py * 20 * size * u, 0)
        local notch = vector.create(cx - sx * 6 * size * u, y - sy * 6 * size * u, 0)
        d:poly({tip, l, notch}, {colour = col, fill = true})
        d:poly({tip, notch, r}, {colour = col, fill = true})
    end
    d:circle(vector.create(cx, y, 0), 34 * u, {colour = draw.ink .. "c8", fill = true})
    arrow(1.18, draw.ink)
    arrow(1, colour)
end

-- ---- the race ----------------------------------------------------------------------------------------------
local function clock_card(d, cx, t)
    local ms = state.done_ms or 0
    if not state.done_ms and state.phase == "running" then ms = t - state.go_at + (state.penalty or 0) * 1000 end
    local x, w = cx - 200, 400
    draw.card(d, x, 22, w, 92)
    draw.text(d, cx, 26, util.clock(ms), 64, state.done_ms and draw.green or draw.paper, "centre", "title")
    local line = "TO THE FINISH"
    if state.laps > 1 then
        line = "LAP " .. math.min(state.lap, state.laps) .. "/" .. state.laps
    elseif state.cps > 1 then
        line = "CHECKPOINT " .. math.min(state.next - 1, state.cps - 1) .. "/" .. (state.cps - 1)
    end
    if state.next > #state.zones then line = "FINISHED" end
    d:rough_rect(V(cx - 150, 108), V(cx + 150, 142), {colour = draw.coral, seed = 11})
    draw.text(d, cx, 112, line, 22, draw.ink, "centre")
    if state.best then
        d:rough_rect(V(cx - 100, 146), V(cx + 100, 172), {colour = draw.ink .. "d0", seed = 12})
        draw.text(d, cx, 149, "RECORD  " .. util.clock(state.best), 17, draw.paper, "centre", "bold")
    end
    if state.place then
        local colour = draw.medals[state.place] or draw.paper
        d:rough_rect(V(x + w + 12, 22), V(x + w + 112, 114), {colour = colour, seed = 5})
        draw.text(d, x + w + 62, 28, string.upper(util.ordinal(state.place)), 40, draw.ink, "centre", "title")
        if state.of and not state.final then draw.text(d, x + w + 62, 84, "OF " .. state.of, 17, draw.ink, "centre") end
    end
end

local glide, glide_at = {}, 0
local function standings(d)
    local rows = state.board
    if not rows or #rows == 0 then return end
    local t = now()
    local step = util.clamp((t - glide_at) / 100, 0, 1)
    glide_at = t
    local x, y, w = 24, draw.h * 0.26, 360
    if settings.side == "left" and settings.corner then y = math.max(y, 170) end
    local last = rows[#rows]
    local h = 52 + #rows * 44 + ((last.p > #rows) and 10 or 0)
    draw.card(d, x, y, w, h)
    draw.tag(d, x + 16, y - 16, "STANDINGS", draw.sun, 20, 24)
    for i, r in ipairs(rows) do
        local slot = i
        if r.p > i and i == #rows then slot = i + 0.25 end
        local cur = glide[r.n] or slot
        cur = cur + (slot - cur) * step
        glide[r.n] = cur
        local ry = y + 44 + (cur - 1) * 44
        local colour = draw.medals[r.p] or "#e8e8e8"
        if r.me then d:rough_rect(V(x + 10, ry - 2), V(x + w - 10, ry + 40), {colour = "#ffd23f30", seed = 70 + i}) end
        d:rough_rect(V(x + 16, ry + 2), V(x + 62, ry + 36), {colour = colour, seed = 60 + i})
        draw.text(d, x + 39, ry + 4, tostring(r.p), 24, draw.ink, "centre", "title")
        draw.text(d, x + 74, ry + 6, r.n, 22, (r.me and draw.sun) or draw.paper)
        local right, rc = "", draw.dim
        if r.t then right, rc = util.clock(r.t), draw.green
        elseif r.g then right, rc = util.gap(r.g), draw.red
        elseif r.p == 1 then right, rc = "LEADER", draw.sun end
        draw.text(d, x + w - 18, ry + 8, right, 20, rc, "right")
    end
end

local function running(d)
    local cx, t = draw.w / 2, now()
    local ok, me = pcall(reskate.skater.get)
    if not ok then me = nil end
    if state.phase == "signup" then
        local left = math.max(0, math.ceil((state.signup_end - t) / 1000))
        draw.card(d, cx - 240, 22, 480, 92)
        draw.tag(d, cx - 70, 6, "SIGN-UP", draw.sun, 20, 21)
        draw.text(d, cx, 46, string.upper(state.title), 30, draw.paper, "centre", "title")
        draw.text(d, cx, 80, "Stay on your spot  -  " .. left .. " s", 18, draw.paper, "centre", "bold")
        draw.bar(d, cx - 220, 104, 440, (state.signup_end - t) / state.signup_ms)
        return
    end
    clock_card(d, cx, t)
    if state.phase == "running" then standings(d) end
    local zone = state.zones[state.next]
    if zone then
        local colour = draw.sun
        local label = "CP " .. zone.cp
        if zone.finish then colour, label = draw.coral, "FINISH" elseif zone.lap then colour, label = draw.coral, "LAP" end
        gate(d, zone.c, colour, label, me, true)
        local after = state.zones[state.next + 1]
        if after then gate(d, after.c, colour, nil, me, false) end
        compass(d, cx, (state.best and 218) or 196, zone.c, me, colour)
    end
    if state.phase == "countdown" then
        local left = math.ceil((state.go_at - t) / 1000)
        local y = draw.h * 0.30
        if left >= 1 then
            local into = 1 - ((state.go_at - t) % 1000) / 1000
            local land = 1 - (1 - math.min(1, into / 0.22)) ^ 3
            local size = 150 * (1.45 - 0.45 * land)
            local colour = draw.red
            if left >= 3 then colour = draw.sun elseif left == 2 then colour = "#ff9a3c" end
            local half = 90 * (1.2 - 0.2 * land)
            d:rough_rect(V(cx - half, y - 6), V(cx + half, y + 180), {colour = draw.ink .. "e8", seed = 14 + left})
            draw.text(d, cx, y + 84 - size * 0.55, tostring(left), size, colour, "centre", "title_large")
        end
    end
    if state.go_flash and t - state.go_flash < 900 then
        local k = (t - state.go_flash) / 900
        local land = 1 - (1 - math.min(1, k / 0.25)) ^ 3
        local y = draw.h * 0.30
        local a = string.format("%02x", math.floor(255 * util.clamp((1 - k) / 0.35, 0, 1)))
        draw.text(d, cx, y + 84 - 150 * (1.5 - 0.5 * land) * 0.55, "GO!", 150 * (1.5 - 0.5 * land), draw.green .. a, "centre", "title_large")
    end
end

local function callout(d)
    local cx = draw.w / 2
    local y = draw.h * 0.70 - util.clamp(1 - (note.until_ms - now()) / 250, 0, 1) * 20
    local grow = math.min(1, (now() - note.at) / 140) * util.clamp((note.until_ms - now()) / 250, 0, 1)
    local w = (draw.width(d, note.text, 42) + 64) * grow
    d:rough_rect(V(cx - w / 2, y - 8), V(cx + w / 2, y + 60), {colour = draw.ink .. "e8", seed = 15})
    d:rough_rect(V(cx - w / 2, y + 52), V(cx + w / 2, y + 60), {colour = note.colour, seed = 16})
    if grow >= 1 then
        draw.text(d, cx, y, note.text, 42, note.colour, "centre")
        if note.sub then
            local sw = draw.width(d, note.sub, 30, "title") + 36
            d:rough_rect(V(cx - sw / 2, y + 68), V(cx + sw / 2, y + 112), {colour = note.sub_colour or draw.red, seed = 17})
            draw.text(d, cx, y + 72, note.sub, 30, draw.ink, "centre", "title")
        end
    end
end

local function vote_card(d)
    local n = #poll.options
    local x, y, w = draw.w - 470 + (1 - draw.ease(now(), poll.at, 160)) * 80, 220, 440
    local h = 96 + n * 62
    draw.card(d, x, y, w, h)
    draw.tag(d, x + 16, y - 16, "PICK THE TRACK", draw.sun, 22, 17)
    draw.text(d, x + w - 22, y + 14, math.max(0, math.ceil((poll.until_at - now()) / 1000)) .. " S", 22, draw.sun, "right")
    local most = 1
    for _, o in ipairs(poll.options) do most = math.max(most, o.votes or 0) end
    for i, o in ipairs(poll.options) do
        local ry = y + 48 + (i - 1) * 62
        local mine = poll.mine == o.n
        if mine then d:rough_rect(V(x + 12, ry - 4), V(x + w - 12, ry + 54), {colour = "#ffffff22", seed = 20 + i}) end
        d:rough_rect(V(x + 20, ry), V(x + 62, ry + 42), {colour = (mine and draw.sun) or "#e8e8e8", seed = 30 + i})
        draw.text(d, x + 41, ry + 4, tostring(o.n), 28, draw.ink, "centre", "title")
        draw.text(d, x + 76, ry, util.one_line(o.title, 22):upper(), 22, (mine and draw.sun) or draw.paper)
        local sub = {}
        if (o.laps or 1) > 1 then sub[#sub + 1] = o.laps .. " laps" end
        if o.best then sub[#sub + 1] = "record " .. util.clock(o.best) end
        draw.text(d, x + 76, ry + 26, table.concat(sub, "  -  "), 15, draw.dim, "left", "bold")
        if (o.votes or 0) > 0 then d:rough_rect(V(x + w - 132, ry + 10), V(x + w - 132 + 90 * o.votes / most, ry + 30), {colour = draw.teal, seed = 40 + i}) end
        draw.text(d, x + w - 24, ry + 6, tostring(o.votes or 0), 22, draw.paper, "right")
    end
    draw.text(d, x + w / 2, y + h - 40, "VOTE: PRESS 1-" .. n .. "  OR  /race <number>", 18, draw.paper, "centre")
    draw.bar(d, x + 20, y + h - 14, w - 40, (poll.until_at - now()) / poll.total)
end

local function results(d)
    local rows = shown.rows
    local w = 600
    local x, y = draw.w / 2 - w / 2, draw.h * 0.20 - (1 - draw.ease(now(), shown.at, 220)) * 60
    local h = 62 + #rows * 46 + ((shown.record and 54) or 0) + 10
    draw.card(d, x, y, w, h)
    d:rough_rect(V(x + w / 2 - 150, y - 22), V(x + w / 2 + 150, y + 30), {colour = draw.coral, seed = 18})
    draw.text(d, x + w / 2, y - 18, "RESULTS", 38, draw.ink, "centre", "title")
    for i, r in ipairs(rows) do
        if now() - shown.at > i * 140 then
            local ry = y + 52 + (i - 1) * 46
            local colour = draw.medals[r.place or 0] or draw.paper
            if r.dnf then colour = draw.dim end
            d:rough_rect(V(x + 26, ry + 4), V(x + 92, ry + 36), {colour = (r.dnf and "#77736c") or colour, seed = 60 + i})
            draw.text(d, x + 59, ry + 6, (r.dnf and "DNF") or string.upper(util.ordinal(r.place)), 22, draw.ink, "centre")
            draw.text(d, x + 108, ry + 6, util.one_line(r.name, 24), 24, (r.dnf and draw.dim) or draw.paper)
            draw.text(d, x + w - 32, ry + 6, (r.dnf and "-") or util.clock(r.ms), 24, colour, "right")
        end
    end
    if shown.record then
        local ry = y + 52 + #rows * 46 + 6
        d:rough_rect(V(x + 110, ry), V(x + w - 110, ry + 40), {colour = draw.sun, seed = 19})
        draw.text(d, x + w / 2, ry + 6, "NEW TRACK RECORD", 24, draw.ink, "centre")
    end
end

-- ---- the editor ------------------------------------------------------------------------------------------
local EDIT_HELP = {{"1", "CHECKPOINT HERE"}, {"2", "DELETE THE NEAREST"}, {"3", "INSERT BEFORE THE NEAREST"}, {"4", "START HERE"},
                   {"5", "TRY IT"}, {"Ctrl+Z", "UNDO"}, {"Ctrl+S", "SAVE"}, {"X", "CANCEL"}}
local function editor(d)
    local ok, me = pcall(reskate.skater.get)
    if not ok then me = nil end
    local n = #edit.zones
    for i, z in ipairs(edit.zones) do
        local colour, label = draw.sun, "CP " .. i .. "  (" .. z.r .. " M)"
        if i == n then colour, label = draw.coral, "FINISH " .. i .. "  (" .. z.r .. " M)" end
        gate(d, z.c, colour, label, me, true)
    end
    if edit.start then gate(d, route.vec(edit.start), draw.green, "START", me, false) end
    local x, y, w = 24, 200, 470
    if settings.side == "right" then x = draw.w - w - 24 end
    local h = 150 + #EDIT_HELP * 40 + 34
    draw.card(d, x, y, w, h)
    draw.tag(d, x + 16, y - 16, "TRACK EDITOR", draw.coral, 22, 22)
    draw.text(d, x + 24, y + 34, util.one_line(edit.title, 26):upper() .. ((edit.dirty and "  *") or ""), 28, draw.paper, "left", "title")
    local tiles = {{tostring(n), "CHECKPOINTS"}, {tostring(edit.laps or 1), "LAPS"}, {(edit.start and "SET") or "-", "START"}}
    for i, tl in ipairs(tiles) do
        local tx = x + 18 + (i - 1) * 146
        d:rough_rect(V(tx, y + 74), V(tx + 136, y + 132), {colour = "#0c0c10f0", seed = 80 + i})
        draw.text(d, tx + 14, y + 76, tl[1], 32, draw.sun, "left", "title")
        draw.text(d, tx + 14, y + 112, tl[2], 13, draw.dim, "left", "bold")
    end
    for i, hl in ipairs(EDIT_HELP) do
        local ry = y + 148 + (i - 1) * 40
        local lit = pressed and pressed.k == hl[1] and now() - pressed.at < 350
        local kw = (#hl[1] > 1 and 78) or 40
        d:rough_rect(V(x + 22, ry), V(x + 22 + kw, ry + 32), {colour = (lit and draw.sun) or "#f2f2f2", seed = 90 + i})
        draw.text(d, x + 22 + kw / 2, ry + 3, string.upper(hl[1]), (#hl[1] > 1 and 16) or 24, draw.ink, "centre", (#hl[1] > 1 and "bold") or "title")
        draw.text(d, x + 36 + kw, ry + 5, hl[2], 18, (lit and draw.sun) or draw.paper)
    end
    draw.text(d, x + 24, y + h - 30, "/race laps <n>  /race radius <n> <m>  /race title <t>  /race help-edit", 13, draw.dim, "left", "bold")
end

function race.draw(d)
    if edit then editor(d) end
    if state then running(d) end
    if poll then
        if now() > poll.until_at + 3000 then poll = nil else vote_card(d) end
    end
    if shown then
        if now() > shown.until_ms then shown = nil else results(d) end
    end
    if note then
        if now() > note.until_ms then note = nil else callout(d) end
    end
end

return race
