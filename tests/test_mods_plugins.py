"""app/mods.py install_plugins: the server side of PLUGINS_REQUIRED goes into /data/Plugins."""
import importlib.util
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent.parent


def load():
    spec = importlib.util.spec_from_file_location("mods_under_test", HERE / "app" / "mods.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class InstallPluginsTest(unittest.TestCase):
    def setUp(self):
        self.mods = load()
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.plugins = root / "Plugins"
        self.mods.PLUGINS_DIR = str(self.plugins)
        self.mods.PLUGINS_TMP = str(root / ".plugins-tmp")
        self.sides = {"COCOJAMBO-TOOLS": "both", "SmokeyDawg58-ReSkate_Throwdown": "client"}
        self.versions = {"COCOJAMBO-TOOLS": "1.0.0", "SmokeyDawg58-ReSkate_Throwdown": "2.0.0"}
        self.downloads = []

        def meta(owner, name, version=None):
            return {"version_number": version or self.versions[f"{owner}-{name}"], "download_url": f"https://x/{owner}/{name}"}

        def unpack(owner, name, version, url, work):
            self.downloads.append(f"{owner}-{name}-{version}")
            os.makedirs(work, exist_ok=True)
            with open(os.path.join(work, "manifest.json"), "w") as f:
                json.dump({"name": name, "version_number": version}, f)
            with open(os.path.join(work, "reskate-plugin.json"), "w") as f:
                json.dump({"api": 1, "entry": "plugin/main.lua", "side": self.sides[f"{owner}-{name}"]}, f)
            with open(os.path.join(work, self.mods.MARKER), "w") as f:
                json.dump({"owner": owner, "name": name, "version": version}, f)

        patches = [mock.patch.object(self.mods, "meta", meta), mock.patch.object(self.mods, "unpack", unpack),
                   mock.patch.object(self.mods, "log", lambda m: None)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_only_plugins_with_a_server_side_are_installed(self):
        self.mods.install_plugins("SmokeyDawg58-ReSkate_Throwdown, COCOJAMBO-TOOLS")
        self.assertEqual(sorted(os.listdir(self.plugins)), ["COCOJAMBO-TOOLS"])
        self.assertTrue((self.plugins / "COCOJAMBO-TOOLS" / "reskate-plugin.json").is_file())
        self.assertFalse(os.path.exists(self.mods.PLUGINS_TMP))

    def test_an_installed_version_is_not_downloaded_again(self):
        self.mods.install_plugins("COCOJAMBO-TOOLS")
        self.mods.install_plugins("COCOJAMBO-TOOLS")
        self.assertEqual(self.downloads, ["COCOJAMBO-TOOLS-1.0.0"])

    def test_a_new_version_replaces_the_old_unless_updates_are_off(self):
        self.mods.install_plugins("COCOJAMBO-TOOLS")
        self.versions["COCOJAMBO-TOOLS"] = "1.1.0"
        self.mods.install_plugins("COCOJAMBO-TOOLS", update=False)
        self.assertEqual(self.mods.read_manifest(self.plugins / "COCOJAMBO-TOOLS")["version_number"], "1.0.0")
        self.mods.install_plugins("COCOJAMBO-TOOLS")
        self.assertEqual(self.mods.read_manifest(self.plugins / "COCOJAMBO-TOOLS")["version_number"], "1.1.0")

    def test_a_plugin_no_longer_required_is_removed_but_not_a_hand_copied_one(self):
        self.mods.install_plugins("COCOJAMBO-TOOLS")
        (self.plugins / "Mine").mkdir()
        self.mods.install_plugins("")
        self.assertEqual(os.listdir(self.plugins), ["Mine"])

    def test_a_failure_never_raises(self):
        with mock.patch.object(self.mods, "meta", side_effect=RuntimeError("offline")):
            self.mods.install_plugins("COCOJAMBO-TOOLS")
        self.assertFalse(self.plugins.exists())


if __name__ == "__main__":
    unittest.main()
