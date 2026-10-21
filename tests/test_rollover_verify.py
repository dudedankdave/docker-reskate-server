"""rollover.py: a swapped-in container is watched; a crash rolls back to the old one, a missing volume stops the swap."""
import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [os.path.join(ROOT, "rollover"), os.path.join(ROOT, "app")]
import rollover  # noqa: E402

SERVER = {"slots": {"a": {"port": 12400, "query_port": 12401, "volume": "data-1"},
                    "b": {"port": 12402, "query_port": 12403, "volume": "data-1b"}}}


class Old:
    name, slot, version, running = "reskate-server-1-2.1.1", "a", "2.1.1", True

    def __init__(self):
        self.env = {"STEAM_TOKEN": "tok", "PORT": "12400", "RESKATE_IMAGE_VERSION": "2.1.1"}
        self.info = {"Id": "old", "Name": "/reskate-server-1-2.1.1",
                     "Config": {"Image": "dudedankdave/reskate-server:2.1.1", "Env": []},
                     "HostConfig": {"NetworkMode": "host"}}


class FakeDocker:
    """Records calls; the new container's inspect follows `states` (one per poll), volumes in `volumes` exist."""

    def __init__(self, states, volumes=("data-1", "data-1b")):
        self.states, self.volumes, self.calls = list(states), set(volumes), []

    def __call__(self, method, path, body=None, raw=False, timeout=60):
        self.calls.append((method, path.split("?")[0]))
        if path.startswith("/volumes/"):
            if path.split("/")[2] not in self.volumes:
                raise RuntimeError(f"docker GET {path}: 404 no such volume")
            return {}
        if path.startswith("/containers/create"):
            return {"Id": "new"}
        if path == "/containers/new/json":
            restarts, running = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            return {"Id": "new", "RestartCount": restarts, "Config": {"Tty": True},
                    "State": {"Running": running, "ExitCode": 0 if running else 1, "StartedAt": "2026-10-21T10:00:00Z"}}
        return None


@mock.patch.dict(os.environ, {"STEAM_TOKEN_1A": "tok"})
class VerifyTest(unittest.TestCase):
    def swap(self, states, up_line=True, volumes=("data-1", "data-1b"), cfg=None):
        ro = rollover.Rollover({"dry_run": False, "verify_steady_seconds": 0, "verify_minutes": 0.05, "servers": {},
                                **(cfg or {})})
        fake = FakeDocker(states, volumes)
        lines = ["[10:00:01] Main is up on San Vansterdam for 20 players."] if up_line else ["[10:00:01] Signing in to Steam..."]
        with mock.patch.object(rollover, "docker", fake), mock.patch.object(rollover, "hub_has", return_value=True), \
                mock.patch.object(rollover, "logs", return_value=lines), mock.patch.object(ro, "pull"), \
                mock.patch.object(rollover.time, "sleep"), mock.patch.object(ro, "notify") as notify, \
                redirect_stdout(io.StringIO()):
            ro.start_copy("1", SERVER, Old(), "a", "2.1.2", 0, [], "ReSkate 2.1.2 is out")
        return ro, fake.calls, " ".join(str(c) for c in notify.call_args_list)

    def test_up_and_steady_keeps_the_new_one(self):
        ro, calls, said = self.swap([(0, True)])
        self.assertIn(("POST", "/containers/new/start"), calls)
        self.assertNotIn(("POST", "/containers/old/start"), calls)
        self.assertNotIn(("POST", "/containers/new/stop"), calls)
        self.assertEqual(ro.failed, {})

    def test_crash_rolls_back_in_place(self):
        ro, calls, said = self.swap([(0, True), (1, True)], up_line=False)
        self.assertIn(("POST", "/containers/new/stop"), calls)
        self.assertIn(("POST", "/containers/old/start"), calls)
        self.assertIn("did not come up", said)
        self.assertIn(("1", "dudedankdave/reskate-server:2.1.2"), ro.failed)

    def test_failed_image_is_not_tried_again_soon(self):
        ro, _, _ = self.swap([(1, False)], up_line=False)
        fake = FakeDocker([(0, True)])
        with mock.patch.object(rollover, "docker", fake), mock.patch.object(rollover, "hub_has", return_value=True), \
                mock.patch.object(ro, "notify"), redirect_stdout(io.StringIO()):
            ro.start_copy("1", SERVER, Old(), "a", "2.1.2", 0, [], "ReSkate 2.1.2 is out")
        self.assertNotIn(("POST", "/containers/create"), fake.calls)

    def test_still_starting_is_left_running(self):
        ro, calls, said = self.swap([(0, True)], up_line=False)
        self.assertNotIn(("POST", "/containers/new/stop"), calls)
        self.assertNotIn(("POST", "/containers/old/start"), calls)
        self.assertIn("still starting", said)
        self.assertEqual(ro.failed, {})

    def test_missing_slot_volume_stops_the_swap(self):
        ro, calls, said = self.swap([(0, True)], volumes=("data-1b",))
        self.assertNotIn(("POST", "/containers/create"), calls)
        self.assertNotIn(("POST", "/containers/old/stop"), calls)
        self.assertIn("does not exist", said)

    def test_missing_volume_allowed_when_set(self):
        ro, calls, said = self.swap([(0, True)], volumes=(), cfg={"allow_new_volume": True})
        self.assertIn(("POST", "/containers/create"), calls)


if __name__ == "__main__":
    unittest.main()
