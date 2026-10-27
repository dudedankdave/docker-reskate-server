"""The COJA tools plugin (plugins/tools, COCOJAMBO-TOOLS): its package checks, and its Lua run against
a stand-in for ReSkate's plugin API: a server copy and players in one session. The races: an admin
building a track in the editor, the server keeping it, hosting it, joining the flag, the start
lights, the gates and the results. The HUD: the handshake, what the server's tools hand in on the
console (stats, the board, the server's info, notes), the time played, the panel and the settings page.

The Lua is Luau; the stand-in runs it on Lua 5.4 (lupa) after turning `x += y` into `x = x + y`.
Without lupa (pip install lupa) only the package checks run.
"""
import base64
import importlib.util
import json
import math
import pathlib
import re
import sys
import tempfile
import unittest
import zipfile

HERE = pathlib.Path(__file__).resolve().parent.parent
PLUGIN = HERE / "plugins" / "tools"
sys.path.insert(0, str(HERE / "app"))
import leaderboard  # noqa: E402   (its hud_line is what the server's tools type)

try:
    import lupa.lua54 as lupa
except ImportError:  # pragma: no cover
    lupa = None


def load_build():
    spec = importlib.util.spec_from_file_location("plugin_build", HERE / "plugins" / "build.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class PackageTest(unittest.TestCase):
    def test_the_package_checks_pass(self):
        self.assertEqual(load_build().check(PLUGIN), [])

    def test_the_zip_holds_the_plugin_at_its_root(self):
        build = load_build()
        with tempfile.TemporaryDirectory() as out:
            target = build.build(PLUGIN, pathlib.Path(out))
            names = set(zipfile.ZipFile(target).namelist())
        manifest = json.loads((PLUGIN / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(target.name, "TOOLS-%s.zip" % manifest["version_number"])
        self.assertTrue({"manifest.json", "icon.png", "README.md", "CHANGELOG.md", "reskate-plugin.json",
                         "plugin/main.lua", "plugin/version.lua", "plugin/hud/client.lua", "plugin/hud/server.lua",
                         "plugin/lib/base64.lua"} <= names, names)

    def test_version_lua_matches_the_manifest(self):
        manifest = json.loads((PLUGIN / "manifest.json").read_text(encoding="utf-8"))
        source = (PLUGIN / "plugin" / "version.lua").read_text(encoding="utf-8")
        self.assertIn('return "%s"' % manifest["version_number"], source)

    def test_a_bad_permission_is_caught(self):
        build = load_build()
        with tempfile.TemporaryDirectory() as tmp:
            folder = pathlib.Path(tmp)
            for path in build.files(PLUGIN):
                target = folder / path.relative_to(PLUGIN)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(path.read_bytes())
            manifest = json.loads((folder / "reskate-plugin.json").read_text())
            manifest["permissions"].append("teleport.everyone")
            (folder / "reskate-plugin.json").write_text(json.dumps(manifest))
            self.assertEqual(build.check(folder), ["reskate-plugin.json: unknown permission 'teleport.everyone'"])


# ---- the stand-in for ReSkate ------------------------------------------------------------------------

PRELUDE = r"""
local py = ...
local V = {}
V.__index = V
local function isv(x) return type(x) == "table" and x.__vector end
V.__add = function(a, b) return vector.create(a.x + b.x, a.y + b.y, a.z + b.z) end
V.__sub = function(a, b) return vector.create(a.x - b.x, a.y - b.y, a.z - b.z) end
V.__mul = function(a, b)
    if isv(a) then return vector.create(a.x * b, a.y * b, a.z * b) end
    return vector.create(b.x * a, b.y * a, b.z * a)
end
vector = {create = function(x, y, z) return setmetatable({x = x, y = y, z = z, __vector = true}, V) end}
function math.clamp(v, lo, hi) return math.max(lo, math.min(hi, v)) end
function table.find(t, value) for i, v in ipairs(t) do if v == value then return i end end return nil end

local handlers, timers, next_timer = {}, {}, 1
function fire(name, ...)
    for _, f in ipairs(handlers[name] or {}) do f(...) end
end
function run_timers(t)
    for id, timer in pairs(timers) do
        if t >= timer.at then
            if timer.every then timer.at = timer.at + timer.every else timers[id] = nil end
            timer.f()
        end
    end
end
-- The plugin's own modules (require("hud/client") is plugin/hud/client.lua), each run once.
local loaded = {}
function require(name)
    if loaded[name] == nil then
        local chunk = assert(load(py.source(name), "=" .. name))
        local value = chunk()
        if value == nil then value = true end
        loaded[name] = value
    end
    return loaded[name]
end
-- Console commands: console_line("coco status") runs one as the server's console does.
local commands = {}
function console_line(line)
    local words = {}
    for w in line:gmatch("%S+") do table.insert(words, w) end
    local f = commands[table.remove(words, 1)]
    if f then f(words) end
end
menu_page = nil
screens = {}   -- reskate.ui.screen's functions by name: the game calls one every frame it is open
local function v(x, y, z) return vector.create(x, y, z) end
-- The draw handler's d: d:size() and a counted shape for anything else.
canvas = setmetatable({}, {__index = function(_, name)
    if name == "size" then return function() return v(1920, 1080, 0) end end
    if name == "measure" then
        return function(_, text, style) local size = (style and style.size) or 20 return v(#text * size * 0.5, size, 0) end
    end
    return function() py.shape() end
end})
local storage = {}

reskate = {
    side = py.side,
    on = function(name, f) handlers[name] = handlers[name] or {} table.insert(handlers[name], f) end,
    time = {
        now = function() return py.now() end,
        after = function(ms, f) local id = next_timer next_timer = id + 1 timers[id] = {at = py.now() + ms, f = f} return id end,
        every = function(ms, f) local id = next_timer next_timer = id + 1 timers[id] = {at = py.now() + ms, every = ms, f = f} return id end,
        cancel = function(id) timers[id] = nil end,
    },
    console = {
        command = function(name, help, f) commands[name] = f py.call("command " .. name) end,
        print = function(text) py.call("print " .. text) end,
    },
    menu = {
        page = function(title, build) menu_page = build py.call("page " .. title) end,
        game = function() return {open = py.menu_open(), skaters = 0, buttons = {}} end,
    },
    server = {
        say = function(text) py.call("say " .. text) end,
        tell = function(id, text) py.call("tell " .. id .. " " .. text) end,
        move = function(id, at) return py.move(id, at.x, at.y, at.z) end,
    },
    json = {encode = function(value) return py.encode(value) end, decode = function(text) return py.decode(text) end},
    net = {send = function(to, payload) return py.send(to, payload) end},
    storage = {
        get = function(key) return storage[key] end,
        keys = function() local out = {} for k in pairs(storage) do table.insert(out, k) end return out end,
        set = function(key, value) py.call("storage " .. key) storage[key] = py.decode(py.encode(value)) end,
    },
    level = {current = function()
        local name = py.level()
        if name then return {name = name} end
        return nil
    end},
    session = {
        me = function() return {id = py.id, name = py.name, me = true} end,
        world = function() return {world = "1", map = py.map(), barred = false} end,
        active = function() return true end,
        is_dedicated = function() return true end,
        players = function() return py.players() end,
    },
    world = {raycast = function(from, to)
        if from.y >= 0 and to.y <= 0 then return v(from.x, 0, from.z) end
        return nil
    end},
    skater = {
        position = function() local p = py.pos() return v(p[1], p[2], p[3]) end,
        get = function() local p = py.pos() return {position = v(p[1], p[2], p[3]), heading = py.heading, speed = 0} end,
        teleport = function(at, heading) return py.teleport(at.x, at.y, at.z, heading) end,
        hold = function(on) py.call("hold " .. tostring(on)) return true end,
    },
    physics = {
        competition = function(on) py.call("competition " .. tostring(on)) end,
        prepare = function() end,
    },
    throwdowns = {
        mode = function(m) py.call("mode " .. m.id) return true end,
        ui = function(doc) py.publish(doc) return true end,
        flag = {leave = function() py.call("flag leave") end,
                state = function() local st = py.flag_state() if st then return st end return {busy = false} end},
    },
    ui = {
        toast = function(text) py.toast(text) return true end,
        screen = function(name, fn, options)
            if type(fn) ~= "function" then error("function expected") end
            screens[name] = fn
            py.call("screen " .. name .. ((options and options.panel == false) and " (no panel)" or ""))
        end,
        open = function(name) py.call("open " .. name) return true end,
        close = function(name) py.call("close " .. name) end,
        dialog = function(id, spec) py.dialog(id, spec) return true end,
        close_dialog = function(id) end,
    },
    audio = {play_ui = function(name) py.call("ui sound " .. name) return true end},
    http = {request = function(request, done)
        local status, body = py.http(request.url)
        reskate.time.after(50, function() done(status, body) end)
        return 1
    end},
    entities = {blueprints = function() return {"Gameplay/Challenges/ChallengeStart_Marker", "Props/Bench_01"} end},
    log = function(text, level) py.call("log " .. text) end,
    input = {
        bind = function(action, key) py.call("bind " .. action .. " " .. key) return true end,
        focus = function() return {focused = true, interface_open = false, gameplay = true} end,
        key = function(code)
            if type(code) ~= "number" or code < 1 or code > 254 then error("a virtual key from 1 to 254") end
            return py.key_down(code)
        end,
        cursor = function()
            local c = py.cursor()
            if not c then return nil end
            return {position = vector.create(c.x, c.y, 0), left = c.left, over_screen = false}
        end,
        pad_bits = function() return py.pad_bits() end,
        gamepad = function()
            local buttons = {}
            for _, name in ipairs({"a", "b", "x", "y", "up", "down", "left", "right", "lb", "rb", "start", "back"}) do
                buttons[name] = py.pad_button(name)
            end
            return {buttons = buttons, left_x = 0, left_y = 0}
        end,
        game_buttons = function(mask) py.call("game_buttons " .. mask) end,
        hide = function(flags) py.call("hide " .. tostring(next(flags or {}) ~= nil)) end,
        game_keys = function(keys)
            local out = {}
            for _, k in ipairs(keys) do
                if k == 0x1B then error("Escape and the mouse's buttons always reach the game") end
                out[#out + 1] = tostring(k)
            end
            py.call("game_keys " .. table.concat(out, ","))
        end,
    },
}
"""


def luau_to_lua(source: str) -> str:
    """The Luau the plugin uses that Lua 5.4 lacks: compound assignment."""
    return re.sub(r"^(\s*)([\w.\[\]]+)\s*\+=\s*(.+)$", r"\1\2 = \2 + (\3)", source, flags=re.M)


SOURCE = luau_to_lua((PLUGIN / "plugin" / "main.lua").read_text(encoding="utf-8"))
# The player's state is in locals of the chunk: a function at its end reads them.
STATE = ("\nfunction plugin_state() return {race = race, joined = joined, host = host, editor = editor, "
         "tracks = tracks, staff = staff} end\n")


class Session:
    def __init__(self, map_name="Skate 3 Improved"):
        self.now = 1_000_000.0
        self.map = map_name
        self.level = None   # reskate.level.current().name (None: no level known)
        self.bus = []
        self.clients = []
        self.staff = set()
        self.bare = []   # players without the plugin: on the server, but nothing runs for them
        self.server = Client(self, "0", "Server", side="server")

    def add(self, steam_id, name, staff=False):
        if staff:
            self.staff.add(steam_id)
        client = Client(self, steam_id, name)
        self.clients.append(client)
        return client

    def deliver(self):
        bus, self.bus = self.bus, []
        for sender, to, payload in bus:
            if sender is not self.server:
                if to in ("all", "server"):
                    self.server.receive(sender, payload)
                if to == "server":
                    continue
            for client in self.clients:
                if client is not sender and (to == "all" or to == client.id):
                    client.receive(sender, payload)

    def step(self, ms=100):
        self.now += ms
        for _ in range(3):  # a message answered within the step reaches its sender in it too
            self.deliver()
        for client in [self.server] + self.clients:
            client.lua.globals().run_timers(self.now)
            client.lua.globals().fire("tick", ms / 1000)
            if client is not self.server:
                client.draw()

    def settle(self, steps=5):
        for _ in range(steps):
            self.step()


class Client:
    def __init__(self, session, steam_id, name, side="client"):
        self.session, self.id, self.name, self.side = session, steam_id, name, side
        self.position = [0.0, 0.0, 0.0]
        self.can_teleport = True
        self.can_move = True     # the server's copy: whether reskate.server.move moves players
        self.keys_down = set()   # Windows virtual-key codes held
        self.cursor = None       # reskate.input.cursor: {x, y, left} in screen pixels, or none (no cursor)
        self.flag_state = None   # reskate.throwdowns.flag.state(), when set
        self.pad_bits = 0        # the controller's buttons, XInput bits (None: pad_bits has nothing, as under a screen)
        self.pad_buttons = set()  # reskate.input.gamepad().buttons held, by name
        self.calls, self.toasts, self.doc, self.dialogs, self.oversize = [], [], {}, [], []
        self.shapes = 0
        self.menu_open = False
        self.lua = lupa.LuaRuntime(unpack_returned_tuples=True)
        py = self.lua.table_from({
            "id": steam_id, "name": name, "side": side, "http": self.http, "map": lambda: session.map, "level": lambda: session.level, "heading": 1.25,
            "now": lambda: session.now, "encode": self.encode, "decode": self.decode, "send": self.send,
            "pos": lambda: self.lua.table_from(self.position), "teleport": self.teleport, "move": self.move,
            "key_down": lambda code: code in self.keys_down,
            "cursor": lambda: self.lua.table_from(self.cursor) if self.cursor else None,
            "pad_bits": lambda: self.pad_bits,
            "flag_state": lambda: self.lua.table_from(self.flag_state) if self.flag_state else None,
            "pad_button": lambda name: name in self.pad_buttons,
            "call": self.calls.append, "publish": self.publish, "toast": self.toasts.append,
            "shape": self.count_shape, "dialog": lambda id, spec: self.dialogs.append((id, self.to_py(spec))),
            "source": lambda name: luau_to_lua((PLUGIN / "plugin" / (name + ".lua")).read_text(encoding="utf-8")),
            "players": self.players, "menu_open": lambda: self.menu_open,
        })
        self.lua.execute(PRELUDE, py)
        self.lua.execute(SOURCE if side == "server" else SOURCE + STATE)
        self.lua.globals().fire("load")

    # -- conversion between Lua tables and JSON
    def to_py(self, value):
        if lupa.lua_type(value) != "table":
            return value
        if value["__vector"]:
            return [value["x"], value["y"], value["z"]]
        keys = list(value.keys())
        if keys and all(isinstance(k, int) for k in keys) and sorted(keys) == list(range(1, len(keys) + 1)):
            return [self.to_py(value[k]) for k in sorted(keys)]
        return {str(k): self.to_py(value[k]) for k in keys}

    def to_lua(self, value):
        if isinstance(value, list):
            return self.lua.table_from([self.to_lua(v) for v in value])
        if isinstance(value, dict):
            return self.lua.table_from({k: self.to_lua(v) for k, v in value.items()})
        return value

    def encode(self, value):
        return json.dumps(self.to_py(value))

    def decode(self, text):
        return self.to_lua(json.loads(text))

    def send(self, to, payload):
        if len(payload) > 4082:  # ReSkate refuses it with an error
            self.oversize.append(len(payload))
            raise RuntimeError("a message is at most 4082 bytes")
        self.session.bus.append((self, to, payload))
        return True

    def receive(self, sender, payload):
        if sender is self.session.server:
            player = self.lua.table_from({"id": "0", "name": "Server", "server": True})
        else:
            player = self.lua.table_from({"id": sender.id, "name": sender.name})
            if self.side == "server" and sender.id in self.session.staff:
                player["role"] = self.lua.table_from({"name": "admin", "staff": True})
        self.lua.globals().fire("message", player, payload)

    def teleport(self, x, y, z, heading):
        if not self.can_teleport:
            return False, "the host has turned teleporting off"
        self.position = [x, y, z]
        self.facing = heading
        self.calls.append("teleport")
        return True

    HUB = {
        "board?league=ranked": {"total": 7, "rows": [{"rank": 3, "name": "Coco", "points": 1200, "wins": 4, "played": 9}]},
        "board?league=unranked": {"total": 2, "rows": [{"rank": 1, "name": "Jambo", "points": 30, "wins": 1, "played": 1}]},
        "teams": {"teams": [{"tag": "COJA", "name": "Cocojambo", "points": {"ranked": 1200, "unranked": 0},
                             "members": [{"name": "Coco", "role": "owner"}]}]},
        "servers": {"servers": [{"server": "1", "online": True, "players": 3}, {"server": "2", "online": False, "players": 0}]},
    }

    def http(self, url):
        """reskate.http.request against the hub: (status, body)."""
        self.calls.append("http " + url)
        prefix = "https://reskate.cocojambo.net/status/api/"
        answer = self.HUB.get(url[len(prefix):]) if url.startswith(prefix) else None
        return (200, json.dumps(answer)) if answer else (404, "{}")

    def move(self, steam_id, x, y, z):
        """reskate.server.move, from the server's copy: the player is put there."""
        if not self.can_move:
            return False, "moving players is off"
        for client in self.session.clients:
            if client.id == steam_id:
                client.position = [x, y, z]
                self.calls.append("move " + steam_id)
                return True
        return False, "no such player"

    def count_shape(self):
        self.shapes += 1

    def players(self):
        """The session's players, as reskate.session.players() gives them (staff with their role)."""
        rows = []
        for c in self.session.clients + self.session.bare:
            row = {"id": c.id, "name": c.name}
            if c.id in self.session.staff:
                row["role"] = self.lua.table_from({"name": "admin", "rank": 100, "staff": True})
            else:
                row["role"] = self.lua.table_from({"name": "player", "rank": 0, "staff": False})
            rows.append(self.lua.table_from(row))
        return self.lua.table_from(rows)

    def console(self, line):
        self.lua.globals().console_line(line)

    def hud(self):
        return self.to_py(self.lua.globals().require("hud/client").state())

    def publish(self, doc):
        self.doc = self.to_py(doc)
        return True

    def draw(self):
        self.lua.globals().fire("draw", self.lua.globals().canvas)

    # -- what a player does
    def ui(self, action, *args):
        self.lua.globals().fire("throwdown_ui", self.lua.table_from({
            "action": action, "mode": "deathraces", "args": self.lua.table_from([str(a) for a in args])}))

    def key(self, action):
        self.lua.globals().fire("action", action)

    def state(self, name):
        return self.lua.globals().plugin_state()[name]

    def race(self):
        return self.state("race")

    def tracks(self):
        tracks = self.to_py(self.state("tracks"))
        return tracks if isinstance(tracks, list) else []

    def gates(self):
        gates = self.to_py(self.state("editor"))["track"]["gates"]
        return gates if isinstance(gates, list) else []


def skate(session, client, target, speed=12.0):
    """Moves `client` to `target` at `speed` m/s, the session ticking every 100 ms."""
    target = [float(c) for c in target]
    while True:
        dx = [t - p for t, p in zip(target, client.position)]
        d = math.sqrt(sum(c * c for c in dx))
        stepped = speed * 0.1
        if d <= stepped:
            client.position = list(target)
            session.step()
            return
        client.position = [p + c / d * stepped for p, c in zip(client.position, dx)]
        session.step()


# A square of 40 m: the gates in the middle of its sides, gone through anticlockwise from above.
CORNERS = [(40, 0, 0), (40, 0, 40), (0, 0, 40), (0, 0, 0)]
GATES = [(20, 0, 0), (40, 0, 20), (20, 0, 40), (0, 0, 20)]


def build_track(session, admin, name="Harbour Loop", circuit=True):
    """The admin builds the square in the editor: a gate at the middle of each side."""
    admin.position = [0.0, 0.0, 0.0]
    session.settle()
    admin.key("race_editor")
    admin.key("gate_add")
    for gate, corner in zip(GATES, CORNERS):
        skate(session, admin, gate)
        admin.key("gate_add")
        skate(session, admin, corner)
    if circuit:
        admin.key("gate_circuit")
    admin.key("race_editor")
    assert admin.dialogs[-1][0] == "save_track", admin.dialogs
    admin.ui("dialog", "save_track", "save", name, 0)
    session.settle()
    admin.key("race_editor")  # close Track Studio after the save returns to its library


def host_and_join(session, host, joiner, laps_choice=1):
    host.ui("open", 7)
    session.step()
    host.ui("setup_row", "laps", laps_choice)
    host.ui("setup_confirm", 4, 0)
    host.ui("flag_placed", 7, 1, 2, 3, 0)
    session.step()
    flag = joiner.doc["flags"][0]
    joiner.ui("join", flag["id"], flag["leader"], 7)
    session.step()


MENU_UI = """
local tab, click, choose, log = ...
local ui = {}
function ui:tabs(id, names, at)
    for _, n in ipairs(names) do log("tab " .. n) end
    for i, n in ipairs(names) do if n == tab then return i end end
    return at
end
function ui:card(title) log("card " .. title) end
function ui:info(label, value) log("info " .. label .. ": " .. tostring(value)) end
function ui:note(text) log("note " .. text) end
function ui:separator() end
function ui:button(id, text) log("button " .. id) return id == click end
function ui:toggle(id, text, on) log("toggle " .. id) if id == click then return not on end return on end
function ui:dropdown(id, text, choices, chosen) log("dropdown " .. id .. " " .. #choices) return choose[id] or chosen end
function ui:slider(id, text, value) return choose[id] or value end
return ui
"""


def menu(client, tab, click=None, **choose):
    """Builds tab `tab` of the COJA menu (F5), clicking the button `click` and choosing `choose`; returns what it
    showed, or None when the player has no such tab."""
    shown = []
    ui = client.lua.execute(MENU_UI, tab, click, client.lua.table_from(choose), shown.append)
    if not client.lua.globals().require("hud/menu").build(tab, ui):
        return None
    return shown


def lights_out(session):
    for _ in range(90):  # the grid and the game's 3-2-1
        session.step()


@unittest.skipUnless(lupa, "needs lupa (pip install lupa)")
class RaceTest(unittest.TestCase):
    def setUp(self):
        self.session = Session()
        self.a = self.session.add("76561198000000001", "Coco", staff=True)
        self.b = self.session.add("76561198000000002", "Jambo")

    def tearDown(self):
        for client in [self.session.server] + self.session.clients:
            self.assertEqual(client.oversize, [], client.name + " sent a message over 4 KB")
            self.assertFalse([c for c in client.calls if c.startswith("log ")], client.name)

    def test_loading_registers_the_mode_and_its_keys(self):
        self.assertIn("mode deathraces", self.a.calls)
        self.assertIn("bind race_editor F7", self.a.calls)
        self.assertIn("bind adjust_right Pad.RB+Pad.Right", self.a.calls)   # the keyboard keys are polled
        self.assertIn("bind hud_panel F4", self.a.calls)
        self.assertEqual(sum(c.startswith("bind ") for c in self.a.calls), 15, "at most 16 actions")

    def test_the_server_tells_players_whether_they_are_staff(self):
        self.session.settle()
        self.assertTrue(self.a.state("staff"))
        self.assertFalse(self.b.state("staff"))
        self.b.key("race_editor")
        self.assertIn("Only admins make race tracks.", self.b.toasts)

    def test_an_admin_builds_a_track_and_everyone_gets_it(self):
        build_track(self.session, self.a)
        for client in (self.a, self.b):
            tracks = client.tracks()
            self.assertEqual(len(tracks), 1, client.name)
            track = tracks[0]
            self.assertEqual(track["name"], "Harbour Loop")
            self.assertTrue(track["circuit"])
            facing = [[1, 0], [0, 1], [-1, 0], [0, -1]]
            self.assertEqual([g["n"] for g in track["gates"]], facing, "facing the way skated")
            self.assertEqual([g["p"] for g in track["gates"]],
                             [[x + 3.0 * n[0], float(y), z + 3.0 * n[1]] for (x, y, z), n in zip(GATES, facing)],
                             "3 m ahead of the admin")
            self.assertEqual(track["author"], "Coco")
        self.assertIn("Track \"Harbour Loop\" saved for everyone.", self.a.toasts)
        late = self.session.add("76561198000000003", "Late")
        self.session.settle()
        self.assertEqual(late.tracks()[0]["name"], "Harbour Loop", "a player who comes later gets it too")

    def test_a_gate_placed_in_the_air_stands_on_the_ground(self):
        self.session.settle()
        self.a.key("race_editor")
        self.a.ui("dialog", "tracks", "new", "", 0)
        self.a.position = [5.0, 3.2, 0.0]          # mid-ollie: the ground (the stand-in's) is at y = 0
        self.a.key("gate_add")
        self.a.position = [9.0, 2.0, 0.0]
        self.a.key("gate_add")
        self.a.key("gate_here")                     # 6: the selected one, moved to here, lands too
        self.assertEqual([g["p"][1] for g in self.a.gates()], [0, 0])

    def test_the_editor_moves_turns_and_sizes_a_gate(self):
        self.session.settle()
        self.a.key("race_editor")
        self.a.ui("dialog", "tracks", "new", "", 0)
        skate(self.session, self.a, (10, 0, 0))
        self.a.key("gate_add")
        self.a.key("adjust_up")       # MOVE: forward
        self.a.key("adjust_right")    # MOVE: right
        self.assertEqual(self.a.gates()[0]["p"], [13.5, 0.0, -0.5])   # placed 3 m ahead
        self.a.key("adjust_mode")     # HEIGHT & TURN
        self.a.key("adjust_up")
        self.a.key("adjust_left")
        self.assertEqual(self.a.gates()[0]["p"][1], 0.25)
        self.assertNotEqual(self.a.gates()[0]["n"], [1, 0], "turned")
        self.a.key("adjust_mode")     # SIZE
        self.a.key("adjust_up")
        self.a.key("adjust_left")
        self.assertEqual((self.a.gates()[0]["w"], self.a.gates()[0]["h"]), (7.5, 5.5))
        for _ in range(40):
            self.a.key("adjust_left")
        self.assertEqual(self.a.gates()[0]["w"], 2, "not narrower than 2 m")
        skate(self.session, self.a, (30, 0, 0))
        self.a.key("gate_here")
        self.assertEqual(self.a.gates()[0]["p"], [33.0, 0.0, 0.0])   # moved to 3 m ahead
        self.assertEqual(self.a.gates()[0]["w"], 2, "keeps its size")
        self.a.key("gate_add")
        self.a.key("gate_prev")
        self.a.key("gate_delete")
        self.assertEqual(len(self.a.gates()), 1)
        self.assertGreater(self.a.shapes, 0)

    def test_a_player_who_is_not_staff_cannot_save_or_delete(self):
        build_track(self.session, self.a)
        track = self.b.tracks()[0]
        packed = {"name": "Mine", "circuit": False, "g": [g["p"] + g["n"] + [g["w"], g["h"], 0] for g in track["gates"]]}
        self.session.bus.append((self.b, "server", json.dumps({"t": "delete", "map": self.session.map, "id": track["id"]})))
        self.session.bus.append((self.b, "server", json.dumps({"t": "save", "map": self.session.map, "track": packed})))
        self.session.settle()
        self.assertEqual([t["name"] for t in self.a.tracks()], ["Harbour Loop"])

    def test_an_admin_deletes_a_track(self):
        build_track(self.session, self.a)
        self.a.key("race_editor")
        self.a.ui("dialog", "tracks", "delete", "", 1)
        delete = self.a.dialogs[-1][1]["buttons"][0]["id"]
        self.a.ui("dialog", "delete_track", delete, "", 0)
        self.session.settle()
        self.assertEqual(self.b.tracks(), [])

    def test_the_setup_page_and_the_flag_show_the_track(self):
        build_track(self.session, self.a)
        self.a.ui("open", 7)
        self.session.step()
        rows = self.a.doc["setup"]["rows"]
        self.assertEqual(rows[0]["options"], ["Harbour Loop"])
        self.assertEqual(rows[3]["id"], "laps", "a circuit has laps")
        host_and_join(self.session, self.a, self.b)
        self.session.settle(6)
        flag = self.b.doc["flags"][0]
        self.assertEqual([s["title"] for s in flag["stats"]], ["TRACK", "SETUP", "GATES"], "its card on the map")
        self.assertEqual(flag["players"], 2)

    def test_without_a_track_the_flag_is_let_go(self):
        self.session.settle()
        self.a.ui("open", 7)
        self.session.step()
        self.assertEqual(self.a.doc["setup"]["rows"][0]["label"], "No tracks saved for this map")
        self.assertEqual(self.a.doc["setup"]["rows"][1]["id"], "refresh_tracks")
        self.a.ui("setup_confirm", 4, 0)
        self.assertIn("flag leave", self.a.calls)

    def test_a_two_lap_race_from_the_lights_to_the_results(self):
        build_track(self.session, self.a)
        host_and_join(self.session, self.a, self.b, laps_choice=1)
        self.a.ui("start", 7)
        for _ in range(4):
            self.session.step()
        for client in (self.a, self.b):
            self.assertLess(client.position[0], 20, client.name + " is on the grid behind the start")
            self.assertTrue(client.doc["match"]["input_lock"], "held for the lights")
            self.assertTrue(client.doc["hud"]["active"], "teleports are the mode's while its HUD is active")
            countdown = client.doc["hud"]["countdown"]
            self.assertTrue(0 < countdown["remaining_ms"] <= countdown["total_ms"] and countdown["ticking"], "the game's 3-2-1-GO")
            self.assertEqual(client.doc["hud"]["intro"]["title"], "HARBOUR LOOP")
            self.assertIn("hold true", client.calls, "held on the grid")
            self.assertIn("competition true", client.calls)
        self.assertNotEqual(self.a.position, self.b.position, "side by side")
        self.assertAlmostEqual(self.a.position[0], self.b.position[0], msg="a fair start: the same distance behind the gate")
        self.assertAlmostEqual(self.a.position[2], -self.b.position[2], msg="spread evenly across it")
        for client in (self.a, self.b):   # gate 1 is gone through along +x: heading pi/2 faces (sin, cos) = (1, 0)
            self.assertAlmostEqual(client.facing, math.pi / 2, places=2, msg=client.name + " faces along the start gate")
        lights_out(self.session)
        self.assertEqual(self.a.race().phase, "running")
        self.assertEqual(self.a.race().started_at, self.a.race().go_at, "a race's clock runs from GO for everyone")
        self.assertFalse(self.a.doc["match"]["input_lock"])
        self.assertNotIn("countdown", self.a.doc["hud"], "GO: the game shows it as the countdown ends")
        for client in (self.a, self.b):
            self.assertIn("hold false", client.calls, "let go at GO")
            self.assertEqual(client.calls.count("ui sound countdown"), 3, "a beep on 3, 2 and 1")
            self.assertIn("ui sound go", client.calls)
        for corner in CORNERS * 2 + [CORNERS[0]]:
            skate(self.session, self.a, corner)
        mine = self.a.race().racers[self.a.id]
        self.assertIsNotNone(mine.ms, "A went through all nine gates")
        self.assertIsNotNone(mine.best, "and has a best lap")
        self.assertIsNotNone(self.b.race().racers[self.a.id].ms, "B hears A finish")
        for corner in CORNERS * 2 + [CORNERS[0]]:
            skate(self.session, self.b, corner, speed=9.0)
        for client in (self.a, self.b):
            race = client.race()
            self.assertEqual(race.phase, "done", client.name)
            self.assertLess(race.racers[self.a.id].ms, race.racers[self.b.id].ms)
        self.assertNotIn("match", self.a.doc)
        self.assertIn("competition false", self.a.calls)
        self.a.key("race_leave")
        self.assertIsNone(self.a.race(), "F10 closes the results")
        for _ in range(260):
            self.session.step()
        self.assertIsNone(self.b.race(), "the results go after 25 s")

    def test_a_challenge_is_run_alone_and_its_record_kept(self):
        build_track(self.session, self.a)
        track_id = self.a.tracks()[0]["id"]
        self.b.ui("dialog", "tracks", "challenge", "", 1)          # not staff: refused
        self.session.settle()
        self.assertIn("Only admins set challenges.", self.b.toasts)
        self.a.key("race_editor")
        self.a.ui("dialog", "tracks", "challenge", "", 1)
        self.session.settle()
        for client in (self.a, self.b):
            self.assertTrue(client.tracks()[0]["challenge"], client.name)
        # far from the start, F8 does nothing; by it, it starts a run with only this player
        self.b.position = [100.0, 0.0, 100.0]
        self.b.key("gate_add")
        self.assertIsNone(self.b.race())
        self.b.position = [16.0, 0.0, 2.0]
        self.b.key("gate_add")
        race = self.b.race()
        self.assertTrue(race.challenge)
        self.assertEqual(list(race.racers.keys()), [self.b.id])
        lights_out(self.session)
        self.assertIsNone(self.b.race().started_at, "a challenge's clock waits for the start gate")
        self.b.key("race_leave")
        self.b.position = [16.0, 0.0, 2.0]
        self.b.key("gate_add")
        race = self.b.race()
        self.assertIsNone(self.a.race(), "nobody else is in it")
        lights_out(self.session)
        for corner in CORNERS + [CORNERS[0]]:
            skate(self.session, self.b, corner)
        self.session.settle()
        mine = self.b.race().racers[self.b.id]
        self.assertIsNotNone(mine.ms)
        self.assertEqual(self.b.race().phase, "done")
        prints = [c for c in self.session.server.calls if c.startswith("print [coco race] challenge ")]
        self.assertEqual(len(prints), 1, self.session.server.calls)
        self.assertIn(f" {track_id} {int(mine.ms)} {self.b.id} Jambo", prints[0])
        self.assertTrue(any(t.startswith("New record on Harbour Loop: Jambo") for t in self.a.toasts), self.a.toasts)
        self.assertTrue(any(t.startswith("Harbour Loop: ") and "#1" in t for t in self.b.toasts), self.b.toasts)
        self.assertEqual(self.a.tracks()[0]["best"]["n"], "Jambo", "the record shows on the marker")
        # a slower run is not kept, and is no record
        self.b.key("race_leave")
        self.b.position = [16.0, 0.0, 2.0]
        self.b.key("gate_add")
        lights_out(self.session)
        for corner in CORNERS + [CORNERS[0]]:
            skate(self.session, self.b, corner, speed=6.0)
        self.session.settle()
        self.assertTrue(any("(not your best)" in t for t in self.b.toasts), self.b.toasts)
        self.assertEqual(sum(t.startswith("New record") for t in self.a.toasts), 1)
        # moving the course's gates starts its times over
        self.a.key("race_editor")
        self.a.ui("dialog", "tracks", "edit", "", 1)
        self.a.key("gate_next")
        self.a.key("adjust_up")
        self.a.key("race_editor")
        self.a.ui("dialog", "save_track", "save", "Harbour Loop", 0)
        self.session.settle()
        self.assertIsNone(self.b.tracks()[0].get("best"))
        self.assertTrue(self.b.tracks()[0]["challenge"], "it stays a challenge")

    def test_f8_on_the_keyboard_starts_a_challenge(self):
        build_track(self.session, self.a)
        self.a.key("race_editor")
        self.a.ui("dialog", "tracks", "challenge", "", 1)
        self.session.settle()
        self.b.position = [16.0, 0.0, 2.0]
        self.b.keys_down.add(0x77)      # F8, by its virtual-key code
        self.session.step()
        self.assertTrue(self.b.race().challenge)
        self.session.step()             # held: not again
        self.b.keys_down.clear()
        self.session.step()
        self.assertFalse(any("diag Jambo: input.key" in c for c in self.session.server.calls), self.session.server.calls)

    def test_the_setup_page_picks_the_track_by_its_label(self):
        build_track(self.session, self.a)
        build_track(self.session, self.a, name="Dock Sprint", circuit=False)
        self.a.ui("open", 7)
        self.session.step()
        self.assertEqual(self.a.doc["setup"]["rows"][0]["options"], ["Harbour Loop", "Dock Sprint"])
        self.a.ui("setup_row", "track", 0, "Dock Sprint")
        self.a.ui("setup_confirm", 4, 0)
        self.a.ui("flag_placed", 7, 1, 2, 3, 0)
        self.assertEqual(self.a.state("host").track.name, "Dock Sprint")
        self.assertIn("Race on Dock Sprint  -  Sprint · 2.5 s grid", self.a.toasts)
        self.session.step()
        self.assertTrue(any(c.startswith("print [coco race] diag Coco: setup_row") and "Dock Sprint" in c
                            for c in self.session.server.calls), self.session.server.calls)

    def test_the_menu_hosts_a_race_on_the_chosen_track(self):
        build_track(self.session, self.a)
        build_track(self.session, self.a, name="Dock Sprint", circuit=False)
        shown = menu(self.a, "RACES", click="race_flag", race_track=2, race_grid=3)
        self.assertIn("dropdown race_track 2", shown)
        self.assertNotIn("dropdown race_laps 4", shown, "a sprint has no laps")
        self.assertTrue(any(t.startswith("Open Throwdowns > Cocojambo Deathraces") for t in self.a.toasts), self.a.toasts)
        self.a.ui("flag_placed", 7, 1, 2, 3, 0)
        self.assertEqual(self.a.state("host").track.name, "Dock Sprint")
        self.assertEqual(self.a.state("host").grid_delay, 5000)
        self.session.step()
        flag = self.b.doc["flags"][0]
        self.b.ui("join", flag["id"], flag["leader"], 7)
        self.session.step()
        self.assertIn("info Racers: 2 / 6", menu(self.a, "RACES"))
        menu(self.a, "RACES", click="race_start")
        self.session.step()
        for client in (self.a, self.b):
            self.assertEqual(client.race().track.name, "Dock Sprint", client.name)

    def test_the_menu_runs_a_challenge_from_anywhere(self):
        build_track(self.session, self.a)
        self.a.key("race_editor")
        self.a.ui("dialog", "tracks", "challenge", "", 1)
        self.session.settle()
        self.b.position = [300.0, 0.0, 300.0]
        shown = menu(self.b, "RACES", click="challenge_1")
        self.assertTrue(any(line.startswith("info Harbour Loop: no record yet") for line in shown), shown)
        self.assertTrue(self.b.race().challenge)
        self.session.settle()
        self.assertLess(math.dist(self.b.position, [16.0, 0.0, 0.0]), 6, "put on its start")

    def test_track_studio_in_the_menu_is_for_staff(self):
        self.session.settle()
        self.assertIsNone(menu(self.b, "ADMIN"), "not staff")
        self.assertIsNotNone(menu(self.a, "ADMIN"))
        menu(self.a, "ADMIN", click="studio_new")
        for x in (10.0, 30.0, 50.0):
            skate(self.session, self.a, (x, 0, 0))
            menu(self.a, "ADMIN", click="gate_add")
        self.assertEqual(len(self.a.gates()), 3)
        before = self.a.gates()[2]["p"][1]
        menu(self.a, "ADMIN", click="raise")
        self.assertAlmostEqual(self.a.gates()[2]["p"][1], before + 0.25)
        menu(self.a, "ADMIN", click="gate_delete")
        self.assertEqual(len(self.a.gates()), 2)
        menu(self.a, "ADMIN", click="studio_save")
        self.assertEqual(self.a.dialogs[-1][0], "save_track")
        self.a.ui("dialog", "save_track", "save", "Menu Sprint", 0)
        self.session.settle()
        self.assertEqual([t["name"] for t in self.b.tracks()], ["Menu Sprint"])

    def tap(self, client, key=None, bit=None):
        """A key (virtual-key code) or a controller button (XInput bit) pressed and let go, a tick each."""
        if key:
            client.keys_down.add(key)
        if bit:
            client.pad_bits = bit
        self.session.step()
        client.keys_down.clear()
        client.pad_bits = 0
        self.session.step()

    def test_f5_opens_the_menu_over_the_game_and_every_tab_draws(self):
        build_track(self.session, self.a)
        menu_lua = self.a.lua.globals().require("hud/menu")
        self.assertIn("screen COJA (no panel)", self.a.calls, "the game draws nothing over the menu")
        self.a.key("coja_menu")
        self.assertTrue(menu_lua.open)
        self.assertIn("open COJA", self.a.calls, "the screen: the cursor and the input are the plugin's")
        self.assertIn("hold true", self.a.calls, "the skater is held")
        for tab in ("HOME", "RACES", "ADMIN", "SETTINGS"):
            menu_lua.show(tab)
            before = self.a.shapes
            self.session.step()
            self.assertGreater(self.a.shapes - before, 20, tab)
        self.a.key("gate_add")                                   # the editor's binds wait while it is open
        self.assertIsNone(self.a.state("editor"))
        self.tap(self.a, key=0x1B)                               # Esc
        self.assertFalse(menu_lua.open)
        self.assertIn("close COJA", self.a.calls)
        self.assertIn("hold false", self.a.calls)
        reports = [c for c in self.session.server.calls if "diag Coco: F5 menu:" in c]
        self.assertTrue(any("hold input true" in c for c in reports), reports)
        self.assertFalse([c for c in reports if "menu tab" in c or "menu draw" in c], reports)

    def test_esc_in_the_game_closes_the_menu(self):
        self.session.settle()
        menu_lua = self.a.lua.globals().require("hud/menu")
        self.a.key("coja_menu")
        self.a.lua.globals().fire("screen_closed", "COJA")
        self.assertFalse(menu_lua.open)
        self.assertIn("hold false", self.a.calls)

    def test_a_race_start_closes_the_menu(self):
        build_track(self.session, self.a)
        host_and_join(self.session, self.a, self.b)
        menu_b = self.b.lua.globals().require("hud/menu")
        self.b.key("coja_menu")
        self.a.ui("start", 7)
        self.session.step()
        self.assertFalse(menu_b.open, "the start sequence is the race's")

    def test_home_shows_the_profile_from_the_server_and_the_hub(self):
        self.session.settle()
        self.a.key("coja_menu")
        for _ in range(4):
            self.session.step()                                  # HOME asks the hub; its answers come back
        self.assertEqual(len([c for c in self.a.calls if c.startswith("http https://reskate.cocojambo.net/")]), 4)
        shown = menu(self.a, "HOME")
        self.assertIn("info Ranked: #3 of 7", shown)
        self.assertIn("info Unranked: unranked", shown)
        self.assertIn("info Crew: [COJA] Cocojambo", shown)
        before = self.a.shapes
        self.session.step()
        self.assertGreater(self.a.shapes - before, 40, "drawn: the logo, the tiles and the hub's lines")
        for _ in range(5):
            self.session.step()
        self.assertEqual(len([c for c in self.a.calls if c.startswith("http ")]), 4, "at most once a minute")

    def test_the_admin_tab_tells_the_game_s_marker_blueprints(self):
        self.session.settle()
        self.assertFalse([c for c in self.a.calls if "blueprints" in c], "not by just showing the tab")
        menu(self.a, "ADMIN", click="find_markers")
        self.session.step()
        self.session.step()
        lines = [c for c in self.session.server.calls if "diag Coco: blueprints:" in c]
        self.assertIn("print [coco race] diag Coco: blueprints: Gameplay/Challenges/ChallengeStart_Marker", lines)

    def test_the_settings_tab_changes_the_hud(self):
        self.session.settle()
        menu(self.a, "SETTINGS", set_size=3)
        saved = self.a.to_py(self.a.lua.eval('reskate.storage.get("hud:settings")'))
        self.assertEqual(saved["scale"], 1.2)

    def test_the_menu_keeps_the_controller_from_the_game_while_open(self):
        self.session.settle()
        self.a.key("coja_menu")
        del self.a.calls[:]
        for _ in range(3):
            self.session.step()
        self.assertEqual(self.a.calls.count("game_buttons 65535"), 3, "renewed every tick")
        self.assertEqual(self.a.calls.count("game_keys 38,40,37,39,13,32,9"), 3, "not Escape")
        self.a.key("coja_menu")
        self.assertIn("game_buttons 0", self.a.calls)
        self.assertIn("game_keys ", self.a.calls)
        del self.a.calls[:]
        self.session.step()
        self.assertFalse([c for c in self.a.calls if c.startswith("game_")], "closed: nothing held")

    def test_the_keyboard_and_the_controller_work_the_menu(self):
        build_track(self.session, self.a)
        menu_lua = self.a.lua.globals().require("hud/menu")
        self.a.key("coja_menu")
        self.session.step()
        for _ in range(2):
            self.tap(self.a, key=0x27)                           # right, along the tab row: HOME, RACES, ADMIN
        self.assertEqual(menu_lua.state().tab, "ADMIN")
        self.tap(self.a, key=0x28)                               # down: the track choice
        self.tap(self.a, bit=0x0002)                             # d-pad down: Edit this track
        self.assertEqual(menu_lua.state().focus, 2)
        self.tap(self.a, bit=0x1000)                             # A
        self.session.step()
        self.assertEqual(self.a.to_py(self.a.state("editor"))["track"]["name"], "Harbour Loop")
        self.tap(self.a, bit=0x2000)                             # B closes
        self.assertFalse(menu_lua.open)

    def test_the_controller_works_the_menu_through_gamepad_under_its_screen(self):
        build_track(self.session, self.a)
        menu_lua = self.a.lua.globals().require("hud/menu")
        self.a.pad_bits = None
        self.a.key("coja_menu")
        self.assertLess(self.a.calls.index("competition true"), self.a.calls.index("hold true"), "stock physics first")
        self.session.step()
        def press(name):
            self.a.pad_buttons = {name}
            self.session.step()
            self.a.pad_buttons = set()
            self.session.step()
        for name in ("rb", "rb"):
            press(name)
        self.assertEqual(menu_lua.state().tab, "ADMIN")
        press("down")
        press("down")
        press("a")
        self.session.step()
        self.assertEqual(self.a.to_py(self.a.state("editor"))["track"]["name"], "Harbour Loop")
        press("b")
        self.assertFalse(menu_lua.open)
        self.assertIn("competition false", self.a.calls)

    def test_the_mouse_works_the_menu_where_the_game_gives_a_cursor(self):
        build_track(self.session, self.a)
        menu_lua = self.a.lua.globals().require("hud/menu")
        self.a.key("coja_menu")
        self.session.step()
        self.session.step()                                      # slid in
        def click(x, y):
            self.a.cursor = {"x": x, "y": y, "left": False}
            self.session.step()
            self.a.cursor = {"x": x, "y": y, "left": True}
            self.session.step()                                  # drawn: the click is kept
            self.session.step()                                  # the next tick applies it
        click(700, 170)                                          # the second tab: RACES
        self.assertEqual(menu_lua.state().tab, "RACES")
        menu_lua.show("ADMIN")
        self.session.step()
        click(620, 356)                                          # Edit this track, the first button
        self.assertEqual(self.a.to_py(self.a.state("editor"))["track"]["name"], "Harbour Loop")
        click(1295, 120)                                         # the X
        self.assertFalse(menu_lua.open)

    def test_challenges_show_as_flags_on_the_map_and_enroll_runs_them(self):
        build_track(self.session, self.a)
        self.a.key("race_editor")
        self.a.ui("dialog", "tracks", "challenge", "", 1)
        self.session.settle()
        flags = [f for f in self.a.doc.get("flags", []) if f["id"].startswith("challenge:")]
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0]["leader"], self.b.id, "led by another player here, or the game would not show it")
        self.assertEqual(flags[0]["stats"][0], {"title": "CHALLENGE", "value": "Harbour Loop"})
        self.assertGreater(flags[0]["game"], 7000)
        self.assertEqual(self.b.to_py(self.b.doc["flags"])[0]["leader"], self.a.id)
        self.b.position = [300.0, 0.0, 300.0]
        self.b.ui("join", self.b.doc["flags"][0]["id"], self.a.id, self.b.doc["flags"][0]["game"])
        self.assertTrue(self.b.race().challenge)
        self.assertNotIn("flags", self.b.doc, "none while racing")

    def test_alone_there_is_nobody_to_lead_a_challenge_flag(self):
        session = Session()
        solo = session.add("76561198000000005", "Solo", staff=True)
        build_track(session, solo)
        solo.key("race_editor")
        solo.ui("dialog", "tracks", "challenge", "", 1)
        session.settle()
        self.assertNotIn("flags", solo.doc)
        for client in [session.server] + session.clients:
            client.lua.globals().fire("unload")

    def place_race(self, host, **choose):
        """The host chooses in F5 > RACES (choose: widget id -> value), then places the flag; the flag's id."""
        menu(host, "RACES", **choose)
        host.ui("open", 7)
        self.session.step()
        host.ui("setup_confirm", 4, 0)
        host.ui("flag_placed", 7, 1, 2, 3, 0)
        self.session.settle(3)
        return host.state("host").id

    def race_flags(self, client):
        return [f for f in client.to_py(client.doc).get("flags", []) if not f["id"].startswith("challenge:")]

    def test_an_invite_only_race_goes_only_to_the_invited(self):
        c = self.session.add("76561198000000003", "Mambo")
        build_track(self.session, self.a)
        menu(self.a, "RACES", race_access=3)
        shown = menu(self.a, "RACES", click="invite_" + self.b.id)
        self.assertIn("toggle invite_" + c.id, shown, "everyone here can be invited")
        race_id = self.place_race(self.a)
        flags = self.race_flags(self.b)
        self.assertEqual([f["id"] for f in flags], [race_id])
        self.assertEqual(flags[0]["stats"][2], {"title": "FOR", "value": "Invited players"})
        self.assertEqual(self.race_flags(c), [], "not invited: no flag")
        self.a.receive(c, json.dumps({"t": "join", "race": race_id}))   # asking anyway
        self.assertEqual(len(self.a.to_py(self.a.state("host"))["order"]), 1, "turned away")
        self.b.ui("join", race_id, self.a.id, 7)
        self.session.settle()
        self.assertEqual(len(self.a.to_py(self.a.state("host"))["order"]), 2)
        c.lua.globals().fire("unload")

    def test_a_crew_race_shows_only_to_that_crew(self):
        c = self.session.add("76561198000000003", "Mambo")
        self.session.settle()
        for client, tag in ((self.a, "COJA"), (self.b, "COJA"), (c, "XYZ")):
            self.session.server.console(leaderboard.hud_line("player", client.id, {"points": 1, "team": tag}))
        self.session.settle()
        build_track(self.session, self.a)
        race_id = self.place_race(self.a, race_access=2)
        self.assertEqual(self.race_flags(self.b)[0]["stats"][2], {"title": "FOR", "value": "Crew [COJA]"})
        self.assertEqual(self.race_flags(c), [], "another crew: no flag")
        self.a.receive(c, json.dumps({"t": "join", "race": race_id, "crew": "XYZ"}))
        self.assertEqual(len(self.a.to_py(self.a.state("host"))["order"]), 1, "turned away")
        self.b.ui("join", race_id, self.a.id, 7)
        self.session.settle()
        self.assertEqual(len(self.a.to_py(self.a.state("host"))["order"]), 2)
        c.lua.globals().fire("unload")

    def test_tracks_kept_for_the_base_world_move_to_the_map_the_server_plays(self):
        session = Session(map_name="bam")
        session.level = "Levels/Game/dingolevel_reskate_skate3improved/dingolevel_reskate_skate3improved"
        server = session.server
        server.lua.execute("""
            reskate.storage.set("tracks:bam", {{id = "1-1", name = "Old", author = "Coco", circuit = false, challenge = true,
                gates = {{p = {0, 0, 0}, n = {1, 0}, w = 8, h = 5, heading = 0}, {p = {20, 0, 0}, n = {1, 0}, w = 8, h = 5, heading = 0}}}})
            reskate.storage.set("best:bam:1-1", {{id = "76561198000000001", n = "Coco", ms = 12345}})
        """)
        a = session.add("76561198000000001", "Coco", staff=True)
        session.settle()
        names = [t["name"] for t in a.tracks()]
        self.assertEqual(names[0], "Old", "the game asks for the level's tracks")
        self.assertIn("University Race 2", names, "and the plugin's bundle for Skate 3 Improved goes in after the move")
        self.assertEqual(a.tracks()[0]["best"]["ms"], 12345, "with their times")
        self.assertIsNone(server.lua.eval('reskate.storage.get("tracks:bam")'))
        self.assertTrue(any(c.startswith("print [coco race] moved 1 track(s) from the base world bam to dingolevel_reskate_skate3improved")
                            for c in server.calls), server.calls)
        a.lua.globals().fire("unload")

    def test_a_flag_registration_left_open_is_let_go(self):
        self.session.settle()
        self.a.flag_state = {"busy": True, "owned": True, "waiting": True, "placing": False, "mode": "deathraces"}
        for _ in range(30):
            self.session.step()
        self.assertNotIn("flag leave", self.a.calls, "not before 5 s")
        for _ in range(40):
            self.session.step()
        self.assertIn("flag leave", self.a.calls, "nothing of ours used it: the Throwdowns cards open again")
        self.assertTrue(any("diag Coco: flag: let go of a registration" in c for c in self.session.server.calls))
        del self.a.calls[:]
        self.a.flag_state = {"busy": True, "owned": True, "waiting": False, "placing": True, "mode": "deathraces"}
        for _ in range(80):
            self.session.step()
        self.assertNotIn("flag leave", self.a.calls, "placing: in use")

    def test_gates_cannot_be_skipped(self):
        build_track(self.session, self.a, circuit=False)
        host_and_join(self.session, self.a, self.b)
        self.a.ui("start", 7)
        lights_out(self.session)
        skate(self.session, self.b, (30, 0, 0))     # through the start
        self.assertEqual(self.b.race().racers[self.b.id].n, 1)
        skate(self.session, self.b, (30, 0, 38))    # around gate 2
        skate(self.session, self.b, (5, 0, 38))     # through gate 3
        self.assertEqual(self.b.race().racers[self.b.id].n, 1, "gate 3 does not count before gate 2")
        skate(self.session, self.b, (30, 0, 10))
        skate(self.session, self.b, (60, 0, 10))
        skate(self.session, self.b, (60, 0, 30))    # through gate 2's plane beside its frame
        self.assertIsNotNone(self.b.race().missed_at, "missed: outside the frame")
        self.assertEqual(self.b.race().racers[self.b.id].n, 1)
        skate(self.session, self.b, (60, 0, 0))
        skate(self.session, self.b, (40, 0, 10))
        skate(self.session, self.b, (40, 0, 30))    # through gate 2 in its frame
        self.assertEqual(self.b.race().racers[self.b.id].n, 2)

    def test_a_gate_the_wrong_way_does_not_count(self):
        build_track(self.session, self.a, circuit=False)
        host_and_join(self.session, self.a, self.b)
        self.a.ui("start", 7)
        lights_out(self.session)
        skate(self.session, self.b, (30, 0, 0))
        skate(self.session, self.b, (35, 0, 30))
        skate(self.session, self.b, (40, 0, 30))
        skate(self.session, self.b, (40, 0, 10))    # through gate 2 backwards
        self.assertEqual(self.b.race().racers[self.b.id].n, 1)
        skate(self.session, self.b, (40, 0, 30))
        self.assertEqual(self.b.race().racers[self.b.id].n, 2)

    def test_a_racer_who_teleports_is_out(self):
        build_track(self.session, self.a)
        host_and_join(self.session, self.a, self.b)
        self.a.ui("start", 7)
        lights_out(self.session)
        skate(self.session, self.b, CORNERS[0])
        self.b.position = [500.0, 0.0, 500.0]
        self.session.step()
        self.session.step()
        self.assertEqual(self.b.race().racers[self.b.id].dnf, "teleported")
        self.assertEqual(self.a.race().racers[self.b.id].dnf, "teleported", "the others hear it")

    def test_the_server_puts_a_racer_its_game_could_not_move_on_the_grid(self):
        build_track(self.session, self.a)
        host_and_join(self.session, self.a, self.b)
        self.b.can_teleport = False
        self.b.position = [-30.0, 0.0, 0.0]
        self.a.ui("start", 7)
        for _ in range(6):
            self.session.step()
        self.assertTrue(self.b.race().placed)
        self.assertLess(self.b.position[0], 20, "on the grid behind the start")
        self.assertGreater(self.b.position[0], 5)
        self.assertTrue(self.b.doc["match"]["input_lock"], "held for the lights")
        self.assertIn("move " + self.b.id, self.session.server.calls)
        self.assertTrue(any(c.startswith("print [coco race] grid Jambo slot 2 on Harbour Loop: moved")
                            for c in self.session.server.calls), self.session.server.calls)
        self.assertTrue(any(c.startswith("print [coco race] diag Jambo: teleport: ok=true returned=false")
                            for c in self.session.server.calls), self.session.server.calls)
        lights_out(self.session)
        self.assertFalse(self.b.doc["match"]["input_lock"])

    def test_a_racer_who_cannot_be_moved_skates_to_the_start(self):
        build_track(self.session, self.a)
        host_and_join(self.session, self.a, self.b)
        self.b.can_teleport = False
        self.session.server.can_move = False
        self.b.position = [-30.0, 0.0, 0.0]
        self.a.ui("start", 7)
        lights_out(self.session)
        self.assertFalse(self.b.race().placed)
        self.assertFalse(self.b.doc["match"]["input_lock"], "free to skate")
        skate(self.session, self.b, CORNERS[0])
        self.assertEqual(self.b.race().racers[self.b.id].n, 1, "the start counts once gone through")

    def test_the_largest_race_fits_in_a_message(self):
        self.session.settle()
        for i in range(8):
            self.session.add(f"7656119800000010{i}", f"Racer number {i} with a long name")
        self.session.settle()
        self.a.key("race_editor")
        self.a.ui("dialog", "tracks", "new", "", 0)
        for i in range(45):
            skate(self.session, self.a, (12345.678 + 20 * i, -987.654, 23456.789), speed=100)
            self.a.key("gate_add")
        self.assertIn("A track has at most 40 gates.", self.a.toasts)
        self.a.key("race_editor")
        self.a.ui("dialog", "save_track", "save", "x" * 40, 0)
        self.session.settle()
        self.assertEqual(len(self.b.tracks()[0]["gates"]), 40)
        self.a.ui("open", 7)
        self.a.ui("setup_confirm", 10, 0)
        self.a.ui("flag_placed", 7, 1, 2, 3, 0)
        self.session.step()
        for client in self.session.clients[1:]:
            flag = client.doc["flags"][0]
            client.ui("join", flag["id"], flag["leader"], 7)
        self.session.settle()
        self.a.ui("start", 7)  # send() checks the size
        self.session.settle()
        self.assertTrue(all(c.race() is not None for c in self.session.clients))

    def test_leaving_a_joined_flag_tells_the_host(self):
        build_track(self.session, self.a)
        host_and_join(self.session, self.a, self.b)
        self.b.key("race_leave")
        self.session.step()
        self.assertEqual(self.a.doc["waiting"]["enrolled"], 1)

    def test_a_host_who_calls_it_off_frees_the_joiners(self):
        build_track(self.session, self.a)
        host_and_join(self.session, self.a, self.b)
        self.assertIsNotNone(self.b.state("joined"))
        self.a.ui("leave", 7)
        self.session.step()
        self.assertIsNone(self.a.state("host"))
        self.assertIsNone(self.b.state("joined"))


if __name__ == "__main__":
    unittest.main()


def b64(value):
    return base64.b64encode(json.dumps(value).encode()).decode()


@unittest.skipUnless(lupa, "pip install lupa")
class HudTest(unittest.TestCase):
    def setUp(self):
        self.session = Session()
        self.a = self.session.add("76561198000000001", "Coco", staff=True)
        self.b = self.session.add("76561198000000002", "Jambo")
        self.server = self.session.server
        self.session.settle()

    def tearDown(self):
        for client in [self.server] + self.session.clients:
            self.assertEqual(client.oversize, [], client.name + " sent a message over 4 KB")
            self.assertFalse([c for c in client.calls if c.startswith("log ")], client.name)

    def prints(self):
        return [c[len("print "):] for c in self.server.calls if c.startswith("print ")]

    def test_loading_registers_the_console_command_and_the_settings_page(self):
        self.assertIn("command coco", self.server.calls)
        self.assertIn("page COJA tools", self.a.calls)

    def test_the_hud_says_hello_and_shows_the_role(self):
        hud = self.a.hud()
        self.assertIsNotNone(hud.get("info"), "the server's copy answered the hello")
        self.assertEqual(hud["me"]["role"], "admin")
        self.assertEqual(self.b.hud()["me"]["role"], "player")
        self.server.console("coco status")
        self.assertTrue(any("2 player(s) with the HUD" in p for p in self.prints()), self.prints())

    def test_stats_reach_only_that_player(self):
        self.server.console("coco player 76561198000000002 " + b64({"points": 1234, "rank": 3, "of": 40, "board": "Week 43"}))
        self.session.settle()
        self.assertEqual(self.b.hud()["me"]["stats"]["points"], 1234)
        self.assertIsNone(self.a.hud()["me"].get("stats"))

    def test_the_board_and_the_server_info_go_to_everyone_and_are_kept(self):
        self.server.console("coco top " + b64({"board": "Week 43", "rows": [{"n": "Jambo", "p": 50}, {"n": "Coco", "p": 20}]}))
        self.server.console("coco info " + b64({"name": "COCOJAMBOS YARD", "links": [{"label": "Discord", "url": "discord.gg/x"}],
                                                "rules": ["Be nice"]}))
        self.session.settle()
        for c in (self.a, self.b):
            self.assertEqual(c.hud()["top"]["rows"][0]["n"], "Jambo")
            self.assertEqual(c.hud()["info"]["name"], "COCOJAMBOS YARD")
        # a new player gets both on their hello
        c = self.session.add("76561198000000003", "Late")
        self.session.settle()
        self.assertEqual(c.hud()["info"]["name"], "COCOJAMBOS YARD")
        self.assertEqual(c.hud()["top"]["board"], "Week 43")

    def test_a_note_goes_on_the_hud_or_to_chat_without_it(self):
        self.session.bare.append(Bare("76561198000000009", "NoMod"))
        self.server.console("coco toast all " + b64({"text": "Event in 5 minutes!", "kind": "warn"}))
        self.session.settle(20)
        self.assertEqual(self.a.hud()["toasts"][0]["text"], "Event in 5 minutes!")
        self.assertIn("tell 76561198000000009 Event in 5 minutes!", self.server.calls)
        self.assertNotIn("tell 76561198000000001 Event in 5 minutes!", self.server.calls)

    def test_what_the_leaderboard_types_reaches_the_hud(self):
        self.server.console(leaderboard.hud_line("player", self.b.id, {"points": 1234, "rank": 2, "of": 9, "board": "All Time (Ranked)",
                                                                         "team": "Café", "lines": [{"l": "Week 44", "v": "#1, 40 pts"}]}))
        self.server.console(leaderboard.hud_line("top", {"board": "All Time (Ranked)", "rows": [{"n": "Jambo", "p": 1234}]}))
        self.server.console(leaderboard.hud_line("info", leaderboard.hud_info({"SERVER_NAME": "Yard", "TOOLS_LINKS": "Discord=d.gg/x"})))
        self.session.settle()
        hud = self.b.hud()
        self.assertEqual((hud["me"]["stats"]["points"], hud["me"]["stats"]["team"]), (1234, "Café"))
        self.assertEqual(hud["top"]["rows"][0]["n"], "Jambo")
        self.assertEqual(hud["info"]["links"][0]["label"], "Discord")
        self.b.key("hud_panel")
        self.session.step()   # the panel draws all of it

    def test_a_note_for_one_player(self):
        self.server.console("coco toast 76561198000000001 " + b64({"text": "Rank up!", "kind": "good"}))
        self.session.settle()
        self.assertEqual(self.a.hud()["toasts"][0]["kind"], "good")
        self.assertFalse(self.b.hud()["toasts"])

    def test_what_is_not_base64_json_is_refused(self):
        self.server.console("coco player 76561198000000002 not-base64!")
        self.server.console("coco player 123 " + b64({"points": 1}))
        self.server.console("coco toast someone " + b64({"text": "x"}))
        self.assertEqual(len([p for p in self.prints() if p.startswith("error: coco")]), 3, self.prints())

    def test_time_played_counts_and_is_saved_when_they_leave(self):
        self.session.now += 125_000
        self.session.step()
        self.server.lua.globals().fire("player_left", self.server.lua.table_from({"id": "76561198000000002", "name": "Jambo"}))
        line = [p for p in self.prints() if p.startswith("[coco] left 76561198000000002")][0]
        self.assertGreaterEqual(int(line.split()[-1]), 125)
        self.assertGreaterEqual(self.server.lua.eval('reskate.storage.get("hud:t:76561198000000002")'), 125)

    def test_f4_opens_and_closes_the_panel(self):
        self.a.key("hud_panel")
        self.assertTrue(self.a.hud()["panel"])
        before = self.a.shapes
        self.session.step()
        self.assertGreater(self.a.shapes, before)
        self.a.key("hud_panel")
        self.assertFalse(self.a.hud()["panel"])

    def test_the_hud_steps_aside_while_a_menu_is_open(self):
        self.session.step()
        shown = self.a.shapes
        self.session.step()
        per_frame = self.a.shapes - shown
        self.a.menu_open = True
        self.session.now += 300
        self.session.step()                      # the HUD looks every 250 ms
        self.assertTrue(self.a.hud()["hidden"])
        before = self.a.shapes
        self.session.step()
        self.assertLess(self.a.shapes - before, per_frame)
        self.a.menu_open = False
        self.session.now += 300
        self.session.step()
        self.assertFalse(self.a.hud()["hidden"])

    def test_the_settings_page_builds_and_keeps_a_change(self):
        lua = self.a.lua
        ui = lua.eval("""{
            tabs = function(_, id, names) for i, n in ipairs(names) do if n == "SETTINGS" then return i end end end,
            card = function() end, info = function() end, note = function() end, separator = function() end,
            toggle = function(_, id, text, on) if id == "corner" then return false end return on end,
            dropdown = function(_, id, text, choices, chosen) return 2 end,
            slider = function(_, id, text, value) return value end,
            button = function() return false end,
        }""")
        lua.globals().menu_page(ui)
        saved = self.a.to_py(lua.eval('reskate.storage.get("hud:settings")'))
        self.assertEqual((saved["corner"], saved["side"]), (False, "right"))
        self.session.step()   # the HUD draws with the tag off and on the right

    def test_base64_decodes_what_python_encodes(self):
        decode = self.server.lua.globals().require("lib/base64")
        for text in ("", "a", "ab", "abc", "abcd", '{"name": "Café ✓"}'):
            encoded = base64.b64encode(text.encode()).decode()
            self.assertEqual(decode(encoded), text)   # lupa hands Lua's bytes back as UTF-8 text
        self.assertIsNone(decode("no*base64"))


class Bare:
    """A player on the server without the plugin."""
    def __init__(self, steam_id, name):
        self.id, self.name = steam_id, name
