import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from package_updater import common, checks, cli
SCRIPT = Path(__file__).resolve().parents[1] / "scripts/update-packages.py"



class PreviewTests(unittest.TestCase):
    def test_nvchecker_invalid_utf8_reports_failure(self):
        error = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid byte")
        with patch.object(checks.subprocess, "run", side_effect=error):
            version, message, fatal = checks.check_entry("app-misc/test", Path("unused"))
        self.assertIsNone(version)
        self.assertIn("cannot run nvchecker", message)
        self.assertTrue(fatal)

    def test_isolated_entry_point_ignores_working_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            Path(temporary, "package_updater.py").write_text("raise RuntimeError('cwd import')")
            result = subprocess.run([sys.executable, "-I", str(SCRIPT), "--help"],
                                    cwd=temporary, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--check", result.stdout)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "nvchecker.toml"
        self.cache = self.root / ".update-packages-cache.json"
        self.entries = {
            "app-misc/one": "github", "app-misc/two": "github",
            "app-misc/three": "github", "app-misc/aur-one": "aur",
            "app-misc/other": "gitlab",
        }
        for name, source in self.entries.items():
            category, package = name.split("/")
            path = self.root / "packages" / category / package
            path.mkdir(parents=True)
            (path / f"{package}-1.0.exheres-0").write_text("# fixture\n")
        self.write_config()

    def write_config(self, changed=False):
        self.config.write_text("[__config__]\nmax_concurrency = 8\n\n" + "\n".join(
            f'["{name}"]\nsource = "{source}"\n'
            + (f'github = "test/{name.split("/")[1]}{("-changed" if changed else "")}"\n'
               if source == "github" else f'{source} = "test"\n')
            for name, source in self.entries.items()))

    def run_preview(self, values=None, *, at=100_000, refresh=False, codes=None):
        calls = []
        values = values or {name: "1.1" for name in self.entries}
        codes = codes or {}

        def run(args, **kwargs):
            self.assertEqual(args.count("--entry"), 1)
            name = args[args.index("--entry") + 1]
            calls.append(name)
            value = values[name]
            if isinstance(value, str) and not value.startswith(("{", "[")):
                output = json.dumps({"name": name, "level": "info", "event": "updated", "version": value})
            else:
                output = value
            return subprocess.CompletedProcess(args, codes.get(name, 0), output, "")

        with patch.object(checks.subprocess, "run", side_effect=run), \
             patch.dict(common.UNSUPPORTED, {}, clear=True):
            rows, fatal = checks.preview(self.root, self.config, self.cache, refresh, at)
        return rows, fatal, calls

    def test_warm_cache_skips_github_only(self):
        rows, fatal, calls = self.run_preview()
        self.assertFalse(fatal)
        self.assertEqual(set(calls), set(self.entries))
        self.assertTrue(all(status == "update" for status, _ in rows.values()))
        cached = json.loads(self.cache.read_text())
        self.assertEqual(set(cached["entries"]), {name for name, source in self.entries.items() if source == "github"})
        del cached["entries"]["app-misc/three"]
        self.cache.write_text(json.dumps(cached))
        rows, fatal, calls = self.run_preview(at=100_060)
        self.assertEqual(set(calls), {"app-misc/three", "app-misc/aur-one", "app-misc/other"})
        self.assertIn("GitHub cache: 1m old", rows["app-misc/one"][1])
        self.assertFalse(fatal)
        _, _, calls = self.run_preview(at=100_061, refresh=True)
        self.assertEqual(set(calls), set(self.entries))

    def test_cache_misses_on_expiry_config_and_corruption(self):
        self.run_preview()
        _, _, calls = self.run_preview(at=100_000 + common.TTL)
        self.assertEqual(set(calls), set(self.entries))
        self.write_config(changed=True)
        _, _, calls = self.run_preview(at=100_001)
        self.assertEqual(set(calls), set(self.entries))
        self.config.write_text(self.config.read_text().replace("max_concurrency = 8", "max_concurrency = 4"))
        _, _, calls = self.run_preview(at=100_001)
        self.assertEqual(set(calls), set(self.entries))
        self.cache.write_text("not json")
        _, _, calls = self.run_preview(at=100_002)
        self.assertEqual(set(calls), set(self.entries))
        self.run_preview(at=100_003)
        data = json.loads(self.cache.read_text())
        data["entries"]["app-misc/one"]["time"] = 100_004
        data["entries"]["app-misc/two"]["version"] = "bad"
        self.cache.write_text(json.dumps(data))
        _, _, calls = self.run_preview(at=100_003)
        self.assertIn("app-misc/one", calls)
        self.assertIn("app-misc/two", calls)
        self.assertNotIn("app-misc/three", calls)

    def test_partial_failure_and_error_precedence(self):
        name = "app-misc/one"
        good = json.dumps({"name": name, "level": "info", "event": "updated", "version": "1.1"})
        bad = json.dumps({"name": name, "level": "error", "event": "no-result"})
        rows, fatal, _ = self.run_preview(values={**{n: "1.1" for n in self.entries}, name: good + "\n" + bad},
                                          codes={name: 3})
        self.assertFalse(fatal)
        self.assertEqual(rows[name], ("failed", "no-result"))
        self.assertNotIn(name, json.loads(self.cache.read_text())["entries"])
        self.assertIn("app-misc/two", json.loads(self.cache.read_text())["entries"])
        rows, _, calls = self.run_preview(at=100_001)
        self.assertIn(name, calls)
        self.assertEqual(rows[name][0], "update")

    def test_detailed_error_survives_no_result(self):
        name = "app-misc/other"
        detail = "HTTP 599: Failed to connect to upstream:443"
        for errors in ((detail, None), (None, detail)):
            events = [json.dumps({
                "name": name, "level": "error",
                "event": "unexpected error happened" if index == 0 else "no-result",
                "error": error,
            }) for index, error in enumerate(errors)]
            with self.subTest(errors=errors), patch.object(checks.subprocess, "run", return_value=
                    subprocess.CompletedProcess([], 3, "\n".join(events), "")):
                self.assertEqual(checks.check_entry(name, self.config), (None, detail, False))

    def test_status_three_cannot_cache_unassigned_failure(self):
        name = "app-misc/one"
        good = json.dumps({"name": name, "level": "info", "event": "updated", "version": "1.1"})
        for extra in ("not-json", json.dumps({"level": "error", "event": "no-result"})):
            for code in (0, 3):
                with self.subTest(extra=extra, code=code):
                    rows, _, _ = self.run_preview(values={**{n: "1.1" for n in self.entries}, name: good + "\n" + extra},
                                                  codes={name: code})
                    self.assertEqual(rows[name][0], "failed")
                    self.assertNotIn(name, json.loads(self.cache.read_text())["entries"])

    def test_timeout_and_oversized_version_are_failures(self):
        name = "app-misc/one"
        with patch.object(checks.subprocess, "run", side_effect=subprocess.TimeoutExpired("nvchecker", 90)):
            version, error, fatal = checks.check_entry(name, self.config)
        self.assertIsNone(version)
        self.assertEqual(error, "nvchecker timed out")
        self.assertTrue(fatal)
        rows, fatal, _ = self.run_preview(values={**{n: "1.1" for n in self.entries}, name: "9" * 5000})
        self.assertFalse(fatal)
        self.assertEqual(rows[name][0], "failed")
        self.assertNotIn(name, json.loads(self.cache.read_text())["entries"])

    def test_empty_error_overrides_success_in_either_order(self):
        name = "app-misc/one"
        good = json.dumps({"name": name, "level": "info", "event": "updated", "version": "1.1"})
        bad = json.dumps({"name": name, "level": "error", "event": ""})
        for output in (good + "\n" + bad, bad + "\n" + good):
            with self.subTest(output=output):
                rows, _, _ = self.run_preview(values={**{n: "1.1" for n in self.entries}, name: output})
                self.assertEqual(rows[name], ("failed", "unknown error"))
                self.assertNotIn(name, json.loads(self.cache.read_text())["entries"])

    def test_exit_three_retains_valid_entry(self):
        rows, fatal, _ = self.run_preview(codes={"app-misc/one": 3})
        self.assertFalse(fatal)
        self.assertEqual(rows["app-misc/one"][0], "update")
        self.assertIn("app-misc/one", json.loads(self.cache.read_text())["entries"])

    def test_bad_events_and_unexpected_exit(self):
        name = "app-misc/one"
        for value in (None, "", "bad", 12):
            with self.subTest(value=value):
                event = json.dumps({"name": name, "level": "info", "event": "updated", "version": value})
                rows, _, _ = self.run_preview(values={**{n: "1.1" for n in self.entries}, name: event},
                                              at=100_000)
                self.assertEqual(rows[name][0], "failed")
                self.assertNotIn(name, json.loads(self.cache.read_text())["entries"])
        rows, fatal, _ = self.run_preview(values={**{n: "1.1" for n in self.entries}, name: "[]\nnot-json"},
                                          codes={name: 1})
        self.assertTrue(fatal)
        self.assertEqual(rows[name][0], "failed")

    def test_current_ahead_and_failed_are_distinct(self):
        values = {name: "1.0" for name in self.entries}
        values["app-misc/one"] = "0.9"
        values["app-misc/two"] = json.dumps({"name": "app-misc/two", "level": "error", "event": "no-result"})
        rows, fatal, _ = self.run_preview(values=values)
        self.assertFalse(fatal)
        self.assertEqual(rows["app-misc/one"][0], "ahead")
        self.assertEqual(rows["app-misc/three"][0], "current")
        self.assertEqual(rows["app-misc/two"], ("failed", "no-result"))
        self.assertNotIn("app-misc/two", json.loads(self.cache.read_text())["entries"])

    def test_untracked_recipe_and_missing_lookup(self):
        unsupported = self.root / "packages/dev-libs/tlottie"
        unsupported.mkdir(parents=True)
        (unsupported / "tlottie-1.0.exheres-0").write_text("# fixture\n")
        with patch.dict(common.UNSUPPORTED, {"dev-libs/tlottie": "pinned commit"}, clear=True):
            with patch.object(checks.subprocess, "run") as run:
                run.side_effect = lambda args, **kwargs: subprocess.CompletedProcess(args, 0, "", "")
                rows, fatal = checks.preview(self.root, self.config, self.cache, now=100_000)
        self.assertFalse(fatal)
        self.assertEqual(rows["dev-libs/tlottie"], ("untracked", "pinned commit"))
        self.assertEqual(rows["app-misc/one"][0], "failed")
        self.assertNotIn("app-misc/one", json.loads(self.cache.read_text())["entries"] if self.cache.exists() else {})

    def test_duplicate_and_mismatched_events(self):
        name = "app-misc/one"
        good = json.dumps({"name": name, "level": "info", "event": "updated", "version": "1.1"})
        wrong = json.dumps({"name": "app-misc/elsewhere", "level": "info", "event": "updated", "version": "1.1"})
        rows, _, _ = self.run_preview(values={**{n: "1.1" for n in self.entries}, name: wrong + "\n" + good + "\n" + good})
        self.assertEqual(rows[name][0], "failed")
        self.assertNotIn(name, json.loads(self.cache.read_text())["entries"])

    def test_cli_reports_failure_without_recipe_changes(self):
        original = {path: path.read_bytes() for path in (self.root / "packages").glob("*/*/*.exheres-0")}
        with patch.object(cli, "preview", return_value=({"app-misc/one": ("failed", "no-result")}, False)), \
             patch("builtins.print") as output:
            self.assertEqual(cli.main([]), 1)
        self.assertTrue(any("no-result" in str(call) for call in output.call_args_list))
        self.assertEqual({path: path.read_bytes() for path in original}, original)
        with patch.object(cli, "preview", return_value=({"app-misc/one": ("current", "1.0 -> 1.0")}, False)), \
             patch("builtins.print"):
            self.assertEqual(cli.main([]), 0)

    def test_default_summary_and_verbose_details(self):
        rows = {"app-misc/one": ("current", "1.0 -> 1.0 (GitHub cache: 5m old)"),
                "app-misc/two": ("update", "1.0 -> 1.1"),
                "app-misc/three": ("failed", "no-result"),
                "app-misc/other": ("untracked", "pinned commit")}
        with patch.object(cli, "preview", return_value=(rows, False)), \
             patch("builtins.print") as printed:
            self.assertEqual(cli.main([]), 1)
        text = "\n".join(str(call) for call in printed.call_args_list)
        self.assertIn("1 to update, 1 failed, 1 not tracked, 1 up to date", text)
        self.assertIn("app-misc/two", text)
        self.assertIn("app-misc/three", text)
        self.assertNotIn("app-misc/one", text)
        self.assertNotIn("app-misc/other", text)
        with patch.object(cli, "preview", return_value=(rows, False)), \
             patch("builtins.print") as printed:
            self.assertEqual(cli.main(["--verbose"]), 1)
        text = "\n".join(str(call) for call in printed.call_args_list)
        self.assertIn("app-misc/one", text)
        self.assertIn("GitHub cache: 5m old", text)
        self.assertIn("app-misc/other", text)

    def test_invalid_or_failed_selection_never_prepares_or_drafts(self):
        cases = (
            (["--apply", "app-misc/unknown"], {"app-misc/one": ("update", "1.0 -> 1.1")}),
            (["--apply", "app-misc/one"], {"app-misc/one": ("failed", "no-result")}),
            (["--apply", "app-misc/one"], {"app-misc/one": ("current", "1.0 -> 1.0")}),
        )
        for arguments, rows in cases:
            with self.subTest(arguments=arguments, rows=rows), \
                 patch.object(cli, "preview", return_value=(rows, False)), \
                 patch.object(cli, "prepare_recipe") as prepare, \
                 patch.object(cli, "update") as kernel, \
                 patch("builtins.print") as printed:
                cli.main(arguments)
            prepare.assert_not_called()
            kernel.assert_not_called()
            self.assertFalse(any("Draft:" in str(call) for call in printed.call_args_list))
        with patch.object(cli, "preview") as preview:
            with patch("sys.stderr"), self.assertRaises(SystemExit):
                cli.main(["--apply", "app-misc/one", "app-misc/one"])
        preview.assert_not_called()

    def test_all_reported_updates_prepare_and_draft(self):
        rows = {"games-misc/hydra-launcher": ("update", "4.1.6 -> 4.2.0"),
                "sys-kernel/scx": ("update", "1.1.3 -> 1.1.4"),
                "app-misc/one": ("current", "1.0 -> 1.0")}

        def run_preview(*, versions_out, **kwargs):
            versions_out.update({"games-misc/hydra-launcher": "4.2.0", "sys-kernel/scx": "1.1.4"})
            return rows, False

        with patch.object(cli, "preview", side_effect=run_preview), \
             patch.object(cli, "prepare_recipe", side_effect=lambda root, name, version:
                  f"update {name.split('/')[1]} to {version}") as prepare, \
             patch("builtins.print") as printed:
            self.assertEqual(cli.main(["--all-eligible"]), 0)
        self.assertEqual(prepare.call_count, 2)
        self.assertTrue(any("Draft: update hydra-launcher to 4.2.0" in str(call) for call in printed.call_args_list))
        self.assertTrue(any("Draft: update scx to 1.1.4" in str(call) for call in printed.call_args_list))

    def test_kernel_apply_reuses_snapshot_and_drafts_on_success(self):
        kernel = self.root / "packages/sys-kernel/kernel-cachyos"
        kernel.mkdir(parents=True)
        (kernel / "kernel-cachyos-7.2.9.exheres-0").write_text('MY_RELEASE="${PV}-2"\n')
        snapshot = ("7.2.9-3", {"linux-cachyos-eevdf-lto": "a" * 64, "linux-cachyos-eevdf-lto-headers": "b" * 64})
        def run_preview(*, versions_out, kernel_out, **kwargs):
            kernel_out.append(snapshot)
            return {common.KERNEL: ("update", "7.2.9-2 -> 7.2.9-3")}, False
        with patch.object(cli, "preview", side_effect=run_preview), \
             patch.object(cli, "update", return_value="7.2.9-3") as apply, \
             patch("builtins.print") as printed:
            self.assertEqual(cli.main(["--apply", common.KERNEL]), 0)
        apply.assert_called_once_with(checks.ROOT, published=snapshot)
        self.assertTrue(any("Draft: update kernel-cachyos to 7.2.9-3" in str(call) for call in printed.call_args_list))
        with patch.object(cli, "update", return_value="7.2.11-1"), \
             patch.object(cli, "preview", side_effect=run_preview), \
             patch("builtins.print") as printed:
            self.assertEqual(cli.main(["--apply", common.KERNEL]), 0)
        self.assertTrue(any("Draft: update kernel-cachyos to 7.2.11" in str(call) for call in printed.call_args_list))
        self.assertFalse(any("7.2.11-1" in str(call) for call in printed.call_args_list))
        with patch.object(cli, "preview", side_effect=run_preview), \
             patch.object(cli, "update", return_value=None), \
             patch("builtins.print") as printed:
            self.assertEqual(cli.main(["--apply", common.KERNEL]), 0)
        self.assertFalse(any("Draft:" in str(call) for call in printed.call_args_list))

    def test_kernel_live_and_untracked(self):
        kernel = self.root / "packages/sys-kernel/kernel-cachyos"
        kernel.mkdir(parents=True)
        (kernel / "kernel-cachyos-7.2.9.exheres-0").write_text('MY_RELEASE="${PV}-2"\n')
        with patch.object(checks, "published_release", return_value=("7.2.9-3", {})) as published:
            for when in (100_000, 100_001):
                rows, fatal, _ = self.run_preview(at=when)
                self.assertEqual(rows[common.KERNEL][0], "update")
                self.assertFalse(fatal)
            self.assertEqual(published.call_count, 2)
        self.assertNotIn(common.KERNEL, json.loads(self.cache.read_text())["entries"])

    def test_kernel_preview_hides_pkgrel_one_only(self):
        kernel = self.root / "packages/sys-kernel/kernel-cachyos"
        kernel.mkdir(parents=True)
        (kernel / "kernel-cachyos-7.2.10.exheres-0").write_text('MY_RELEASE="${PV}-1"\n')
        with patch.object(checks, "published_release", return_value=("7.2.11-1", {})):
            rows, fatal, _ = self.run_preview(at=100_000)
        self.assertEqual(rows[common.KERNEL], ("update", "7.2.10 -> 7.2.11"))
        self.assertFalse(fatal)
        with patch.object(checks, "published_release", return_value=("7.2.11-2", {})):
            rows, _, _ = self.run_preview(at=100_001)
        self.assertEqual(rows[common.KERNEL], ("update", "7.2.10 -> 7.2.11-2"))

    def test_preview_rejects_version_files(self):
        for setting in ('oldver = ""', 'newver = ""'):
            with self.subTest(setting=setting):
                self.write_config()
                self.config.write_text(self.config.read_text().replace('max_concurrency = 8', setting))
                with self.assertRaisesRegex(ValueError, "version file writes"):
                    checks.preview(self.root, self.config, self.cache, now=100_000)
                self.assertFalse(self.cache.exists())


if __name__ == "__main__":
    unittest.main()
