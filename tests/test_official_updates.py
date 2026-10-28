import base64
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from package_updater import common, prepare as prepare_mod, cli
SCRIPT = Path(__file__).resolve().parents[1] / "scripts/update-packages.py"



class OfficialUpdates(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / "packages/app-misc/test"
        self.directory.mkdir(parents=True)
        self.old = "test-1.0.exheres-0"
        self.new = "test-2.0.exheres-0"
        self.base = b"header\n\nupstream=old\n\nlocal=old\n"
        self.origin = {"repository": "https://gitlab.exherbo.org/exherbo/arbor",
                       "package": "app-misc/test", "commit": "a" * 40,
                       "recipe": self.old, "local_recipe": self.old, "mode": "overlay",
                       "baseline": {self.old: base64.b64encode(self.base).decode()}}
        (self.directory / self.old).write_bytes(self.base)

    def plan(self, incoming):
        with patch.object(prepare_mod, "official_files", return_value={self.new: incoming}):
            return prepare_mod.prepare_official(self.root, "app-misc/test", "2.0", self.origin)

    def test_mirror_update_and_import(self):
        incoming = self.base.replace(b"upstream=old", b"upstream=new")
        plan = self.plan(incoming)
        self.assertEqual((self.directory / self.old).read_bytes(), self.base)
        prepare_mod.write_official(plan)
        self.assertEqual((self.directory / self.new).read_bytes(), incoming)
        self.assertFalse((self.directory / self.old).exists())
        (self.directory / self.new).unlink()
        prepare_mod.write_official(self.plan(incoming))
        self.assertEqual((self.directory / self.new).read_bytes(), incoming)

    def test_overlay_preserved_and_conflicts_closed(self):
        local = self.base.replace(b"local=old", b"local=custom")
        (self.directory / self.old).write_bytes(local)
        plan = self.plan(self.base.replace(b"upstream=old", b"upstream=new"))
        self.assertIn(b"local=custom", plan[2][self.new])
        with self.assertRaises(common.PreparationError):
            self.plan(self.base.replace(b"local=old", b"local=upstream"))
        self.assertEqual((self.directory / self.old).read_bytes(), local)

    def test_edit_after_review_rejected(self):
        plan = self.plan(self.base)
        (self.directory / self.old).write_bytes(b"edited")
        with self.assertRaises(common.PreparationError):
            prepare_mod.write_official(plan)
        self.assertFalse((self.directory / self.new).exists())

    def test_network_failure_no_write(self):
        with patch.object(prepare_mod.urllib.request, "build_opener", side_effect=OSError("offline")):
            with self.assertRaises(common.PreparationError):
                prepare_mod.official_files(self.origin, "2.0")
        self.assertEqual(list(self.directory.iterdir()), [self.directory / self.old])

    def test_interactive_cancel(self):
        rows = {"app-misc/test": ("update", "1.0 -> 2.0")}
        with patch.object(cli, "preview", return_value=(rows, False)), patch.object(cli, "official_config", return_value={}), patch("builtins.input", return_value=""), patch("builtins.print"), patch.object(cli, "prepare_recipe") as prepare:
            self.assertEqual(cli.main(["--interactive"]), 0)
        prepare.assert_not_called()

    def test_check_does_not_prepare(self):
        with patch.object(cli, "preview", return_value=({}, False)), patch.object(cli, "official_config", return_value={}), patch.object(cli, "prepare_recipe") as prepare, patch("builtins.print"):
            self.assertEqual(cli.main(["--check"]), 0)
        prepare.assert_not_called()


if __name__ == "__main__":
    unittest.main()
