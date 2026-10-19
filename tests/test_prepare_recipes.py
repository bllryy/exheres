import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/update-packages.py"
spec = importlib.util.spec_from_file_location("update_packages_prepare", SCRIPT)
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)

GUARD = '''src_unpack() {
    local checksum
    checksum=$(sha256sum "${FETCHEDDIR}/${PNV}.deb") || die
    [[ ${checksum%% *} == ''' + "a" * 64 + ''' ]] || die
}
'''


class RecipePreparation(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.package = self.root / "packages/games-misc/hydra-launcher"
        self.package.mkdir(parents=True)
        self.source = self.package / "hydra-launcher-4.1.6.exheres-0"
        self.source.write_text('DOWNLOADS="https://example.org/${PV}.deb -> ${PNV}.deb"\n')
        original_run = subprocess.run

        def clean_git(args, **kwargs):
            if args[0] == "git":
                return subprocess.CompletedProcess(args, 0, "", "")
            return original_run(args, **kwargs)

        self.git = patch.object(updater.subprocess, "run", side_effect=clean_git)
        self.git.start()
        self.addCleanup(self.git.stop)

    def test_rename_keeps_bytes_without_download(self):
        original = self.source.read_bytes()
        with patch.object(updater, "download_checksum") as download:
            subject = updater.prepare_recipe(self.root, "games-misc/hydra-launcher", "4.2.0")
        self.assertEqual(subject, "update hydra-launcher to 4.2.0")
        self.assertFalse(self.source.exists())
        self.assertEqual((self.package / "hydra-launcher-4.2.0.exheres-0").read_bytes(), original)
        download.assert_not_called()

    def test_bad_version_collision_and_dirty_source_do_not_publish(self):
        target = self.package / "hydra-launcher-4.2.0.exheres-0"
        with self.assertRaisesRegex(updater.PreparationError, "newer numeric"):
            updater.prepare_recipe(self.root, "games-misc/hydra-launcher", "4.1.6")
        target.write_text("already here")
        with self.assertRaisesRegex(updater.PreparationError, "already exists"):
            updater.prepare_recipe(self.root, "games-misc/hydra-launcher", "4.2.0")
        self.assertEqual(target.read_text(), "already here")
        target.unlink()
        with patch.object(updater.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, " M source", "")):
            with self.assertRaisesRegex(updater.PreparationError, "local changes"):
                updater.prepare_recipe(self.root, "games-misc/hydra-launcher", "4.2.0")
        self.assertTrue(self.source.exists())
        self.assertFalse(target.exists())

    def test_untracked_source_refused(self):
        def untracked(args, **kwargs):
            if args[:2] == ["git", "ls-files"]:
                raise subprocess.CalledProcessError(1, args)
            return subprocess.CompletedProcess(args, 0, "", "")

        with patch.object(updater.subprocess, "run", side_effect=untracked):
            with self.assertRaisesRegex(updater.PreparationError, "not tracked"):
                updater.prepare_recipe(self.root, "games-misc/hydra-launcher", "4.2.0")
        self.assertTrue(self.source.exists())

    def test_multiple_recipes_and_bad_source_refused(self):
        second = self.package / "hydra-launcher-4.1.5.exheres-0"
        second.write_text("# old")
        with self.assertRaisesRegex(updater.PreparationError, "exactly one"):
            updater.prepare_recipe(self.root, "games-misc/hydra-launcher", "4.2.0")
        second.unlink()
        self.source.unlink()
        self.source.symlink_to(self.package / "elsewhere")
        with self.assertRaises(updater.PreparationError):
            updater.prepare_recipe(self.root, "games-misc/hydra-launcher", "4.2.0")

    def test_element_requires_manual_review(self):
        with self.assertRaisesRegex(updater.PreparationError, "Electron pin require manual review"):
            updater.prepare_recipe(self.root, "net-im/element-desktop", "1.12.33")
        self.assertTrue(self.source.exists())

    def test_scx_and_loader_rename_independently(self):
        for package in ("scx", "scx-loader"):
            with self.subTest(package=package):
                directory = self.root / "packages/sys-kernel" / package
                directory.mkdir(parents=True)
                (directory / f"{package}-1.1.3.exheres-0").write_text("# fixture\n")
                with patch("builtins.print") as printed:
                    subject = updater.prepare_recipe(self.root, f"sys-kernel/{package}", "1.1.4")
                self.assertEqual(subject, f"update {package} to 1.1.4")
                printed.assert_not_called()

    def test_checksum_patterns_and_versionless_url(self):
        fixtures = (
            ("app-misc/ente-auth", "4.4.25", "4.4.26", "AppImage",
             "https://github.com/ente-io/ente/releases/download/auth-v${PV}/ente-auth-v${PV}-x86_64.AppImage"),
            ("net-vpn/mullvad-vpn", "2026.5", "2026.6", "deb",
             "https://cdn.mullvad.net/app/desktop/releases/${PV}/MullvadVPN-${PV}_amd64.deb"),
            ("net-im/viber", "27.3.0.2", "27.4.0.0", "deb",
             "https://download.cdn.viber.com/cdn/desktop/Linux/viber.deb"),
        )
        for name, old, new, extension, url in fixtures:
            with self.subTest(name=name):
                package = name.split("/")[1]
                directory = self.root / "packages" / name
                directory.mkdir(parents=True)
                source = directory / f"{package}-{old}.exheres-0"
                recipe = (f'DOWNLOADS="{url} -> ${{PNV}}.{extension}"\n'
                          f'src_unpack() {{\n    checksum=$(sha256sum "${{FETCHEDDIR}}/${{PNV}}.{extension}") || die\n'
                          f'    [[ ${{checksum%% *}} == {"a" * 64} ]] || die\n}}\n')
                source.write_text(recipe)
                with patch.object(updater, "download_checksum", return_value="b" * 64) as download:
                    updater.prepare_recipe(self.root, name, new)
                download.assert_called_once_with(url.replace("${PV}", new))
                updated = (directory / f"{package}-{new}.exheres-0").read_text()
                self.assertEqual(updated, recipe.replace("a" * 64, "b" * 64))

    def test_ambiguous_checksum_or_download_failure_preserves_recipe(self):
        self.source.write_text('DOWNLOADS="https://example.org/${PV}.deb -> ${PNV}.deb"\n' + GUARD)
        original = self.source.read_bytes()
        target = self.package / "hydra-launcher-4.2.0.exheres-0"
        with patch.object(updater, "download_checksum", side_effect=updater.PreparationError("download failed")):
            with self.assertRaisesRegex(updater.PreparationError, "download failed"):
                updater.prepare_recipe(self.root, "games-misc/hydra-launcher", "4.2.0")
        self.assertEqual(self.source.read_bytes(), original)
        self.assertFalse(target.exists())
        self.source.write_text(self.source.read_text() + GUARD)
        with self.assertRaisesRegex(updater.PreparationError, "exactly one SHA-256"):
            updater.prepare_recipe(self.root, "games-misc/hydra-launcher", "4.2.0")
        self.assertFalse(target.exists())

    def test_versioned_patch_requires_manual_review(self):
        self.source.write_text('DEFAULT_SRC_PREPARE_PATCHES=( "${FILES}/${PNV}-fix.patch" )\n')
        with self.assertRaisesRegex(updater.PreparationError, "Version-dependent files"):
            updater.prepare_recipe(self.root, "games-misc/hydra-launcher", "4.2.0")
        self.assertTrue(self.source.exists())
        self.assertFalse((self.package / "hydra-launcher-4.2.0.exheres-0").exists())

    def test_download_checksum_rejects_bad_redirect_and_oversize(self):
        origin = "https://github.com/test/project/releases/download/v1/archive.deb"
        redirect = updater.ChecksumRedirect(origin)
        with self.assertRaisesRegex(updater.PreparationError, "redirect"):
            redirect.redirect_request(None, None, 302, "redirect", {}, "https://example.net/archive.deb")

        class Response:
            def __init__(self, data):
                self.data = data
                self.reads = 0

            def geturl(self):
                return origin

            def read(self, size):
                self.reads += 1
                return self.data if self.reads == 1 else b""

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        with patch.object(updater.urllib.request, "build_opener") as build, \
             patch.object(updater, "MAX_SOURCE", 3):
            build.return_value.open.return_value = Response(b"four")
            with self.assertRaisesRegex(updater.PreparationError, "size limit"):
                updater.download_checksum(origin)
        with patch.object(updater.urllib.request, "build_opener") as build:
            short = Response(b"ok")
            short.headers = {"Content-Length": "4"}
            build.return_value.open.return_value = short
            with self.assertRaisesRegex(updater.PreparationError, "Incomplete"):
                updater.download_checksum(origin)
        with patch.object(updater.urllib.request, "build_opener") as build:
            complete = Response(b"ok")
            complete.headers = {"Content-Length": "2"}
            build.return_value.open.return_value = complete
            self.assertEqual(updater.download_checksum(origin),
                             updater.hashlib.sha256(b"ok").hexdigest())

    def test_source_change_during_checksum_aborts(self):
        self.source.write_text('DOWNLOADS="https://example.org/${PV}.deb -> ${PNV}.deb"\n' + GUARD)
        changed = self.source.read_text() + "# changed\n"

        def mutate(url):
            self.source.write_text(changed)
            return "b" * 64

        with patch.object(updater, "download_checksum", side_effect=mutate):
            with self.assertRaisesRegex(updater.PreparationError, "changed"):
                updater.prepare_recipe(self.root, "games-misc/hydra-launcher", "4.2.0")
        self.assertEqual(self.source.read_text(), changed)
        self.assertFalse((self.package / "hydra-launcher-4.2.0.exheres-0").exists())

    def test_unlink_failure_rolls_back(self):
        original_unlink = Path.unlink

        def refuse_source(path, *args, **kwargs):
            if path == self.source:
                raise OSError("cannot unlink")
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", refuse_source):
            with self.assertRaisesRegex(updater.PreparationError, "cannot unlink"):
                updater.prepare_recipe(self.root, "games-misc/hydra-launcher", "4.2.0")
        self.assertTrue(self.source.exists())
        self.assertFalse((self.package / "hydra-launcher-4.2.0.exheres-0").exists())


if __name__ == "__main__":
    unittest.main()
