import importlib.util
import re
import tempfile
import tomllib
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/update-packages.py"
spec = importlib.util.spec_from_file_location("update_packages_checks", SCRIPT)
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)


class VersionChecks(unittest.TestCase):
    def test_numeric_comparison(self):
        self.assertEqual(updater.compare("1.2.9", "1.2.10"), "update")
        self.assertEqual(updater.compare("2.0", "2.0.0"), "current")
        self.assertEqual(updater.compare("2.1", "2.0"), "ahead")
        self.assertEqual(updater.compare("1.0", "2.0rc1"), "incomparable")
        self.assertEqual(updater.compare("0_pre20260803", "1.0"), "incomparable")

    def test_recipes_detect_multiple_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "packages/app-misc/example"
            package.mkdir(parents=True)
            (package / "example-0_pre20260803.exheres-0").touch()
            found, problems = updater.recipes(root)
            self.assertEqual(found, {"app-misc/example": "0_pre20260803"})
            self.assertEqual(problems, [])
            (package / "example-1.0.exheres-0").touch()
            _, problems = updater.recipes(root)
            self.assertIn("multiple versions for app-misc/example", problems)

    def test_kernel_uses_published_index_not_aur(self):
        with (SCRIPT.parents[1] / "nvchecker.toml").open("rb") as file:
            config = tomllib.load(file)
        self.assertNotIn(updater.KERNEL, config)
        self.assertEqual(config["app-misc/ente-auth"]["aur"], "ente-auth")
        self.assertTrue(config["app-misc/ente-auth"]["strip_release"])

    def test_kernel_recipe_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "packages/sys-kernel/kernel-cachyos"
            package.mkdir(parents=True)
            recipe = package / "kernel-cachyos-7.2.9-r1.exheres-0"
            recipe.write_text('MY_RELEASE="${PV}-2"\n')
            self.assertEqual(updater.kernel_release(root, "7.2.9-r1"), "7.2.9-2")
            recipe.write_text('MY_RELEASE="${PV}-${REL}"\n')
            self.assertIsNone(updater.kernel_release(root, "7.2.9-r1"))

    def test_tarball_feeds(self):
        with (SCRIPT.parents[1] / "nvchecker.toml").open("rb") as file:
            config = tomllib.load(file)
        examples = {
            "media-libs/zita-convolver": (
                'https://kokkinizita.linuxaudio.org/linuxaudio/downloads/',
                '<a href="zita-convolver-4.0.3.tar.bz2">',
                "4.0.3",
            ),
            "net-firewall/ufw": (
                'https://launchpad.net/ufw/+download',
                '<a href="https://launchpad.net/ufw/0.36/0.36.2/+download/ufw-0.36.2.tar.gz">',
                "0.36.2",
            ),
        }
        for name, (url, html, version) in examples.items():
            with self.subTest(name=name):
                self.assertNotIn(name, updater.UNSUPPORTED)
                self.assertEqual(config[name]["source"], "regex")
                self.assertEqual(config[name]["url"], url)
                self.assertEqual(re.findall(config[name]["regex"], html), [version])

    def test_electron_stays_on_stable_44(self):
        with (SCRIPT.parents[1] / "nvchecker.toml").open("rb") as file:
            entry = tomllib.load(file)["dev-util/electron44"]
        self.assertEqual(entry["github"], "electron/electron")
        self.assertTrue(entry["use_max_release"])
        self.assertEqual(entry["prefix"], "v")
        for tag in ("v44.9.0", "v44.10.2", "v45.0.0", "v44.11.0-beta.1"):
            with self.subTest(tag=tag):
                self.assertEqual(bool(re.fullmatch(entry["include_regex"], tag)), tag in ("v44.9.0", "v44.10.2"))

    def test_zoom_feed(self):
        with (SCRIPT.parents[1] / "nvchecker.toml").open("rb") as file:
            config = tomllib.load(file)
        entry = config["net-im/zoom"]
        self.assertEqual(entry["source"], "regex")
        self.assertEqual(entry["url"], "https://zoom.us/rest/download?os=linux")
        response = '{"downloadVO":{"zoom":{"version":"7.2.1.5760"},"zoomX86":{"version":"5.4.53391.1108"}}}'
        self.assertEqual(re.findall(entry["regex"], response), ["7.2.1.5760"])
        self.assertEqual(updater.compare("7.2.1.5760", "7.2.1.5760"), "current")

    def test_repository_coverage(self):
        root = SCRIPT.parents[1]
        with (root / "nvchecker.toml").open("rb") as file:
            config = tomllib.load(file)
        found, problems = updater.recipes(root)
        self.assertEqual(problems, [])
        self.assertEqual(set(found), (set(config) - {"__config__"}) | set(updater.UNSUPPORTED) | {updater.KERNEL})
        self.assertFalse((set(config) - {"__config__"}) & set(updater.UNSUPPORTED))
        self.assertNotIn(updater.KERNEL, config)


if __name__ == "__main__":
    unittest.main()
