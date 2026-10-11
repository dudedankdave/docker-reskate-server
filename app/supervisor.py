"""Runs the ReSkate server as a child process instead of exec'ing it, so the container can
install a ReSkate update and restart the server without the container restarting.

* Everything typed into the console (docker attach) is passed through to the server, except a
  bare `update`: with UPDATE_MODE=auto that installs a waiting release now (see notifier.py).
* notifier.py talks to the server through send() (`announce` countdowns) and install().
* A server that dies within ROLLBACK_WINDOW of an update is rolled back to the previous version.
* SIGTERM/SIGINT go to the server; when it exits on its own, so does the container, as before.
"""
import os
import signal
import subprocess
import sys
import threading
import time

import updater

CONFIG = "/data/ReSkateServer.json"
ROLLBACK_WINDOW = 180
STEAM_LINK = os.path.expanduser("~/.steam/sdk64/steamclient.so")


class Supervisor:
    def __init__(self, folder, args):
        self.folder, self.args = folder, [a for a in args if a != "--no-update"]
        self.proc = None
        self.lock = threading.Lock()
        self.pending = None          # (staged folder, version) to swap in when the server is down
        self.stopping = False
        self.updated_at = 0.0
        self.on_update = None        # console `update` (set by the notifier)
        self.last_command = ("", 0.0)  # what was typed last and when, for the log feed's formatting
        self.notify = lambda kind, **data: None

    def start(self):
        try:  # steamclient.so is looked up in ~/.steam/sdk64: follow the folder the server runs from
            tmp = STEAM_LINK + ".tmp"
            if os.path.lexists(tmp):
                os.remove(tmp)
            os.symlink(os.path.join(self.folder, "steamclient.so"), tmp)
            os.replace(tmp, STEAM_LINK)
        except OSError as e:
            print(f"[update] could not point {STEAM_LINK} at {self.folder}: {e}", flush=True)
        env = dict(os.environ, LD_LIBRARY_PATH=self.folder)
        cmd = [os.path.join(self.folder, "ReSkateServer"), "--config", CONFIG, "--no-update", *self.args]
        self.proc = subprocess.Popen(cmd, cwd=self.folder, stdin=subprocess.PIPE, env=env)

    def send(self, line):
        """Type a console command into the server. False if it is not running."""
        with self.lock:
            try:
                self.proc.stdin.write(line.encode("utf-8") + b"\n")
                self.proc.stdin.flush()
                return True
            except (OSError, ValueError, AttributeError):
                return False

    def install(self, staged, version):
        """Stop the server, swap the staged release in and start it again (from the main loop)."""
        self.pending = (staged, version)
        proc = self.proc
        if not self.send("quit"):
            proc.terminate()

        def force():
            for wait, act in ((30, proc.terminate), (15, proc.kill)):
                time.sleep(wait)
                if proc.poll() is None:
                    act()
        threading.Thread(target=force, daemon=True).start()

    def relay(self):
        """Console input -> server. Keeps the server's stdin open after our own stdin closes."""
        for raw in iter(sys.stdin.buffer.readline, b""):
            if raw.strip().lower() == b"update" and self.on_update:
                print("[update] checking for a new ReSkate release now", flush=True)
                threading.Thread(target=self.on_update, daemon=True).start()
                continue
            self.last_command = (raw.decode("utf-8", "replace").strip(), time.time())
            with self.lock:
                try:
                    self.proc.stdin.write(raw)
                    self.proc.stdin.flush()
                except (OSError, ValueError):
                    pass

    def stop(self, sig, _frame):
        self.stopping = True
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(sig)

    def run(self):
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGHUP, self.stop)
        self.start()
        threading.Thread(target=self.relay, daemon=True).start()
        while True:
            code = self.proc.wait()
            if self.stopping:
                break
            if self.pending:
                staged, version = self.pending
                self.pending = None
                try:
                    updater.swap_in(staged)
                    self.updated_at = time.time()
                    self.notify("installed", version=version)
                except OSError as e:
                    self.notify("failed", version=version, error=f"could not swap in the new files: {e}")
                self.start()
                continue
            if self.updated_at and time.time() - self.updated_at < ROLLBACK_WINDOW:
                self.updated_at = 0.0
                failed = updater.version_of(updater.DIR)
                if updater.rollback():
                    self.notify("rolled_back", version=failed, code=code, now=updater.version_of(updater.DIR))
                    self.start()
                    continue
            self.notify("exited", code=code)
            time.sleep(2)       # let the notifier post it
            break
        code = self.proc.returncode
        # os._exit, not sys.exit: the relay thread is blocked reading stdin, and interpreter shutdown
        # then aborts on the stdin lock ("Fatal Python error", exit 134) after the server stopped cleanly.
        sys.stdout.flush()
        os._exit(128 - code if code < 0 else code)
