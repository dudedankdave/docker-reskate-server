-- Base64 (standard alphabet, padding optional) to text, for what the server's tools send through the console:
-- a console line splits on spaces, so JSON comes in base64. nil for anything that is not base64.
local ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
local value = {}
for i = 1, #ALPHABET do value[ALPHABET:sub(i, i)] = i - 1 end

return function(text)
    if type(text) ~= "string" then return nil end
    text = text:gsub("=+$", "")
    if #text % 4 == 1 or text:find("[^%w%+/]") then return nil end
    local out = {}
    for i = 1, #text, 4 do
        local n, bits = 0, 0
        for j = i, math.min(i + 3, #text) do
            n = n * 64 + value[text:sub(j, j)]
            bits = bits + 6
        end
        -- 4 characters are 3 bytes; 3 are 2 (18 bits, 2 of them padding); 2 are 1 (12 bits, 4 padding)
        local bytes = bits // 8
        n = n // (2 ^ (bits - bytes * 8))
        local chunk = {}
        for k = bytes, 1, -1 do
            chunk[k] = string.char(math.floor(n % 256))
            n = n // 256
        end
        out[#out + 1] = table.concat(chunk)
    end
    return table.concat(out)
end
