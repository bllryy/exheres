import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check-versions.py"
spec = importlib.util.spec_from_file_location("check_versions", SCRIPT)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


class VersionChecks(unittest.TestCase):
    def test_numeric_comparison(self):
        self.assertEqual(checker.compare("1.2.9", "1.2.10"), "newer")
        self.assertEqual(checker.compare("2.0", "2.0.0"), "current")
        self.assertEqual(checker.compare("2.1", "2.0"), "behind")
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
            self.assertIn("proxy-newer", text)
            self.assertIn("no version returned", text)
            self.assertIn("failed=1", text)

    def test_repository_coverage(self):
        import tomllib
        root = SCRIPT.parents[1]
        with (root / "nvchecker.toml").open("rb") as file:
            config = tomllib.load(file)
        found, problems = checker.recipes(root)
        self.assertEqual(problems, [])
        self.assertEqual(set(found), (set(config) - {"__config__"}) | set(checker.UNSUPPORTED))
        self.assertFalse((set(config) - {"__config__"}) & set(checker.UNSUPPORTED))


if __name__ == "__main__":
    unittest.main()
