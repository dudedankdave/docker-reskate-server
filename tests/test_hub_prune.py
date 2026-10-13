"""tools/hub-prune.py: only older revision tags and released -rc tags are deleted."""
import importlib.util
import os
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
spec = importlib.util.spec_from_file_location("hub_prune", os.path.join(ROOT, "tools", "hub-prune.py"))
hub_prune = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hub_prune)


class PruneTest(unittest.TestCase):
    def test_stale(self):
        tags = {"latest", "slim", "2.0", "1.1", "2.0.8", "2.0.8-slim", "2.0.7", "2.0.7-slim", "2.0.5", "2.0.5-rc",
                "2.0.8-r111", "2.0.8-r111-slim", "2.0.8-r109", "2.0.8-r109-slim", "2.0.8-r104", "2.0.8-r104-slim",
                "2.0.7-r104", "2.0.7-r104-slim", "2.0.7-r101", "2.0.7-r101-slim", "2.0.9-rc"}
        self.assertEqual(hub_prune.stale(tags), [
            "2.0.5-rc", "2.0.7-r101", "2.0.7-r101-slim",
            "2.0.8-r104", "2.0.8-r104-slim", "2.0.8-r109", "2.0.8-r109-slim"])

    def test_flavours_apart(self):
        # a slim build that failed leaves the older slim revision as the newest slim one
        self.assertEqual(hub_prune.stale({"2.0.8-r111", "2.0.8-r109", "2.0.8-r109-slim"}), ["2.0.8-r109"])


if __name__ == "__main__":
    unittest.main()
