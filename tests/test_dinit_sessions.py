"""Isolated session lifecycle checks: no real service commands run."""
import os
from pathlib import Path
import signal
import socket
import subprocess
import tempfile
import types
import unittest
from unittest.mock import Mock, patch


HELPER = Path(__file__).resolve().parents[1] / 'contrib/dinit/helpers/sway-session'


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.module = types.ModuleType('sway_session_test')
        self.module.__file__ = str(HELPER)
        exec(compile(HELPER.read_text(), str(HELPER), 'exec'), self.module.__dict__)
        self.temp = tempfile.TemporaryDirectory(prefix='dinit-session-test-')
        self.addCleanup(self.temp.cleanup)
        self.calls = []
        self.handlers = {}
        self.fail_cleanup = False
        self.child = Mock()
        self.child.wait.return_value = 0
        self.child.poll.return_value = 0
        self.spawn_env = None
        env = {'XDG_RUNTIME_DIR': self.temp.name, 'DBUS_SESSION_BUS_ADDRESS': 'mock:bus',
               'PATH': '/mock', 'SSH_AUTH_SOCK': '/old/agent'}
        for context in (
            patch.dict(os.environ, env, clear=True),
            patch.object(self.module.shutil, 'which', return_value='/mock/tool'),
            patch.object(self.module.signal, 'signal', side_effect=lambda s, h: self.handlers.update({s: h})),
            patch.object(self.module.subprocess, 'run', side_effect=self.run_command),
            patch.object(self.module.subprocess, 'Popen', side_effect=self.spawn),
        ):
            context.start()
            self.addCleanup(context.stop)

    def run_command(self, command, **kwargs):
        self.calls.append(tuple(command))
        status = int(self.fail_cleanup and command[-1] == 'cleanup')
        return subprocess.CompletedProcess(command, status, stdout=b'')

    def spawn(self, command):
        self.spawn_command = command
        self.spawn_env = dict(os.environ)
        return self.child

    def test_dinit_agent_environment_arguments_and_restore(self):
        self.assertEqual(self.module.main(['--debug', 'two words']), 0)
        self.assertEqual(self.spawn_command, ['sway', '--debug', 'two words'])
        self.assertIn(('dinitctl', '--user', 'start', 'gcr-ssh-agent'), self.calls)
        self.assertEqual(self.spawn_env['SSH_AUTH_SOCK'], self.temp.name + '/gcr/ssh')
        self.assertEqual(self.spawn_env['MOZ_ENABLE_WAYLAND'], '1')
        self.assertEqual(self.spawn_env['QT_QPA_PLATFORM'], 'wayland')
        self.assertEqual(self.spawn_env['SDL_VIDEODRIVER'], 'wayland')
        self.assertIn(('dinitctl', '--user', 'setenv', 'SSH_AUTH_SOCK=/old/agent'), self.calls)
        self.assertFalse(list(Path(self.temp.name).iterdir()))
        self.child.wait.assert_called_once()

    def test_missing_previous_agent_is_unset(self):
        del os.environ['SSH_AUTH_SOCK']
        self.assertEqual(self.module.main([]), 0)
        self.assertIn(('dinitctl', '--user', 'unsetenv', 'SSH_AUTH_SOCK'), self.calls)

    def test_cleanup_failure_retains_state_and_fails(self):
        self.fail_cleanup = True
        self.assertEqual(self.module.main([]), 1)
        self.assertTrue(Path(self.spawn_env['SWAY_SESSION_STATE']).is_dir())
        self.assertIn(('dinitctl', '--user', 'setenv', 'SSH_AUTH_SOCK=/old/agent'), self.calls)

    def test_systemd_lifecycle(self):
        os.environ['SWAY_SESSION_MANAGER'] = 'systemd'
        self.assertEqual(self.module.main([]), 0)
        self.assertIn(('systemctl', '--user', 'start', 'sway-session.target'), self.calls)
        self.assertIn(('systemctl', '--user', 'stop', 'sway-session.target'), self.calls)
        self.assertIn((str(HELPER.with_name('graphical-env')), 'cleanup'), self.calls)
        self.assertNotIn(('dinitctl', '--user', 'start', 'gcr-ssh-agent'), self.calls)

    def test_bus_inferred_only_from_socket(self):
        del os.environ['DBUS_SESSION_BUS_ADDRESS']
        with socket.socket(socket.AF_UNIX) as bus:
            bus.bind(self.temp.name + '/bus')
            self.assertEqual(self.module.main([]), 0)
        self.assertEqual(self.spawn_env['DBUS_SESSION_BUS_ADDRESS'], 'unix:path=' + self.temp.name + '/bus')

    def test_signal_forwarded_and_child_reaped(self):
        def wait():
            self.child.wait.side_effect = None
            self.child.wait.return_value = -signal.SIGINT
            self.child.poll.side_effect = [None, -signal.SIGINT, -signal.SIGINT]
            self.handlers[signal.SIGINT](signal.SIGINT, None)
        self.child.wait.side_effect = wait
        self.assertEqual(self.module.main([]), 130)
        self.child.send_signal.assert_called_once_with(signal.SIGINT)
        self.assertEqual(self.child.wait.call_count, 2)
        self.child.kill.assert_not_called()

    def test_signal_during_spawn_is_forwarded_after_child_registered(self):
        original = self.spawn
        def spawn(command):
            self.handlers[signal.SIGHUP](signal.SIGHUP, None)
            child = original(command)
            child.poll.side_effect = [None, -signal.SIGHUP, -signal.SIGHUP]
            return child
        with patch.object(self.module.subprocess, 'Popen', side_effect=spawn):
            self.assertEqual(self.module.main([]), 129)
        self.child.send_signal.assert_called_once_with(signal.SIGHUP)
        self.child.wait.assert_called_once()

    def test_child_failure_survives_cleanup_failure(self):
        self.child.wait.return_value = 7
        self.fail_cleanup = True
        self.assertEqual(self.module.main([]), 7)

    def test_systemd_cleanup_failure_fails(self):
        os.environ['SWAY_SESSION_MANAGER'] = 'systemd'
        self.fail_cleanup = True
        self.assertEqual(self.module.main([]), 1)

    def test_startup_failure_restores_agent_without_spawning(self):
        original = self.run_command
        def run(command, **kwargs):
            response = original(command, **kwargs)
            if tuple(command) == ('dinitctl', '--user', 'setenv', 'SSH_AUTH_SOCK'):
                response.returncode = 1
            return response
        with patch.object(self.module.subprocess, 'run', side_effect=run):
            self.assertEqual(self.module.main([]), 1)
        self.assertIsNone(self.spawn_env)
        self.assertIn(('dinitctl', '--user', 'setenv', 'SSH_AUTH_SOCK=/old/agent'), self.calls)


class GraphicalTests(unittest.TestCase):
    def setUp(self):
        path = HELPER.with_name('graphical-env')
        self.module = types.ModuleType('graphical_test')
        exec(compile(path.read_text(), str(path), 'exec'), self.module.__dict__)

    def test_each_import_failure_rolls_back_all_cleanup_steps(self):
        for failure in range(6):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory(prefix='graphical-test-') as tmp:
                state = Path(tmp)
                (state / 'active').touch()
                calls = []
                def run(*command):
                    calls.append(command)
                    return len(calls) != failure + 1
                with patch.dict(os.environ, {'SWAY_SESSION_MANAGER': 'dinit', 'SWAY_SESSION_STATE': tmp,
                        'WAYLAND_DISPLAY': 'wayland-1', 'SWAYSOCK': '/mock/sway',
                        'DBUS_SESSION_BUS_ADDRESS': 'mock:bus'}, clear=True), \
                        patch.object(self.module.shutil, 'which', return_value='/mock/tool'), \
                        patch.object(self.module, 'run', side_effect=run):
                    self.assertEqual(self.module.main(['import']), 1)
                self.assertFalse((state / 'active').exists())
                self.assertEqual(len(calls), failure + 1 + 5)
                self.assertEqual(calls[-1][0], 'dbus-update-activation-environment')

    def test_cleanup_attempts_all_steps_after_failure(self):
        with tempfile.TemporaryDirectory(prefix='graphical-test-') as tmp:
            with patch.object(self.module, 'run', return_value=False) as run:
                self.assertEqual(self.module.cleanup(Path(tmp)), 1)
                self.assertEqual(run.call_count, 5)

    def test_systemd_import_empty_desktop_defaults_and_argument_boundaries(self):
        env = {'SWAY_SESSION_MANAGER': 'systemd', 'WAYLAND_DISPLAY': 'wayland-1',
               'SWAYSOCK': '/mock/two words', 'XDG_CURRENT_DESKTOP': '', 'XDG_SESSION_DESKTOP': ''}
        with patch.dict(os.environ, env, clear=True), patch.object(self.module.os, 'execvp') as execute:
            self.module.main(['import'])
        command = execute.call_args.args[1]
        self.assertIn('SWAYSOCK=/mock/two words', command)
        self.assertIn('XDG_CURRENT_DESKTOP=sway', command)
        self.assertIn('XDG_SESSION_DESKTOP=sway', command)


if __name__ == '__main__':
    unittest.main()
