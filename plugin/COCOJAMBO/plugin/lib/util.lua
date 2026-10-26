-- Small helpers both copies use. Written in the part of Luau that Lua 5.4 also runs (pairs/ipairs, no +=,
-- no continue, no if-expressions), so tests/test_plugin.py can run it with lupa.
local util = {}

util.STEAMID = "^7656119%d%d%d%d%d%d%d%d%d%d$"

function util.clamp(x, lo, hi)
    if x < lo then return lo end
    if x > hi then return hi end
    return x
end

function util.round(x) return math.floor(x + 0.5) end

-- A player's name or a server's text on one line, at most `most` characters (default 40).
function util.one_line(text, most)
    text = tostring(text or ""):gsub("[%c]", " ")
    most = most or 40
    if #text > most then text = text:sub(1, most - 1) .. "." end
    return text
end

-- 83456 ms -> "1:23.45"; under a minute "23.45".
function util.clock(ms)
    ms = math.max(0, math.floor(ms or 0))
    local cs = (ms // 10) % 100
    local s = (ms // 1000) % 60
    local m = ms // 60000
    if m > 0 then return string.format("%d:%02d.%02d", m, s, cs) end
    return string.format("%d.%02d", s, cs)
end

-- 1234 ms -> "+1.23"
function util.gap(ms) return "+" .. util.clock(ms) end

-- 3725 s -> "1h 02m"; under an hour "12m"; under a minute "45s".
function util.duration(seconds)
    seconds = math.max(0, math.floor(seconds or 0))
    if seconds >= 3600 then return string.format("%dh %02dm", seconds // 3600, (seconds % 3600) // 60) end
    if seconds >= 60 then return string.format("%dm", seconds // 60) end
    return string.format("%ds", seconds)
end

function util.ordinal(n)
    local tens, ones = n % 100, n % 10
    if tens >= 11 and tens <= 13 then return n .. "th" end
    if ones == 1 then return n .. "st" end
    if ones == 2 then return n .. "nd" end
    if ones == 3 then return n .. "rd" end
    return n .. "th"
end

-- 12345 -> "12,345"
function util.thousands(n)
    local s = tostring(math.floor(n or 0))
    local sign = ""
    if s:sub(1, 1) == "-" then sign, s = "-", s:sub(2) end
    local out = s:reverse():gsub("(%d%d%d)", "%1,"):reverse()
    if out:sub(1, 1) == "," then out = out:sub(2) end
    return sign .. out
end

-- "/race new Big Loop" with name "race" -> {"new", "big", "loop"} (lowercased) and {"new", "Big", "Loop"} (as typed);
-- nil for another command.
function util.command_words(text, name)
    local rest = tostring(text or ""):match("^%s*/" .. name .. "(.*)$")
    if not rest or (rest ~= "" and not rest:match("^%s")) then return nil end
    local words, raw = {}, {}
    for word in rest:gmatch("%S+") do
        raw[#raw + 1] = word
        words[#words + 1] = word:lower()
    end
    return words, raw
end

-- A track's id from its title: "Big Loop!" -> "big-loop"; nil when nothing is left.
function util.slug(text)
    local s = tostring(text or ""):lower():gsub("[^%w]+", "-"):gsub("^%-+", ""):gsub("%-+$", "")
    if s == "" then return nil end
    return s:sub(1, 32)
end

function util.copy(t)
    if type(t) ~= "table" then return t end
    local out = {}
    for k, v in pairs(t) do out[k] = util.copy(v) end
    return out
end

function util.count(t)
    local n = 0
    for _ in pairs(t) do n = n + 1 end
    return n
end

-- JSON from a message, or nil (never an error).
function util.decode(json, text)
    if type(text) ~= "string" then return nil end
    local ok, value = pcall(json.decode, text)
    if ok and type(value) == "table" then return value end
    return nil
end

return util
