-- Small helpers both copies of the HUD use. Written in the part of Luau that Lua 5.4 also runs (pairs/ipairs,
-- no +=, no continue, no if-expressions), so tests/test_tools_plugin.py runs it with lupa as it is.
local util = {}

util.STEAMID = "^7656119%d%d%d%d%d%d%d%d%d%d$"

function util.clamp(x, lo, hi)
    if x < lo then return lo end
    if x > hi then return hi end
    return x
end

-- A player's name or a server's text on one line, at most `most` characters (default 40).
function util.one_line(text, most)
    text = tostring(text or ""):gsub("[%c]", " ")
    most = most or 40
    if #text > most then text = text:sub(1, most - 1) .. "." end
    return text
end

-- 3725 s -> "1h 02m"; under an hour "12m"; under a minute "45s".
function util.duration(seconds)
    seconds = math.max(0, math.floor(seconds or 0))
    if seconds >= 3600 then return string.format("%dh %02dm", seconds // 3600, (seconds % 3600) // 60) end
    if seconds >= 60 then return string.format("%dm", seconds // 60) end
    return string.format("%ds", seconds)
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

-- JSON from a message, or nil (never an error).
function util.decode(json, text)
    if type(text) ~= "string" then return nil end
    local ok, value = pcall(json.decode, text)
    if ok and type(value) == "table" then return value end
    return nil
end

return util
