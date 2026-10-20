"""The default image (app/entrypoint.py) and the slim image (slim/, Go) must write the same
ReSkateServer.json and data files from the same env vars. Both run in their "config" mode
(write, then exit) into a temporary RESKATE_DATA. Needs Go for the slim side (skipped without)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

# Every setting the entrypoints know, with a valid value (one env set per case).
FULL = {
    "SERVER_NAME": "Test Yard 1 - Main", "SERVER_PASSWORD": "secret", "WELCOME_MESSAGE": "Hi #1",
    "LISTED": "false", "MAX_PLAYERS": "50", "PORT": "12400", "QUERY_PORT": "12401", "STEAM_TOKEN": "abc",
    "ACTIVITY_LOG": "true", "CHAT_COLOR": "#8e5cff", "CHAT_TEXT_COLOR": "#D9C8FF", "GLOBAL_BANS": "true",
    "MAP": "Isle of Grom", "MAP_POOL": "Isle of Grom,San Vansterdam", "MAP_ROTATION_MINUTES": "30",
    "MAP_ROTATION_VOTE": "true", "MAP_ROTATION_VOTE_SECONDS": "45", "MAP_ROTATION_EXTEND_MINUTES": "15",
    "MAP_ROTATION_ANNOUNCE": "false", "MAP_LATEST_VERSIONS": "true", "MAP_LATEST_CHECK_MINUTES": "60",
    "WORLD_LAYER_SYNC": "true", "LAYERS": "rain=on,fog=off", "BOOSTS": "false", "NO_BAIL": "true",
    "NOCLIP": "false", "PARTIES": "true", "PARTY_SIZE": "4", "VOICE_CHAT": "true", "VOICE_RANGE": "300",
    "OBJECT_PLACEMENT": "admins", "OBJECT_LIMIT": "200", "ANNOUNCE_THROWDOWNS": "false", "THROWDOWN_LOCKS": "true",
    "AFK_KICK_MINUTES": "20", "KICK_MINUTES": "30", "WORD_WARNINGS": "2", "OBJECT_SCALING": "false", "SYNC_EFFECTS": "false", "SYNC_DRONES": "false",
    "GLOBAL_WORD_FILTER": "false", "SPEED_CHECK": "kick", "SCORE_CHECK": "kick", "BOARD_CHECK": "kick",
    "ENFORCE_TUNING": "true", "BONE_SCALE_LIMIT": "2", "BONE_REACH_LIMIT": "1.5",
    "USE_STEAM_RELAY": "false", "SEND_RATE": "900", "CROWD_BUDGET": "600", "PACK_MS": "10", "THREADS": "4",
    "FINGER_DISTANCE": "25", "STEAM_DEBUG": "false", "DISTANCE_FULL_RATE_RETURN": "50",
    "DISTANCE_HALF_RATE_START": "60", "DISTANCE_HALF_RATE_RETURN": "150", "DISTANCE_LOW_RATE_START": "170",
    "PARK_CONSTRUCTION": "skatepark_01", "PARK_HISTORIC": "megapark_05", "PARK_FINANCIAL": "empty",
    "PARK_MODS": '[{"name": "street", "folder": "street park", "map": "San Vansterdam", "key": "bam"}]',
    "PARK_LIMIT_PARKS": "2", "PARK_LIMIT_OBJECTS": "300",
    "VOTE_MAP_ENABLED": "true", "VOTE_MAP_PERCENT": "60", "VOTE_KICK_ENABLED": "false",
    "VOTE_TIME_OF_DAY_ENABLED": "true", "VOTE_TIME_OF_DAY_MIN_PLAYERS": "3", "VOTE_EXTEND_MAP_SECONDS": "20",
    "VOTE_KICK_MINUTES": "restart", "VOTE_SECONDS": "30", "VOTE_COOLDOWN_SECONDS": "60", "VOTE_STARTER_YES": "false", "POLLS": "everyone",
    "POLL_SECONDS": "90", "VOTES_CUSTOM": '[{"name": "restart", "command": "map {map}", "percent": 60}]',
    "ANNOUNCEMENTS": "Join our Discord|Vote with /vote map", "ANNOUNCEMENT_INTERVAL_MINUTES": "10",
    "COMMANDS": '[{"name": "discord", "reply": "Join us"}]', "ADMINS": "76561198000000001",
    "RESERVED": "76561198000000002", "SCORE_ALLOW": "ABCDEF0123456789", "BANS": "76561198000000003:Bob",
    "WORDS_FILTERED": "darn,heck", "WORDS_BLOCKED": "badword",
}
CASES = {
    "nothing set": {},
    "every setting": FULL,
    "cleared": {"SERVER_PASSWORD": "off", "WELCOME_MESSAGE": "off", "MAP_POOL": "off", "ANNOUNCEMENTS": "off",
                "COMMANDS": "off", "VOTES_CUSTOM": "off", "PARK_MODS": "off", "WORDS_BLOCKED": "off"},
    "leaderboard": {"LEADERBOARD": "true", "COMMANDS": '[{"name": "rules", "reply": "Be nice"}]'},
    "name cleaned": {"SERVER_NAME": "Cocojambo's Yard | discord.gg/x"},
}


# Settings files (/shared/settings) for the layer cases: {case: {file: text}}, each run as server 1.
LAYERS = {
    "layers": {
        "common.env": (
            "# shared by every server\n"
            "WELCOME_MESSAGE=\"Welcome to the #1 yard\"   # quoted, keeps the #\n"
            "MAX_PLAYERS=100   # a comment\n"
            "ANNOUNCEMENTS=Join our Discord|Be nice\n"
            "MAP_POOL=Isle of Grom,San Vansterdam\n"
            "COMMANDS='[{\"name\": \"discord\", \"reply\": \"Join us\"}]'\n"
            "PORT=1   # identity: ignored here\n"
            "export SPEED_CHECK=warn\n"
        ),
        "secrets/common.env": "SERVER_PASSWORD=shh\n",
        "server-1.env": (
            "SERVER_NAME=Yard 1 - Main\n"
            "MAX_PLAYERS=20\n"
            "MAP_POOL+=Super Ultra Mega Resort\n"
            "ANNOUNCEMENTS+=Ranked server\n"
            "COMMANDS+=[{\"reply\": \"No griefing\", \"name\": \"rules\"}]\n"
        ),
        "secrets/server-1.env": "STEAM_TOKEN=token-1\nWEBHOOK_URL_x=https://example.invalid/secret-hook\n",
        "server-2.env": "MAX_PLAYERS=5\n",
    },
}


def files(folder):
    out = {}
    for base, _, names in os.walk(folder):
        for name in names:
            path = os.path.join(base, name)
            with open(path, encoding="utf-8") as f:
                text = f.read()
            out[os.path.relpath(path, folder)] = json.loads(text) if name.endswith(".json") else text
    return out


class ConfigParityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.slim = None
        if shutil.which("go"):
            cls.slim = os.path.join(cls.tmp, "reskate")
            subprocess.run(["go", "build", "-o", cls.slim, "."], cwd=os.path.join(ROOT, "slim"), check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def run_config(self, cmd, env, layers=None):
        data = tempfile.mkdtemp(dir=self.tmp)
        settings = os.path.join(data, "..", os.path.basename(data) + "-settings")
        for name, text in (layers or {}).items():
            os.makedirs(os.path.dirname(os.path.join(settings, name)), exist_ok=True)
            with open(os.path.join(settings, name), "w", encoding="utf-8") as f:
                f.write(text)
        run = subprocess.run(cmd, env={"PATH": os.environ["PATH"], "RESKATE_DATA": data, "SETTINGS_DIR": settings, **env},
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        return files(data), run.stdout

    def python(self, env, layers=None):
        return self.run_config([sys.executable, os.path.join(ROOT, "app", "entrypoint.py"), "config"], env, layers)

    def test_layers(self):
        env = {"RESKATE_SERVER": "1", "PORT": "12400", "QUERY_PORT": "12401", "STEAM_TOKEN": "slot-a", "MAX_PLAYERS": "250",
               "SERVER_NAME": "from the container"}
        py, out = self.python(env, LAYERS["layers"])
        cfg = py["ReSkateServer.json"]
        self.assertEqual(cfg["server"]["welcome_message"], "Welcome to the #1 yard")
        self.assertEqual(cfg["server"]["max_players"], 20)                 # own beats shared beats container
        self.assertEqual(cfg["server"]["name"], "Yard 1 - Main")
        self.assertEqual(cfg["server"]["port"], 12400)                     # PORT in common.env is ignored
        self.assertEqual(cfg["server"]["password"], "shh")
        self.assertEqual(cfg["server"]["steam_token"], "slot-a")           # per slot: the container's
        self.assertIn("STEAM_TOKEN in secrets/server-1.env is ignored", out)
        self.assertEqual(cfg["maps"]["pool"], ["Isle of Grom", "San Vansterdam", "Super Ultra Mega Resort"])
        self.assertEqual(cfg["announcements"]["messages"], ["Join our Discord", "Be nice", "Ranked server"])
        self.assertEqual([c["name"] for c in cfg["commands"]], ["discord", "rules"])
        self.assertEqual(cfg["anti_cheat"]["speed_hack"], "warn")
        self.assertIn("[settings] common.env: WELCOME_MESSAGE, MAX_PLAYERS", out)
        self.assertNotIn("shh", out)
        self.assertNotIn("secret-hook", out)
        if self.slim:
            go, go_out = self.run_config([self.slim, "config"], env, LAYERS["layers"])
            self.assertEqual(py, go)
            digest = [line for line in out.splitlines() if "digest" in line]
            self.assertEqual(digest, [line for line in go_out.splitlines() if "digest" in line])
            self.assertEqual(len(digest), 2)                              # digest and restart-digest

    def test_same_config(self):
        if not self.slim:
            self.skipTest("go is not installed")
        for name, env in CASES.items():
            with self.subTest(name):
                py, _ = self.python(env)
                go, _ = self.run_config([self.slim, "config"], env)
                self.assertEqual(py, go)

    def test_every_setting_lands(self):
        py, _ = self.run_config([sys.executable, os.path.join(ROOT, "app", "entrypoint.py"), "config"], FULL)
        cfg = py["ReSkateServer.json"]
        self.assertEqual(cfg["anti_cheat"]["pinned_board"], "kick")
        self.assertEqual(cfg["players"]["use_global_word_filter"], False)
        self.assertEqual(cfg["maps"]["park_limits"], {"parks": 2, "objects": 300})
        self.assertEqual(cfg["maps"]["park_mods"][0]["name"], "street")
        self.assertEqual((cfg["players"]["kick_minutes"], cfg["votes"]["kick_minutes"]), (30, 0))
        self.assertIn("darn\nheck\n", py[os.path.join("data", "words_filtered.txt")])

    def test_bad_value_stops(self):
        for env in ({"PARTY_SIZE": "9"}, {"BOARD_CHECK": "maybe"}, {"KICK_MINUTES": "1441"}, {"PARK_MODS": "{}"}):
            with self.subTest(env):
                run = subprocess.run([sys.executable, os.path.join(ROOT, "app", "entrypoint.py"), "config"],
                                     env={"PATH": os.environ["PATH"], "RESKATE_DATA": tempfile.mkdtemp(dir=self.tmp), **env},
                                     capture_output=True, text=True)
                self.assertNotEqual(run.returncode, 0)


if __name__ == "__main__":
    unittest.main()
