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
    },
    json = {encode = function(value) return py.encode(value) end, decode = function(text) return py.decode(text) end},
    net = {send = function(to, payload) return py.send(to, payload) end},
    storage = {
        get = function(key) return storage[key] end,
        keys = function() local out = {} for k in pairs(storage) do table.insert(out, k) end return out end,
        set = function(key, value) py.call("storage " .. key) storage[key] = py.decode(py.encode(value)) end,
    },
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
    },
    physics = {
        competition = function(on) py.call("competition " .. tostring(on)) end,
        prepare = function() end,
    },
    throwdowns = {
        mode = function(m) py.call("mode " .. m.id) return true end,
        ui = function(doc) py.publish(doc) return true end,
        flag = {leave = function() py.call("flag leave") end},
    },
    ui = {
        toast = function(text) py.toast(text) return true end,
        dialog = function(id, spec) py.dialog(id, spec) return true end,
        close_dialog = function(id) end,
    },
    audio = {play_ui = function(name) return true end},
    log = function(text, level) py.call("log " .. text) end,
    input = {
        bind = function(action, key) py.call("bind " .. action .. " " .. key) return true end,
        focus = function() return {focused = true, interface_open = false, gameplay = true} end,
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
        self.calls, self.toasts, self.doc, self.dialogs, self.oversize = [], [], {}, [], []
        self.shapes = 0
        self.menu_open = False
        self.lua = lupa.LuaRuntime(unpack_returned_tuples=True)
        py = self.lua.table_from({
            "id": steam_id, "name": name, "side": side, "map": lambda: session.map, "heading": 1.25,
            "now": lambda: session.now, "encode": self.encode, "decode": self.decode, "send": self.send,
            "pos": lambda: self.lua.table_from(self.position), "teleport": self.teleport,
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
        self.calls.append("teleport")
        return True

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


def lights_out(session):
    for _ in range(90):  # the grid, five lights, the hold
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
        self.assertEqual(sum(c.startswith("bind ") for c in self.a.calls), 14, "at most 16 actions")

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
            self.assertIn("competition true", client.calls)
        self.assertNotEqual(self.a.position, self.b.position, "side by side")
        lights_out(self.session)
        self.assertEqual(self.a.race().phase, "running")
        self.assertFalse(self.a.doc["match"]["input_lock"])
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

    def test_a_racer_who_cannot_be_moved_skates_to_the_start(self):
        build_track(self.session, self.a)
        host_and_join(self.session, self.a, self.b)
        self.b.can_teleport = False
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
            tabs = function() return 3 end,
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
