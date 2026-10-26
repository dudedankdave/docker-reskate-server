"""modules/dockerproxy.py end to end: a fake Docker daemon on one unix socket, the proxy on another."""
import io
import json
import os
import socketserver
import sys
import tarfile
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler
from unittest import mock

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [os.path.join(ROOT, "modules")]
import dockerproxy  # noqa: E402

CONTAINERS = {
    "c1": {"Id": "c1", "Name": "/reskate-server-1", "Config": {"Env": ["PORT=12400", "SERVER_PASSWORD=pw"]}},
    "c2": {"Id": "c2", "Name": "/reskate-bot", "Config": {"Env": ["DISCORD_TOKEN=abc", "BOT_ROLE=stats"]}},
}
CALLS = []


class FakeDocker(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def answer(self, status, body=b""):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_any(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        path = dockerproxy.VERSION.sub("", self.path.split("?")[0])
        CALLS.append((self.command, path))
        parts = path.strip("/").split("/")
        if parts[:1] == ["containers"] and len(parts) == 3:
            info = CONTAINERS.get(parts[1]) or next((c for c in CONTAINERS.values() if c["Name"] == "/" + parts[1]), None)
            if not info:
                return self.answer(404, b'{"message":"no such container"}')
            if parts[2] == "json":
                return self.answer(200, json.dumps(info).encode())
            if parts[2] == "restart":
                return self.answer(204)
            return self.answer(200, b"data")
        self.answer(200, b"[]")

    do_GET = do_PUT = do_POST = do_DELETE = handle_any


class Unix(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def get_request(self):
        request, _ = super().get_request()
        return request, ("unix", 0)


def tar(*members):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for name, kind in members:
            info = tarfile.TarInfo(name)
            if kind == "dir":
                info.type = tarfile.DIRTYPE
                t.addfile(info)
            elif kind == "link":
                info.type, info.linkname = tarfile.SYMTYPE, "/mods/packages/x"
                t.addfile(info)
            else:
                info.size = 2
                t.addfile(info, io.BytesIO(b"hi"))
    return buf.getvalue()


class ProxyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        cls.real = os.path.join(cls.dir.name, "docker.sock")
        cls.proxy = os.path.join(cls.dir.name, "proxy.sock")
        cls.fake = Unix(cls.real, FakeDocker)
        threading.Thread(target=cls.fake.serve_forever, daemon=True).start()
        cls.patch = mock.patch.object(dockerproxy, "UPSTREAM", cls.real)
        cls.patch.start()
        cls.server = dockerproxy.Server(cls.proxy, dockerproxy.Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.fake.shutdown()
        cls.patch.stop()
        cls.dir.cleanup()

    def call(self, method, path, body=None):
        conn = dockerproxy.Unix(self.proxy, timeout=5)
        try:
            conn.request(method, "/v1.41" + path, body=body, headers={"Content-Type": "application/x-tar"} if body else {})
            r = conn.getresponse()
            return r.status, r.read()
        finally:
            conn.close()

    def test_reads_pass(self):
        self.assertEqual(self.call("GET", "/containers/json?all=1")[0], 200)
        self.assertEqual(self.call("GET", "/containers/c2/logs?stdout=1&tail=10")[0], 200)

    def test_follow_is_refused(self):
        self.assertEqual(self.call("GET", "/containers/c1/logs?follow=1")[0], 403)

    def test_tool_secrets_are_redacted_server_env_kept(self):
        env = json.loads(self.call("GET", "/containers/c2/json")[1])["Config"]["Env"]
        self.assertEqual(env, ["DISCORD_TOKEN=***", "BOT_ROLE=stats"])
        env = json.loads(self.call("GET", "/containers/c1/json")[1])["Config"]["Env"]
        self.assertIn("SERVER_PASSWORD=pw", env)

    def test_restart_only_for_game_servers(self):
        self.assertEqual(self.call("POST", "/containers/c1/restart?t=20")[0], 204)
        self.assertEqual(self.call("POST", "/containers/c2/restart")[0], 403)
        self.assertEqual(self.call("POST", "/containers/reskate-bot/restart")[0], 403)

    def test_everything_else_is_refused(self):
        for method, path in (("POST", "/containers/create"), ("POST", "/containers/c1/exec"), ("POST", "/containers/c1/stop"),
                             ("POST", "/containers/c1/kill"), ("DELETE", "/containers/c1"), ("GET", "/images/json"),
                             ("POST", "/images/create"), ("GET", "/volumes"), ("GET", "/containers/c2/archive?path=/")):
            CALLS.clear()
            self.assertEqual(self.call(method, path)[0], 403, path)
            self.assertNotIn((method, path.split("?")[0]), CALLS, path)

    def test_put_archive_rules(self):
        self.assertEqual(self.call("PUT", "/containers/c1/archive?path=/data", tar(("world-layers.json", "file")))[0], 200)
        self.assertEqual(self.call("PUT", "/containers/c1/archive?path=/data/Mods", tar(("Owner-Map", "link")))[0], 200)
        self.assertEqual(self.call("PUT", "/containers/c1/archive?path=/etc", tar(("x", "file")))[0], 403)
        self.assertEqual(self.call("PUT", "/containers/c1/archive?path=/data", tar(("../x", "file")))[0], 403)
        self.assertEqual(self.call("PUT", "/containers/c1/archive?path=/data", tar(("a/b", "file")))[0], 403)
        self.assertEqual(self.call("PUT", "/containers/c1/archive?path=/data", tar(("d", "dir")))[0], 403)
        self.assertEqual(self.call("PUT", "/containers/c2/archive?path=/data", tar(("x", "file")))[0], 403)
        self.assertEqual(self.call("PUT", "/containers/c1/archive?path=/data", b"not a tar")[0], 403)


if __name__ == "__main__":
    unittest.main()
