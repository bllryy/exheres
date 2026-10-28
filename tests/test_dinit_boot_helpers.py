"""Mocked boot-helper regressions; no installed daemons or boot actions."""
import ast
import os
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HELPERS = ROOT / 'contrib/dinit/helpers'


class BootHelperTests(unittest.TestCase):
    def invoke_chronyd(self, config_text=None, metadata='0 600'):
        source = (HELPERS / 'chronyd').read_text()
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config'
            if config_text is not None:
                config.write_text(config_text.replace('TEST_DIRECTORY', shlex.quote(directory)))
            source = source.replace('/etc/sysconfig/chronyd', str(config))
            source = source.replace("metadata=$(/usr/host/bin/stat -Lc '%u %a' -- \"$config\")",
                                    'metadata=' + shlex.quote(metadata))
            with patch('os.execve') as execute, patch('os.chdir') as chdir, patch('os.umask') as mask:
                exec(compile(source, '<chronyd>', 'exec'), {'__name__': 'test'})
            return execute, chdir, mask, directory

    def test_chronyd_preserves_trusted_config_state(self):
        execute, chdir, mask, directory = self.invoke_chronyd(
            'cd TEST_DIRECTORY\numask 027\nexport CHRONYD_TEST=kept\n'
            'OPTIONS="-f relative.conf *.conf"\n')
        chdir.assert_called_once_with(directory)
        mask.assert_called_once_with(0o27)
        path, args, env = execute.call_args.args
        self.assertEqual(path, '/usr/sbin/chronyd')
        self.assertEqual(args, [path, '-n', '-f', 'relative.conf', '*.conf'])
        self.assertEqual(env['CHRONYD_TEST'], 'kept')

    def test_chronyd_without_config(self):
        execute, _, _, _ = self.invoke_chronyd()
        self.assertEqual(execute.call_args.args[1], ['/usr/sbin/chronyd', '-n'])

    def test_chronyd_rejects_unsafe_config_before_sourcing(self):
        for metadata in ('1000 600', '0 620', '0 602'):
            with self.subTest(metadata=metadata), self.assertRaises(SystemExit) as error:
                self.invoke_chronyd('exit 23\n', metadata)
            self.assertEqual(error.exception.code, 1)

    def test_chronyd_propagates_config_failure(self):
        with self.assertRaises(SystemExit) as error:
            self.invoke_chronyd('exit 23\n')
        self.assertEqual(error.exception.code, 23)

    def test_boot_service_helper_references(self):
        checked = set()
        for service in (ROOT / 'contrib/dinit/services').iterdir():
            if not service.is_file() or service.name.startswith('zram'):
                continue
            for line in service.read_text().splitlines():
                if '/etc/dinit.d/helpers/' not in line:
                    continue
                command = shlex.split(line.split('=', 1)[1])
                helper = next(word for word in command if word.startswith('/etc/dinit.d/helpers/'))
                path = HELPERS / Path(helper).name
                self.assertTrue(path.is_file(), str(service))
                self.assertTrue(os.access(path, os.X_OK), str(path))
                ast.parse(path.read_text(), filename=str(path))
                checked.add(path.name)
        self.assertTrue({'chronyd', 'preflight', 'libvirt-ready', 'cgroups'} <= checked)


if __name__ == '__main__':
    unittest.main()
