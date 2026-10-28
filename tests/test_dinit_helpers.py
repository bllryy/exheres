import importlib.machinery
import importlib.util
from pathlib import Path
import unittest
import subprocess
import tempfile
import os
import runpy
import io
from unittest.mock import MagicMock, Mock, patch


ROOT = Path(__file__).resolve().parents[1]


def load_helper(name):
    loader = importlib.machinery.SourceFileLoader(
        name, str(ROOT / 'contrib/dinit/helpers' / name))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class ZramTests(unittest.TestCase):
    def setUp(self):
        self.helper = load_helper('zram')
        self.marker = Mock()
        self.marker.is_file.return_value = False
        self.block = MagicMock()
        self.block.is_dir.return_value = False
        self.paths = patch.object(self.helper, 'Path', side_effect=self.path)
        self.paths.start()
        self.addCleanup(self.paths.stop)
        self.commands = patch.object(self.helper, 'run', return_value=0)
        self.run = self.commands.start()
        self.addCleanup(self.commands.stop)
        self.active_patch = patch.object(self.helper, 'active', return_value=False)
        self.active = self.active_patch.start()
        self.addCleanup(self.active_patch.stop)
        self.access = patch.object(self.helper.os, 'access', return_value=False)
        self.access.start()
        self.addCleanup(self.access.stop)
        self.env = patch.dict(self.helper.os.environ)
        self.env.start()
        self.addCleanup(self.env.stop)

    def path(self, value):
        if value.startswith('/run/'):
            return self.marker
        if value.startswith('/sys/'):
            return self.block
        raise AssertionError(value)

    def test_start_delegates_config_parser_unchanged(self):
        self.assertEqual(self.helper.main(['start', 'zram0']), 0)
        self.assertEqual([c.args for c in self.run.call_args_list], [
            ('modprobe', 'zram'), (self.helper.HELPER, 'zram0'),
            ('swapon', '/dev/zram0')])
        self.marker.write_bytes.assert_called_once_with(b'')

    def test_helper_failure_keeps_marker_without_swap_or_cleanup(self):
        self.run.side_effect = [0, 7]
        self.assertEqual(self.helper.main(['start', 'zram0']), 7)
        self.assertEqual(self.run.call_count, 2)
        self.marker.unlink.assert_not_called()

    def test_stop_failure_keeps_marker(self):
        self.marker.is_file.return_value = True
        self.active.return_value = True
        self.run.return_value = 9
        self.assertEqual(self.helper.main(['stop', 'zram0']), 9)
        self.run.assert_called_once_with('swapoff', '/dev/zram0')
        self.marker.unlink.assert_not_called()

    def test_stop_cleanup_order(self):
        self.marker.is_file.return_value = True
        self.active.return_value = True
        self.block.is_dir.side_effect = [True, False]
        self.assertEqual(self.helper.main(['stop', 'zram0']), 0)
        self.assertEqual([c.args for c in self.run.call_args_list], [
            ('swapoff', '/dev/zram0'), (self.helper.HELPER, 'zram0', 'stop')])
        self.marker.unlink.assert_called_once_with(missing_ok=True)

    def test_upstream_stop_failure_keeps_marker(self):
        self.marker.is_file.return_value = True
        self.active.return_value = True
        self.block.is_dir.return_value = True
        self.run.side_effect = [0, 8]
        self.assertEqual(self.helper.main(['stop', 'zram0']), 8)
        self.assertEqual([call.args for call in self.run.call_args_list], [
            ('swapoff', '/dev/zram0'), (self.helper.HELPER, 'zram0', 'stop')])
        self.marker.unlink.assert_not_called()

    def test_malformed_disksize_fails_before_ownership(self):
        self.helper.os.access.return_value = True
        size = self.block.__truediv__.return_value
        stream = size.open.return_value.__enter__.return_value
        for value in ('not-a-size\n', '0', ''):
            with self.subTest(value=value):
                self.run.reset_mock()
                stream.readline.return_value = value
                self.assertEqual(self.helper.main(['start', 'zram0']), 1)
                self.run.assert_called_once_with('modprobe', 'zram')
                self.marker.write_bytes.assert_not_called()

    def test_disksize_read_is_bounded_and_nonzero_refused(self):
        self.helper.os.access.return_value = True
        size = self.block.__truediv__.return_value
        stream = size.open.return_value.__enter__.return_value
        stream.readline.return_value = '128\n'
        self.assertEqual(self.helper.main(['start', 'zram0']), 1)
        stream.readline.assert_called_once_with(64)
        self.marker.write_bytes.assert_not_called()
        self.run.assert_called_once_with('modprobe', 'zram')

    def test_unreadable_disksize_skips_read_like_shell(self):
        self.helper.os.access.return_value = False
        self.assertEqual(self.helper.main(['start', 'zram0']), 0)
        self.block.__truediv__.return_value.open.assert_not_called()

    def test_disksize_open_failure_prevents_ownership(self):
        self.helper.os.access.return_value = True
        size = self.block.__truediv__.return_value
        size.open.side_effect = PermissionError('denied')
        self.assertEqual(self.helper.main(['start', 'zram0']), 1)
        self.run.assert_called_once_with('modprobe', 'zram')
        self.marker.write_bytes.assert_not_called()

    def test_marker_creation_failure_prevents_configuration(self):
        self.marker.write_bytes.side_effect = PermissionError('denied')
        self.assertEqual(self.helper.main(['start', 'zram0']), 1)
        self.run.assert_called_once_with('modprobe', 'zram')
        self.marker.unlink.assert_not_called()

    def test_marker_removal_failure_reports_error_after_cleanup(self):
        self.marker.is_file.return_value = True
        self.active.return_value = True
        self.block.is_dir.side_effect = [True, False]
        self.marker.unlink.side_effect = PermissionError('denied')
        self.assertEqual(self.helper.main(['stop', 'zram0']), 1)
        self.assertEqual([call.args for call in self.run.call_args_list], [
            ('swapoff', '/dev/zram0'), (self.helper.HELPER, 'zram0', 'stop')])
        self.marker.unlink.assert_called_once_with(missing_ok=True)

    def test_run_maps_launch_errors_and_signal_status(self):
        # Exercise real wrapper, not setUp's command mock. Never launch commands.
        self.commands.stop()
        for error, expected in ((FileNotFoundError('missing'), 127),
                                (PermissionError('denied'), 126)):
            with self.subTest(error=error), patch.object(
                    self.helper.subprocess, 'run', side_effect=error) as command:
                self.assertEqual(self.helper.run('modprobe', 'zram'), expected)
                command.assert_called_once_with(('modprobe', 'zram'))
        with patch.object(self.helper.subprocess, 'run',
                          return_value=Mock(returncode=-15)):
            self.assertEqual(self.helper.run('modprobe', 'zram'), 143)

    def test_unreadable_proc_swaps_prevents_start_and_stop_cleanup(self):
        self.active.side_effect = PermissionError('cannot read swaps')
        self.marker.is_file.return_value = True
        for action in ('start', 'stop'):
            with self.subTest(action=action):
                self.run.reset_mock()
                self.assertEqual(self.helper.main([action, 'zram0']), 1)
                if action == 'start':
                    self.run.assert_called_once_with('modprobe', 'zram')
                else:
                    self.run.assert_not_called()
                self.marker.write_bytes.assert_not_called()
                self.marker.unlink.assert_not_called()

    def test_active_streams_exact_device_and_propagates_read_errors(self):
        self.active_patch.stop()
        with patch.object(self.helper, 'Path') as proc:
            stream = proc.return_value.open.return_value.__enter__.return_value
            stream.__iter__.return_value = iter([
                'Filename Type Size Used Priority\n',
                '/dev/zram01 partition 1 0 -2\n',
                '/dev/zram0 partition 1 0 -2\n'])
            self.assertTrue(self.helper.active('zram0'))
            proc.assert_called_once_with('/proc/swaps')
            proc.return_value.open.side_effect = PermissionError('denied')
            with self.assertRaises(PermissionError):
                self.helper.active('zram0')

    def test_swapon_failure_keeps_marker_without_cleanup(self):
        self.run.side_effect = [0, 0, 6]
        self.assertEqual(self.helper.main(['start', 'zram0']), 6)
        self.marker.write_bytes.assert_called_once_with(b'')
        self.marker.unlink.assert_not_called()
        self.assertEqual(self.run.call_count, 3)

    def test_stop_refuses_to_drop_marker_if_block_remains(self):
        self.marker.is_file.return_value = True
        self.block.is_dir.return_value = True
        self.assertEqual(self.helper.main(['stop', 'zram0']), 1)
        self.run.assert_called_once_with(self.helper.HELPER, 'zram0', 'stop')
        self.marker.unlink.assert_not_called()

    def test_unowned_active_device_is_untouched(self):
        self.active.return_value = True
        self.assertEqual(self.helper.main(['start', 'zram0']), 1)
        self.run.assert_called_once_with('modprobe', 'zram')
        self.marker.write_bytes.assert_not_called()

    def test_invalid_devices_never_run_commands(self):
        for device in ('zram100', 'zram-1', 'zram0/../x', 'zram١'):
            self.assertEqual(self.helper.main(['start', device]), 1)
        self.run.assert_not_called()

    def test_unowned_stop_does_nothing(self):
        self.assertEqual(self.helper.main(['stop', 'zram99']), 0)
        self.run.assert_not_called()


class GraphicalTests(unittest.TestCase):
    def test_failed_import_cleans_up_in_order(self):
        helper = load_helper('graphical-env')
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / 'active').touch()
            calls = []
            def run(*args):
                calls.append(args)
                return args != ('dinitctl', '--user', 'start', 'graphical.monitor')
            with patch.dict(os.environ, SWAY_SESSION_MANAGER='dinit',
                            SWAY_SESSION_STATE=directory, WAYLAND_DISPLAY='wayland-1',
                            SWAYSOCK='/tmp/test-sway', DBUS_SESSION_BUS_ADDRESS='test'), \
                    patch.object(helper.shutil, 'which', return_value='/mock'), \
                    patch.object(helper, 'run', side_effect=run):
                self.assertEqual(helper.main(['import']), 1)
            self.assertFalse((state / 'active').exists())
            self.assertEqual(calls[-5][0:4], ('dinitctl', '--user', 'stop', 'graphical.monitor'))
            self.assertEqual(calls[-4][0:3], ('dinitctl', '--user', 'unsetenv'))
            self.assertEqual(calls[-3], ('dinitctl', '--user', 'untrigger', 'graphical.target'))
            self.assertEqual(calls[-2], ('dinitctl', '--user', 'stop', 'graphical.target'))
            self.assertEqual(calls[-1][0], 'dbus-update-activation-environment')


class DifferentialTests(unittest.TestCase):
    def test_zram_shell_equivalence(self):
        old = (ROOT / 'tests/fixtures/zram-original.sh').read_text()
        new = (ROOT / 'contrib/dinit/helpers/zram').read_text()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bindir = root / 'bin'
            bindir.mkdir()
            command = bindir / 'command'
            command.write_text('#!/usr/bin/python3\nimport os,sys\nfrom pathlib import Path\n'
                               'name=Path(sys.argv[0]).name\n'
                               'with open(os.environ["LOG"],"a") as f: f.write(name+" "+" ".join(sys.argv[1:])+"\\n")\n'
                               'if name=="zram-helper" and sys.argv[-1]=="stop" and os.environ.get("REMOVE")=="1":\n'
                               ' import shutil; shutil.rmtree(os.environ["BLOCK"],ignore_errors=True)\n'
                               'sys.exit(int(os.environ.get("FAIL", "0")) if name==os.environ.get("FAIL_COMMAND") else 0)\n')
            command.chmod(0o755)
            for name in ('modprobe', 'swapon', 'swapoff', 'zram-helper'):
                (bindir / name).symlink_to(command)
            for action, owned, active, size, fail in (
                ('start', False, False, None, ''),
                ('start', False, True, None, ''),
                ('start', True, True, None, ''),
                ('start', False, False, '128\n', ''),
                ('start', True, False, '128\n', ''),
                ('start', False, False, None, 'zram-helper'),
                ('start', False, False, None, 'swapon'),
                ('stop', False, True, None, ''),
                ('stop', True, True, '128\n', ''),
                ('stop', True, True, '128\n', 'swapoff'),
                ('stop', True, False, '128\n', 'zram-helper'),
            ):
                results = []
                for language, source in (('sh', old), ('python3', new)):
                    import shutil
                    case = root / 'case'
                    shutil.rmtree(case, ignore_errors=True)
                    case.mkdir()
                    marker = case / 'dinit-zram0.owned'
                    if owned:
                        marker.touch()
                    block = case / 'zram0'
                    if size is not None:
                        block.mkdir()
                        (block / 'disksize').write_text(size)
                    swaps = case / 'swaps'
                    swaps.write_text('/dev/zram0 partition 1 0 -2\n' if active else '')
                    log = case / 'log'
                    source = source.replace('/usr/x86_64-pc-linux-gnu/bin:/usr/bin:/bin', str(bindir)+':/usr/bin:/bin')
                    source = source.replace('/usr/x86_64-pc-linux-gnu/libexec/dinit-chimera/zram', str(bindir/'zram-helper'))
                    source = source.replace('/run/dinit-', str(case)+'/dinit-')
                    source = source.replace('/sys/block/', str(case)+'/')
                    source = source.replace('/proc/swaps', str(swaps))
                    script = root / ('script.'+language)
                    script.write_text(source)
                    env = dict(os.environ, LOG=str(log), BLOCK=str(block), REMOVE='1',
                               FAIL_COMMAND=fail, FAIL='7')
                    process = subprocess.run([language, str(script), action, 'zram0'], env=env,
                                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    results.append((process.returncode, log.read_text() if log.exists() else '',
                                    marker.exists(), block.exists()))
                self.assertEqual(*results, msg=str((action, owned, active, size, fail)))

    def test_chronyd_shell_config_preserves_splitting_and_exports(self):
        source = (ROOT / 'contrib/dinit/helpers/chronyd').read_text()
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'chronyd'
            config.write_text('export CHRONY_TEST=value\nOPTIONS="-f /tmp/chrony.conf *"\n')
            source = source.replace('/etc/sysconfig/chronyd', str(config))
            source = source.replace("metadata=$(/usr/host/bin/stat -Lc '%u %a' -- \"$config\")", "metadata='0 644'")
            captured = []
            def execute(path, args, env):
                captured.append((path, args, env))
            with patch('os.execve', side_effect=execute):
                exec(compile(source, '<chronyd>', 'exec'), {'__name__': 'test'})
            self.assertEqual(captured[0][1], ['/usr/sbin/chronyd', '-n', '-f', '/tmp/chrony.conf', '*'])
            self.assertEqual(captured[0][2]['CHRONY_TEST'], 'value')


if __name__ == '__main__':
    unittest.main()
