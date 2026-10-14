import importlib.util
import io
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


updater = load("update_kernel_cachyos", "update-kernel-cachyos.py")
checker = load("check_versions_revisions", "check-versions.py")
TEMPLATE = '''MY_RELEASE="${PV}-1"
SLOT="0"
src_unpack() {
    case ${archive} in
        "${MY_KERNEL}") expected=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa ;;
        "${MY_HEADERS}") expected=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb ;;
    esac
}
'''


class KernelUpdates(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.package = self.root / "packages/sys-kernel/kernel-cachyos"
        self.package.mkdir(parents=True)
        self.recipe("7.2.9", TEMPLATE)

    def recipe(self, version, text):
        path = self.package / f"kernel-cachyos-{version}.exheres-0"
        path.write_text(text)
        return path

    def run_update(self, version, error=None):
        urls = []

        def download(url, destination):
            urls.append(url)
            if error == "download" and "headers" in url:
                raise updater.UpdateError("no matching headers")
            destination.write_bytes(b"archive")
            if error == "hash" and "headers" in url:
                return "c" * 64
            return "a" * 64 if "headers" not in url else "b" * 64

        def inspect(_archive, name, _release):
            if error == "inspect" and name == "linux-cachyos-headers":
                raise updater.UpdateError("mismatched headers")

        hashes = {"linux-cachyos": "a" * 64, "linux-cachyos-headers": "b" * 64}
        with patch.object(updater, "published_release", return_value=(version, hashes)), \
             patch.object(updater, "download", side_effect=download), \
             patch.object(updater, "inspect_archive", side_effect=inspect):
            updater.update(self.root)
        return urls

    def names(self):
        return sorted(path.name for path in self.package.iterdir())

    def test_same_pv_replaces_previous_recipe(self):
        urls = self.run_update("7.2.9-2")
        self.assertEqual(
            urls,
            [
                f"{updater.CDN}/linux-cachyos-7.2.9-2-x86_64_v3.pkg.tar.zst",
                f"{updater.CDN}/linux-cachyos-headers-7.2.9-2-x86_64_v3.pkg.tar.zst",
            ],
        )
        self.assertEqual(self.names(), ["kernel-cachyos-7.2.9-r1.exheres-0"])
        revised = (self.package / "kernel-cachyos-7.2.9-r1.exheres-0").read_text()
        self.assertIn('MY_RELEASE="${PV}-2"', revised)
        self.assertIn('SLOT="0"', revised)
        self.assertIn("a" * 64, revised)
        self.assertIn("b" * 64, revised)
        found, problems = checker.recipes(self.root)
        self.assertEqual(problems, [])
        self.assertEqual(found["sys-kernel/kernel-cachyos"], "7.2.9-r1")
        self.assertEqual(checker.kernel_release(self.root, "7.2.9-r1"), "7.2.9-2")

    def test_next_pkgrel_and_new_version(self):
        self.run_update("7.2.9-2")
        self.run_update("7.2.9-3")
        self.assertEqual(self.names(), ["kernel-cachyos-7.2.9-r2.exheres-0"])
        self.run_update("7.3.0-1")
        self.assertEqual(self.names(), ["kernel-cachyos-7.3.0.exheres-0"])

    def test_noop_and_failure_preserve_recipes(self):
        before = self.names()
        self.run_update("7.2.9-1")
        self.assertEqual(self.names(), before)
        failures = (("7.2.9-3", None), ("7.2.9-2", "download"),
                    ("7.2.9-2", "hash"), ("7.2.9-2", "inspect"))
        for version, error in failures:
            with self.subTest(version=version, error=error), self.assertRaises(updater.UpdateError):
                self.run_update(version, error)
            self.assertEqual(self.names(), before)

    def test_duplicate_and_legacy_slot_are_rejected(self):
        old = self.recipe("7.2.8", TEMPLATE)
        found, problems = checker.recipes(self.root)
        self.assertIn("keep only one kernel-cachyos recipe", problems)
        self.assertIn("sys-kernel/kernel-cachyos", found)
        with patch.object(updater, "published_release") as published, self.assertRaises(updater.UpdateError):
            updater.update(self.root)
        published.assert_not_called()
        old.unlink()
        current = self.package / "kernel-cachyos-7.2.9.exheres-0"
        current.write_text(TEMPLATE.replace('SLOT="0"', 'SLOT="${PV}"'))
        with patch.object(updater, "published_release") as published, self.assertRaises(updater.UpdateError):
            updater.update(self.root)
        published.assert_not_called()
        self.assertEqual(self.names(), ["kernel-cachyos-7.2.9.exheres-0"])

    def test_bad_index_response_preserves_recipes(self):
        before = self.names()
        from urllib.error import HTTPError
        failure = HTTPError(updater.INDEX, 404, "missing", {}, None)
        with patch.object(updater.urllib.request, "urlopen", side_effect=failure):
            with self.assertRaises(updater.UpdateError):
                updater.update(self.root)
        self.assertEqual(self.names(), before)

    def test_repository_index_selects_matching_release(self):
        def entry(name, release, digest="a" * 64, filename=None):
            filename = filename or f"{name}-{release}-x86_64_v3.pkg.tar.zst"
            return (
                f"%NAME%\n{name}\n\n%VERSION%\n{release}\n\n%FILENAME%\n{filename}\n\n"
                f"%SHA256SUM%\n{digest}\n\n%ARCH%\nx86_64_v3\n"
            ).encode()

        def index(items):
            raw = io.BytesIO()
            with tarfile.open(fileobj=raw, mode="w") as tar:
                for name, release, metadata in items:
                    info = tarfile.TarInfo(f"{name}-{release}/desc")
                    info.size = len(metadata)
                    tar.addfile(info, io.BytesIO(metadata))
            return subprocess.run(["zstd", "-cq"], input=raw.getvalue(), stdout=subprocess.PIPE, check=True).stdout

        kernel, headers = "linux-cachyos", "linux-cachyos-headers"
        items = [
            (kernel, "7.2.9-2", entry(kernel, "7.2.9-2")),
            (headers, "7.2.9-2", entry(headers, "7.2.9-2", "b" * 64)),
            (kernel, "7.2.10-1", entry(kernel, "7.2.10-1")),
        ]
        with patch.object(updater.urllib.request, "urlopen", return_value=io.BytesIO(index(items))):
            self.assertEqual(updater.published_release(), ("7.2.9-2", {kernel: "a" * 64, headers: "b" * 64}))
        items.append((headers, "7.2.10-1", entry(headers, "7.2.10-1", "b" * 64)))
        with patch.object(updater.urllib.request, "urlopen", return_value=io.BytesIO(index(items))):
            self.assertEqual(updater.published_release()[0], "7.2.10-1")
        with patch.object(updater.urllib.request, "urlopen", return_value=io.BytesIO(index(items[:1]))):
            with self.assertRaises(updater.UpdateError):
                updater.published_release()
        items[-1] = (headers, "7.2.10-1", entry(headers, "7.2.10-1", filename="wrong.pkg.tar.zst"))
        with patch.object(updater.urllib.request, "urlopen", return_value=io.BytesIO(index(items))):
            with self.assertRaises(updater.UpdateError):
                updater.published_release()
        with patch.object(updater.urllib.request, "urlopen", return_value=io.BytesIO(b"<html>placeholder</html>")):
            with self.assertRaises(updater.UpdateError):
                updater.published_release()

    def test_archive_metadata_and_paths(self):
        def archive(name, unsafe=False):
            path = self.root / f"{name}.tar.zst"
            module = "usr/lib/modules/7.2.9-2-cachyos"
            items = {
                ".PKGINFO": f"pkgname = {name}\npkgver = 7.2.9-2\narch = x86_64_v3\n".encode(),
            }
            if name == "linux-cachyos":
                items.update({f"{module}/vmlinuz": b"kernel", f"{module}/kernel/example.ko.zst": b"module"})
            else:
                items.update({f"{module}/build/{part}": b"header" for part in (".config", "Module.symvers", "System.map")})
                items[f"{module}/vdso/vdso64.so"] = b"vdso"
            if unsafe:
                items["../outside"] = b"bad"
            raw = io.BytesIO()
            with tarfile.open(fileobj=raw, mode="w") as tar:
                for item, data in items.items():
                    info = tarfile.TarInfo(item)
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
            import subprocess
            path.write_bytes(subprocess.run(["zstd", "-cq"], input=raw.getvalue(), stdout=subprocess.PIPE, check=True).stdout)
            return path

        updater.inspect_archive(archive("linux-cachyos"), "linux-cachyos", "7.2.9-2")
        updater.inspect_archive(archive("linux-cachyos-headers"), "linux-cachyos-headers", "7.2.9-2")
        with self.assertRaises(updater.UpdateError):
            updater.inspect_archive(archive("linux-cachyos", unsafe=True), "linux-cachyos", "7.2.9-2")
        with self.assertRaises(updater.UpdateError):
            updater.inspect_archive(archive("linux-cachyos"), "linux-cachyos", "7.2.9-3")

    def test_directory_cannot_stand_in_for_required_file(self):
        archive = self.root / "invalid.tar.zst"
        module = "usr/lib/modules/7.2.9-2-cachyos"
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w") as tar:
            metadata = b"pkgname = linux-cachyos\npkgver = 7.2.9-2\narch = x86_64_v3\n"
            info = tarfile.TarInfo(".PKGINFO")
            info.size = len(metadata)
            tar.addfile(info, io.BytesIO(metadata))
            for name in (f"{module}/vmlinuz", f"{module}/kernel/example"):
                info = tarfile.TarInfo(name)
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
        import subprocess
        archive.write_bytes(subprocess.run(["zstd", "-cq"], input=raw.getvalue(), stdout=subprocess.PIPE, check=True).stdout)
        with self.assertRaises(updater.UpdateError):
            updater.inspect_archive(archive, "linux-cachyos", "7.2.9-2")


if __name__ == "__main__":
    unittest.main()
