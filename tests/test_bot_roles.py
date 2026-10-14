"""bot/roles.py: which jobs each of the three bots does."""
import os
import sys
import types
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [os.path.join(ROOT, "bot"), os.path.join(ROOT, "rollover"), os.path.join(ROOT, "app")]
sys.modules.setdefault("discord", types.ModuleType("discord"))
import feeds  # noqa: E402
import roles  # noqa: E402


class RolesTest(unittest.TestCase):
    def setUp(self):
        os.environ.pop("BOT_ROLE", None)

    def test_default_is_everything(self):
        self.assertEqual(roles.role_of({}), "all")
        self.assertTrue(roles.jobs({})["stats"] and roles.jobs({})["console"])

    def test_env_beats_config(self):
        os.environ["BOT_ROLE"] = "chat"
        self.assertEqual(roles.role_of({"role": "admin"}), "chat")

    def test_unknown_role_stops(self):
        with self.assertRaises(SystemExit):
            roles.role_of({"role": "vee"})

    def test_scopes_are_split_without_overlap(self):
        chat, admin = set(roles.ROLES["chat"]["scopes"]), set(roles.ROLES["admin"]["scopes"])
        self.assertFalse(chat & admin)
        self.assertEqual(chat | admin, set(roles.ROLES["all"]["scopes"]))
        self.assertFalse(roles.ROLES["stats"]["scopes"])

    def test_only_one_bot_does_each_job(self):
        for job in ("stats", "console"):
            self.assertEqual([r for r in ("stats", "chat", "admin") if roles.ROLES[r][job]], ["stats"] if job == "stats" else ["admin"])

    def test_feeds_only_collect_the_roles_scopes(self):
        lines = [("1", "A", "console", None, "[20:01:02] Dave joined (7656, admin), 3/100 players, loaded in 1 s", False),
                 ("1", "A", "console", None, "[20:09:00] [chat] Dave: hi", False)]
        cfg = {"servers": {"1": {s: "1" for s in ("public", "chat", "admin", "log")}}}
        for role, want in (("chat", {"public", "chat"}), ("admin", {"admin", "log"})):
            bot = types.SimpleNamespace(cfg=cfg, jobs=roles.ROLES[role],
                                        backend=types.SimpleNamespace(lines=lambda: lines))
            got = {scope for (_n, scope) in feeds.Feeds(bot).collect()}
            self.assertEqual(got, want, role)


if __name__ == "__main__":
    unittest.main()
