import contextlib
import importlib.machinery
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


PLUGIN = Path(__file__).resolve().parents[1] / 'contrib/kernel-install/50-mkinitcpio.install'
loader = importlib.machinery.SourceFileLoader('mkinitcpio_install', str(PLUGIN))
spec = importlib.util.spec_from_loader(loader.name, loader)
plugin = importlib.util.module_from_spec(spec)
loader.exec_module(plugin)


class MkinitcpioInstallTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.staging = Path(temporary.name)
        self.kernel = self.staging / 'vmlinuz'
        self.kernel.write_bytes(b'kernel')
        self.args = ['add', '7.3.0-2-cachyos', '/boot/token/version', str(self.kernel)]
        env = patch.dict(os.environ, {
            'KERNEL_INSTALL_INITRD_GENERATOR': 'mkinitcpio',
            'KERNEL_INSTALL_LAYOUT': 'uki',
            'KERNEL_INSTALL_IMAGE_TYPE': 'pe',
            'KERNEL_INSTALL_STAGING_AREA': str(self.staging),
        }, clear=True)
        env.start()
        self.addCleanup(env.stop)
        mock = patch.object(plugin.subprocess, 'run', side_effect=self.generate)
        self.run = mock.start()
        self.addCleanup(mock.stop)

    def generate(self, command, **kwargs):
        Path(command[-1]).write_bytes(b'initramfs')
        return subprocess.CompletedProcess(command, 0)

    def invoke(self, args=None):
        with contextlib.redirect_stderr(io.StringIO()):
            return plugin.main(self.args if args is None else args)

    def test_generate_for_bls_and_uki(self):
        for layout in ('bls', 'uki'):
            with self.subTest(layout=layout):
                os.environ['KERNEL_INSTALL_LAYOUT'] = layout
                self.assertEqual(self.invoke(), 0)
                command = self.run.call_args.args[0]
                self.assertEqual(command[:4], ['mkinitcpio', '-k', self.args[1], '-g'])
                self.assertEqual(Path(command[-1]).parent.parent, self.staging)
                target = self.staging / 'initrd'
                self.assertEqual(target.read_bytes(), b'initramfs')
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)
                target.unlink()
        self.assertEqual(sorted(p.name for p in self.staging.iterdir()), ['vmlinuz'])

    def test_skip_conditions(self):
        for key, value in (
            ('KERNEL_INSTALL_INITRD_GENERATOR', 'dracut'),
            ('KERNEL_INSTALL_INITRD_GENERATOR', ''),
            ('KERNEL_INSTALL_IMAGE_TYPE', 'uki'),
            ('KERNEL_INSTALL_LAYOUT', 'other'),
            ('KERNEL_INSTALL_LAYOUT', ''),
        ):
            with self.subTest(key=key, value=value), patch.dict(os.environ, {key: value}):
                self.assertEqual(self.invoke(), 0)
        for args in (['remove'], ['inspect'], self.args + ['/supplied/initrd'],
                     self.args + ['/microcode', '/initramfs']):
            self.assertEqual(self.invoke(args), 0)
        self.run.assert_not_called()

    def test_invalid_arguments(self):
        for args in ([], ['add'], self.args[:3]):
            self.assertEqual(self.invoke(args), 1)
        for version in ('', '-preset', '../escape', 'bad/name', 'bad\nname'):
            with self.subTest(version=version):
                self.assertEqual(self.invoke(['add', version, *self.args[2:]]), 1)
        for index, value in ((2, 'relative'), (3, '/nonexistent/kernel')):
            args = self.args.copy()
            args[index] = value
            self.assertEqual(self.invoke(args), 1)
        self.run.assert_not_called()

    def test_invalid_staging(self):
        for value in ('', '/', 'relative', str(self.kernel), str(self.staging / 'missing')):
            with self.subTest(value=value), patch.dict(os.environ, {'KERNEL_INSTALL_STAGING_AREA': value}):
                self.assertEqual(self.invoke(), 1)
        self.run.assert_not_called()

    def test_existing_output_preserved(self):
        target = self.staging / 'initrd'
        target.write_bytes(b'existing')
        self.assertEqual(self.invoke(), 1)
        self.assertEqual(target.read_bytes(), b'existing')
        target.unlink()
        target.symlink_to(self.staging / 'missing')
        self.assertEqual(self.invoke(), 1)
        self.assertTrue(target.is_symlink())
        self.run.assert_not_called()

    def test_failed_generator_cleans_partial_output(self):
        for code in (1, 42, 77, -15):
            def fail(command, **kwargs):
                self.generate(command)
                return subprocess.CompletedProcess(command, code)
            self.run.side_effect = fail
            self.assertEqual(self.invoke(), code if code in (1, 42) else 1)
            self.assertEqual(list(self.staging.iterdir()), [self.kernel])

    def test_missing_executable(self):
        self.run.side_effect = FileNotFoundError('mkinitcpio')
        self.assertEqual(self.invoke(), 1)
        self.assertEqual(list(self.staging.iterdir()), [self.kernel])

    def test_invalid_output(self):
        for kind in ('missing', 'empty', 'symlink', 'directory'):
            def generate(command, **kwargs):
                output = Path(command[-1])
                if kind == 'empty':
                    output.touch()
                elif kind == 'symlink':
                    output.symlink_to(self.kernel)
                elif kind == 'directory':
                    output.mkdir()
                return subprocess.CompletedProcess(command, 0)
            self.run.side_effect = generate
            self.assertEqual(self.invoke(), 1)
            self.assertEqual(list(self.staging.iterdir()), [self.kernel])

    def test_publication_does_not_overwrite_racing_writer(self):
        def race(command, **kwargs):
            self.generate(command)
            (self.staging / 'initrd').write_bytes(b'other writer')
            return subprocess.CompletedProcess(command, 0)
        self.run.side_effect = race
        self.assertEqual(self.invoke(), 1)
        self.assertEqual((self.staging / 'initrd').read_bytes(), b'other writer')
        self.assertFalse(list(self.staging.glob('.mkinitcpio-*')))

    def test_executable_install_extension(self):
        self.assertEqual(PLUGIN.suffix, '.install')
        self.assertTrue(os.access(PLUGIN, os.X_OK))


if __name__ == '__main__':
    unittest.main()
