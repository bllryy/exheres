import importlib.util
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check-versions.py"
spec = importlib.util.spec_from_file_location("check_versions", SCRIPT)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


class VersionChecks(unittest.TestCase):
    def test_numeric_comparison(self):
        self.assertEqual(checker.compare("1.2.9", "1.2.10"), "update")
        self.assertEqual(checker.compare("2.0", "2.0.0"), "current")
        self.assertEqual(checker.compare("2.1", "2.0"), "ahead")
        self.assertEqual(checker.compare("1.0", "2.0rc1"), "incomparable")
        self.assertEqual(checker.compare("0_pre20260803", "1.0"), "incomparable")

    def test_recipes_and_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "packages/app-misc/example"
            package.mkdir(parents=True)
            (package / "example-0_pre20260803.exheres-0").touch()
            found, problems = checker.recipes(root)
            self.assertEqual(found, {"app-misc/example": "0_pre20260803"})
            self.assertEqual(problems, [])
            text, failed = checker.report(root, {}, "", 0)
            self.assertTrue(failed)
            self.assertIn("unmapped recipe: app-misc/example", text)
            (package / "example-1.0.exheres-0").touch()
            _, problems = checker.recipes(root)
            self.assertIn("multiple versions for app-misc/example", problems)

    def test_result_events(self):
        output = "\n".join([
            json.dumps({"event": "updated", "name": "a/b", "version": "1.2", "level": "info"}),
            json.dumps({"event": "no-result", "name": "c/d", "level": "error"}),
        ])
        self.assertEqual(checker.results(output), ({"a/b": "1.2"}, {"c/d": "no-result"}))

    def test_failed_and_proxy_results(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("net-im/viber", "dev-python/structlog"):
                category, package = name.split("/")
                path = root / "packages" / category / package
                path.mkdir(parents=True)
                (path / f"{package}-1.0.exheres-0").touch()
            config = {name: {"source": "aur"} for name in ("net-im/viber", "dev-python/structlog")}
            output = json.dumps({"event": "updated", "name": "net-im/viber", "version": "1.2", "level": "info"})
            text, failed = checker.report(root, config, output, 3)
            self.assertTrue(failed)
            self.assertIn("update  net-im/viber", text)
            self.assertIn("no version returned", text)
            self.assertIn("1 failed", text)

    def test_kernel_uses_published_index_not_aur(self):
        import tomllib
        with (SCRIPT.parents[1] / "nvchecker.toml").open("rb") as file:
            config = tomllib.load(file)
        self.assertNotIn(checker.KERNEL, config)
        self.assertEqual(config["app-misc/ente-auth"]["aur"], "ente-auth")
        self.assertTrue(config["app-misc/ente-auth"]["strip_release"])

    def test_kernel_published_release(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(checker.UNSUPPORTED, {}, clear=True):
            root = Path(directory)
            package = root / "packages/sys-kernel/kernel-cachyos"
            package.mkdir(parents=True)
            recipe = package / "kernel-cachyos-7.2.9-r1.exheres-0"
            recipe.write_text('MY_RELEASE="${PV}-2"\n')
            for published, expected in (("7.2.9-2", "current"), ("7.2.9-3", "update"),
                                        ("7.3.0-1", "update"), ("7.2.9-1", "ahead")):
                with self.subTest(published=published):
                    text, failed = checker.report(root, {}, "", 0, published=published)
                    self.assertIn(f"1 {checker.LABELS[expected]}", text)
                    self.assertFalse(failed)
                    if expected == "update":
                        self.assertIn(f"7.2.9-2 -> {published}", text)
            text, failed = checker.report(root, {}, "", 0, kernel_error="Cannot download repository index")
            self.assertTrue(failed)
            self.assertIn("failed  sys-kernel/kernel-cachyos", text)
            self.assertIn("Cannot download repository index", text)
            text, failed = checker.report(root, {}, "", 0, published="7.2.9")
            self.assertTrue(failed)
            self.assertIn("unrecognized published release", text)
            recipe.write_text('MY_RELEASE="${PV}-${REL}"\n')
            text, failed = checker.report(root, {}, "", 0, published="7.2.9-2")
            self.assertTrue(failed)
            self.assertIn("unrecognized recipe release", text)
            text, failed = checker.report(root, {checker.KERNEL: {"source": "aur"}}, "", 0,
                                          published="7.2.9-2")
            self.assertTrue(failed)
            self.assertIn("kernel must use published repository index", text)

    def test_tarball_feeds(self):
        import tomllib
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
                self.assertNotIn(name, checker.UNSUPPORTED)
                self.assertEqual(config[name]["source"], "regex")
                self.assertEqual(config[name]["url"], url)
                self.assertEqual(re.findall(config[name]["regex"], html), [version])

    def test_zoom_feed(self):
        import tomllib
        with (SCRIPT.parents[1] / "nvchecker.toml").open("rb") as file:
            config = tomllib.load(file)
        entry = config["net-im/zoom"]
        self.assertEqual(entry["source"], "regex")
        self.assertEqual(entry["url"], "https://zoom.us/rest/download?os=linux")
        response = '{"downloadVO":{"zoom":{"version":"7.2.1.5760"},"zoomX86":{"version":"5.4.53391.1108"}}}'
        self.assertEqual(re.findall(entry["regex"], response), ["7.2.1.5760"])
        self.assertEqual(checker.compare("7.2.1.5760", "7.2.1.5760"), "current")

    def test_repository_coverage(self):
        import tomllib
        root = SCRIPT.parents[1]
        with (root / "nvchecker.toml").open("rb") as file:
            config = tomllib.load(file)
        found, problems = checker.recipes(root)
        self.assertEqual(problems, [])
        self.assertEqual(set(found), (set(config) - {"__config__"}) | set(checker.UNSUPPORTED) | {checker.KERNEL})
        self.assertFalse((set(config) - {"__config__"}) & set(checker.UNSUPPORTED))
        self.assertNotIn(checker.KERNEL, config)


if __name__ == "__main__":
    unittest.main()
