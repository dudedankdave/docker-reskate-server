"""app/teams.py: applications, accept/deny, joining, leaving, configuring, Discord links; and the
leaderboard's /team command and tags (app/leaderboard.py) on top of a teams provider."""
import os
import sys
import tempfile
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [os.path.join(ROOT, "app"), os.path.join(ROOT, "rollover"), os.path.join(ROOT, "modules"), os.path.join(ROOT, "bot")]
import bridge  # noqa: E402
import leaderboard  # noqa: E402
import sqlite3  # noqa: E402
import teams  # noqa: E402

A, B, C = "76561198000000001", "76561198000000002", "76561198000000003"


class TeamsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.t = teams.Teams(os.path.join(self.dir.name, "teams.db"), max_members=3).setup()
        for steam_id, name in ((A, "Anna"), (B, "Ben"), (C, "Cleo")):
            self.t.seen(steam_id, name)

    def tearDown(self):
        self.dir.cleanup()

    def act(self, steam_id, verb, **args):
        name = {A: "Anna", B: "Ben", C: "Cleo"}.get(steam_id, "Dan")
        return self.t.act({"steam_id": steam_id, "name": name}, verb, args)

    def found(self):
        """Anna's team [AB] Alpha Bravo, accepted."""
        applied = self.act(A, "apply", name="Alpha Bravo", tag="ab")
        self.assertTrue(applied["ok"], applied)
        self.assertEqual(self.t.decide(applied["application"], True, "admin")["text"], "[AB] Alpha Bravo accepted")

    def test_apply_needs_an_accept_before_the_tag_shows(self):
        applied = self.act(A, "apply", name="Alpha Bravo", tag="ab")
        self.assertEqual(self.t.tags(), {})
        self.assertEqual(self.t.applications()[0]["tag"], "AB")
        self.assertFalse(self.act(B, "apply", name="Other", tag="AB")["ok"])        # tag waits for approval
        done = self.t.decide(applied["application"], True, "admin")
        self.assertTrue(done["ok"])
        self.assertEqual(done["notify"][0]["steam_id"], A)
        self.assertEqual(self.t.tags(), {A: "AB"})
        self.assertFalse(self.t.decide(applied["application"], False, "admin")["ok"])   # decided once

    def test_deny_creates_nothing(self):
        applied = self.act(A, "apply", name="Alpha Bravo", tag="AB")
        self.assertIn("denied", self.t.decide(applied["application"], False, "admin")["text"])
        self.assertEqual(self.t.tags(), {})
        self.assertTrue(self.act(A, "apply", name="Alpha Bravo", tag="AB")["ok"])       # may apply again

    def test_bad_names_and_one_application_at_a_time(self):
        self.assertFalse(self.act(A, "apply", name="x", tag="AB")["ok"])
        self.assertFalse(self.act(A, "apply", name="Alpha Bravo", tag="A-B")["ok"])
        self.assertTrue(self.act(A, "apply", name="Alpha Bravo", tag="AB")["ok"])
        self.assertFalse(self.act(A, "apply", name="Alpha Two", tag="AC")["ok"])
        self.assertTrue(self.act(A, "cancel")["ok"])
        self.assertEqual(self.t.applications(), [])

    def test_invite_only_then_open(self):
        self.found()
        self.assertFalse(self.act(B, "join", team="AB")["ok"])
        self.assertTrue(self.act(A, "invite", player="Ben")["ok"])
        self.assertTrue(self.act(B, "join", team="ab")["ok"])
        self.act(A, "set", key="open", value="yes")
        self.assertTrue(self.act(C, "join", team="Alpha Bravo")["ok"])
        self.assertEqual(self.t.tags(), {A: "AB", B: "AB", C: "AB"})

    def test_only_the_owner_configures_and_the_team_has_a_limit(self):
        self.found()
        self.act(A, "set", key="open", value="yes")
        self.act(B, "join", team="AB")
        self.assertFalse(self.act(B, "set", key="tag", value="ZZ")["ok"])
        self.assertTrue(self.act(A, "set", key="tag", value="zz")["ok"])
        self.assertEqual(self.t.tags()[B], "ZZ")
        self.act(C, "join", team="ZZ")
        self.t.seen("76561198000000004", "Dan")
        self.assertIn("full", self.act("76561198000000004", "join", team="ZZ")["lines"][0])

    def test_leaving_hands_the_team_over_and_the_last_one_closes_it(self):
        self.found()
        self.act(A, "set", key="open", value="yes")
        self.act(B, "join", team="AB")
        self.assertTrue(self.act(A, "leave")["ok"])
        self.assertEqual(self.t.tags(), {B: "AB"})
        self.assertTrue(self.act(B, "set", key="motto", value="hi")["ok"])               # Ben owns it now
        self.act(B, "leave")
        self.assertEqual(self.act(A, "list")["lines"], ["No teams yet. /team apply TAG Name"])

    def test_kick_owner_and_disband(self):
        self.found()
        self.act(A, "set", key="open", value="yes")
        self.act(B, "join", team="AB")
        self.assertTrue(self.act(A, "kick", player="Ben")["ok"])
        self.assertEqual(self.t.tags(), {A: "AB"})
        self.assertFalse(self.act(A, "disband")["ok"])
        self.assertTrue(self.act(A, "disband", confirm="yes")["ok"])
        self.assertEqual(self.t.tags(), {})

    def test_discord_needs_a_link_from_game(self):
        who = {"discord": "42", "name": "Anna#1"}
        self.assertFalse(self.t.act(who, "apply", {"name": "Alpha Bravo", "tag": "AB"})["ok"])
        code = self.act(A, "link")["lines"][0].split()[3]
        self.assertTrue(self.t.act(who, "link", {"code": code})["ok"])
        self.assertTrue(self.t.act(who, "apply", {"name": "Alpha Bravo", "tag": "AB"})["ok"])
        self.assertEqual(self.t.applications()[0]["steam_id"], A)
        self.assertEqual(self.t.discord_of(A), "42")
        self.assertFalse(self.t.act({"discord": "43"}, "link", {"code": code})["ok"])    # a code works once


class ProfileTest(TeamsTest):
    """Profiles: leaders, the profile settings and the stats the bot adds from the leaderboards."""

    def test_leaders_can_run_the_team_but_not_the_owner_things(self):
        self.found()
        self.act(A, "set", key="open", value="yes")
        self.act(B, "join", team="AB")
        self.act(C, "join", team="AB")
        self.assertFalse(self.act(B, "set", key="description", value="x")["ok"])
        self.assertFalse(self.act(B, "leader", player="Cleo")["ok"])
        self.assertTrue(self.act(A, "leader", player="Ben")["ok"])
        self.assertTrue(self.act(B, "set", key="description", value="We skate parks.")["ok"])
        self.assertTrue(self.act(B, "set", key="category", value="Competitive")["ok"])
        self.assertTrue(self.act(B, "set", key="league", value="Ranked")["ok"])
        self.assertTrue(self.act(B, "set", key="logo", value="https://example.com/logo.png")["ok"])
        self.assertFalse(self.act(B, "set", key="logo", value="javascript:alert(1)")["ok"])
        self.assertFalse(self.act(B, "set", key="league", value="pro")["ok"])
        self.assertFalse(self.act(B, "set", key="tag", value="ZZ")["ok"])
        self.assertFalse(self.act(B, "kick", player="Anna")["ok"])               # the owner stays
        self.assertTrue(self.act(B, "kick", player="Cleo")["ok"])
        p = self.t.profile("alpha bravo")
        self.assertEqual((p["description"], p["category"], p["league"], p["logo"]),
                         ("We skate parks.", "Competitive", "ranked", "https://example.com/logo.png"))
        self.assertEqual([(m["name"], m["role"]) for m in p["members"]], [("Anna", "owner"), ("Ben", "leader")])
        self.assertEqual(self.act(A, "info")["team"], "AB")

    def test_stats_come_from_the_members_all_time_scores(self):
        try:
            import team_cmds
        except ImportError:                       # other tests stub discord; the real library is needed for embeds
            self.skipTest("discord.py is not installed")
        self.found()
        self.act(A, "set", key="open", value="yes")
        self.act(B, "join", team="AB")
        db = os.path.join(self.dir.name, "lb.db")
        con = sqlite3.connect(db)
        con.executescript(leaderboard.SCHEMA)
        con.execute("INSERT INTO boards (id, name, kind, created_at) VALUES ('general', 'All Time', 'general', 0)")
        for steam_id, points, wins, played in ((A, 30, 3, 5), (B, 10, 1, 4), (C, 99, 9, 9)):
            con.execute("INSERT INTO scores VALUES ('general', ?, ?, ?, ?)", (steam_id, points, wins, played))
        con.commit()
        con.close()

        def read_db(fn, league="ranked"):
            c = sqlite3.connect(db)
            c.row_factory = sqlite3.Row
            return fn(c.cursor())
        profile = self.t.profile("AB")
        stats = team_cmds.team_stats(profile, read_db, ["ranked"])["ranked"]
        self.assertEqual((stats["points"], stats["wins"], stats["played"], stats["best"]), (40, 4, 9, ("Anna", 30)))
        embed = team_cmds.profile_embed(profile, self.t.profiles(), read_db)
        self.assertEqual(embed.title, "[AB] Alpha Bravo")
        self.assertIn("Team ranking: #1 of 1", " ".join(f.value for f in embed.fields))


class LeaderboardTeamsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.t = teams.Teams(os.path.join(self.dir.name, "teams.db"), max_members=3).setup()
        board = leaderboard.Leaderboard(lambda line: self.sent.append(line), None, {
            "LEADERBOARD": "true", "LEADERBOARD_FILE": os.path.join(self.dir.name, "lb.db")})
        self.sent = []
        board.online = {"Anna": A, "Ben": B}
        self.board = board

    def tearDown(self):
        self.dir.cleanup()

    class Provider:
        def __init__(self, t):
            self.t = t

        def tags(self):
            return self.t.tags()

        def act(self, who, verb, args):
            return self.t.act(who, verb, args)

    def test_command_applies_and_who_lists_tags(self):
        self.board.teams = self.Provider(self.t)
        self.board.command("Anna /team apply AB Alpha Bravo")
        self.assertTrue(any("Application for [AB] Alpha Bravo sent" in m for m in self.sent), self.sent)
        self.t.decide(self.t.applications()[0]["id"], True, "admin")
        self.board.tag_cache, self.board.tags_at = {}, 0
        self.sent.clear()
        self.board.asked.clear()
        self.board.command("Ben /team who")
        self.assertTrue(any("[AB] Anna" in m for m in self.sent), self.sent)

    def test_rows_show_the_tag(self):
        self.board.teams = self.Provider(self.t)
        self.t.seen(A, "Anna")
        self.t.decide(self.act_apply(), True, "admin")
        self.assertEqual(self.board.shown({"steam_id": A, "name": "Anna"}), "[AB] Anna")
        self.assertEqual(self.board.shown({"steam_id": B, "name": "Ben"}), "Ben")

    def act_apply(self):
        return self.t.act({"steam_id": A, "name": "Anna"}, "apply", {"name": "Alpha Bravo", "tag": "AB"})["application"]

    def test_without_a_provider_nothing_changes(self):
        self.assertEqual(self.board.shown({"steam_id": A, "name": "Anna"}), "Anna")
        self.assertIn("not available", self.board.cmd_team(A, "apply AB Alpha", 0)[1][0])


class BridgeTeamsTest(unittest.TestCase):
    """The bridge follows joins and leaves, tells the teams every name and answers /v1/teams calls."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.old = bridge.teams_db, dict(bridge.online), bridge.board
        bridge.board = type("Board", (), {"cfg": {"servers": {"1": {"slots": {"a": {"port": 1}}}}}, "gather": lambda self: {"1": []}})()
        bridge.teams_db = teams.Teams(os.path.join(self.dir.name, "teams.db")).setup()
        bridge.online.clear()

    def tearDown(self):
        bridge.teams_db, online, bridge.board = self.old
        bridge.online.clear()
        bridge.online.update(online)
        self.dir.cleanup()

    def test_tracks_who_is_on_and_resolves_names(self):
        bridge.track("1", "a", f"[12:00:00] Anna joined ({A}), 1/20 players")
        bridge.track("1", "a", f"[12:00:05] Ben joined ({B}, admin), 2/20 players")
        bridge.track("1", "a", "[12:01:00] Ben left (disconnected) [x]")
        self.assertEqual(bridge.online[("1", "a")], {A: "Anna"})
        result = bridge.team_call("/v1/teams/act", {"who": {"steam_id": A, "name": "Anna"}, "verb": "apply",
                                                    "args": {"name": "Alpha Bravo", "tag": "ab"}})
        self.assertTrue(result["ok"])
        decided = bridge.team_call("/v1/teams/decide", {"id": result["application"], "accept": True, "by": "admin"})
        self.assertTrue(decided["ok"])
        self.assertEqual(bridge.teams_db.tags(), {A: "AB"})
        bridge.track("1", "a", "[12:02:00] Server is up on Pattonville for 20 players.")
        self.assertNotIn(("1", "a"), bridge.online)


if __name__ == "__main__":
    unittest.main()
