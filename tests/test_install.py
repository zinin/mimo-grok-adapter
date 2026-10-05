import contextlib
import importlib.util
import io
import json
import inspect
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
FILES = {
    'mimo-grok-adapter': ROOT / 'mimo-grok-adapter',
    'check-mimo-grok': ROOT / 'check-mimo-grok',
    'mimo-grok-adapter.service': ROOT / 'systemd/mimo-grok-adapter.service',
}


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT / 'install.py').is_file(), 'installer is missing')
        spec = importlib.util.spec_from_file_location('installer_test', ROOT / 'install.py')
        self.installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.installer)
        self.temp = tempfile.TemporaryDirectory(prefix='mimo-install-test-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'home with spaces'
        self.home.mkdir()
        self.config = self.home / '.grok/config.toml'
        self.config.parent.mkdir()
        self.config.write_bytes(b'[models]\ndefault = "unchanged"\n')
        self.config_before = self.config.read_bytes()
        self.spy = self.home / 'systemctl.log'
        self.fakebin = self.home / 'fakebin'
        self.fakebin.mkdir()
        command = self.fakebin / 'systemctl'
        command.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
with open(os.environ['SYSTEMCTL_LOG'], 'a') as log:
    log.write(json.dumps(args) + '\\n')
if 'show-environment' in args and os.environ.get('FAIL_PREFLIGHT') == '1':
    sys.exit(1)
if 'is-enabled' in args:
    sys.exit(0 if os.environ.get('WAS_ENABLED') == '1' else 1)
active = pathlib.Path(os.environ['SYSTEMCTL_LOG'] + '.active')
if 'is-active' in args:
    if active.exists() and os.environ.get('FAIL_AFTER_START') == '1':
        sys.exit(1)
    sys.exit(0 if os.environ.get('WAS_ACTIVE') == '1' or active.exists() else 1)
if 'show' in args:
    print(os.getppid())
if 'restart' in args:
    if os.environ.get('FAIL_RESTART') == '1':
        marker = pathlib.Path(os.environ['SYSTEMCTL_LOG'] + '.failed')
        if not marker.exists():
            marker.touch()
            sys.exit(1)
    active.touch()
if 'stop' in args:
    active.unlink(missing_ok=True)
sys.exit(0)
''')
        command.chmod(0o755)
        self.env = {
            **os.environ, 'HOME': str(self.home), 'SYSTEMCTL_LOG': str(self.spy),
            'PATH': str(self.fakebin) + os.pathsep + os.environ.get('PATH', ''),
            'PYTHONDONTWRITEBYTECODE': '1',
        }

    def targets(self):
        return {
            'mimo-grok-adapter': self.home / '.local/bin/mimo-grok-adapter',
            'check-mimo-grok': self.home / '.local/bin/check-mimo-grok',
            'mimo-grok-adapter.service': self.home / '.config/systemd/user/mimo-grok-adapter.service',
        }

    def cli(self, *args):
        return subprocess.run([sys.executable, '-B', str(ROOT / 'install.py'), *args],
                              env=self.env, text=True, capture_output=True, timeout=15)

    def call_main(self, args, **environment):
        with patch.dict(os.environ, {**self.env, **environment}), \
                patch.object(self.installer, 'wait_for_health'), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return self.installer.main(args)

    def test_file_only_install_uses_existing_paths_and_preserves_grok_config(self):
        result = self.cli('--no-start')
        self.assertEqual(result.returncode, 0, result.stderr)
        for name, target in self.targets().items():
            self.assertEqual(target.read_bytes(), FILES[name].read_bytes())
            self.assertEqual(target.stat().st_mode & 0o777, 0o644 if name.endswith('.service') else 0o755)
        self.assertEqual(self.config.read_bytes(), self.config_before)
        self.assertFalse(self.spy.exists())
        self.assertFalse(list(self.home.rglob('.mimo-install-*')))

    def test_reinstall_updates_files_and_retains_private_backups(self):
        self.assertEqual(self.cli('--no-start').returncode, 0)
        target = self.targets()['mimo-grok-adapter']
        previous = b'previous installed version\n'
        target.write_bytes(previous)
        result = self.cli('--no-start')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(target.read_bytes(), FILES['mimo-grok-adapter'].read_bytes())
        backups = self.home / '.local/state/mimo-grok-adapter/backups'
        matching = [p for p in backups.rglob('mimo-grok-adapter') if p.read_bytes() == previous]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(matching[0].parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.config.read_bytes(), self.config_before)

    def test_symlink_destination_is_refused_before_any_changes(self):
        outside = self.home / 'unrelated-script'
        outside.write_bytes(b'unrelated work\n')
        target = self.targets()['mimo-grok-adapter']
        target.parent.mkdir(parents=True)
        target.symlink_to(outside)
        result = self.cli('--no-start')
        self.assertEqual(result.returncode, 2)
        self.assertTrue(target.is_symlink())
        self.assertEqual(outside.read_bytes(), b'unrelated work\n')
        self.assertFalse(self.targets()['check-mimo-grok'].exists())
        self.assertEqual(self.config.read_bytes(), self.config_before)

    def test_default_install_enables_and_restarts_only_own_user_service(self):
        result = self.call_main([])
        self.assertEqual(result, 0)
        calls = [json.loads(line) for line in self.spy.read_text().splitlines()]
        self.assertIn(['--user', 'daemon-reload'], calls)
        self.assertIn(['--user', 'enable', 'mimo-grok-adapter.service'], calls)
        self.assertIn(['--user', 'restart', 'mimo-grok-adapter.service'], calls)
        self.assertTrue(all(call[0] == '--user' for call in calls))
        self.assertFalse(any('linger' in value for call in calls for value in call))
        self.assertEqual(self.config.read_bytes(), self.config_before)

    def test_failed_restart_restores_previous_files_and_modes(self):
        old = {}
        for name, target in self.targets().items():
            target.parent.mkdir(parents=True, exist_ok=True)
            old[name] = ('old ' + name + '\n').encode()
            target.write_bytes(old[name])
            target.chmod(0o640)
        result = self.call_main([], WAS_ENABLED='1', WAS_ACTIVE='1', FAIL_RESTART='1')
        self.assertEqual(result, 2)
        for name, target in self.targets().items():
            self.assertEqual(target.read_bytes(), old[name])
            self.assertEqual(target.stat().st_mode & 0o777, 0o640)
        calls = [json.loads(line) for line in self.spy.read_text().splitlines()]
        self.assertEqual(calls.count(['--user', 'restart', 'mimo-grok-adapter.service']), 2)
        self.assertFalse(list(self.home.rglob('.mimo-install-*')))
        self.assertEqual(self.config.read_bytes(), self.config_before)

    def test_missing_user_manager_fails_before_installing(self):
        result = self.call_main([], FAIL_PREFLIGHT='1')
        self.assertEqual(result, 2)
        self.assertTrue(all(not path.exists() for path in self.targets().values()))
        self.assertEqual(self.config.read_bytes(), self.config_before)

    def test_new_install_failure_removes_only_its_new_files(self):
        result = self.call_main([], FAIL_RESTART='1')
        self.assertEqual(result, 2)
        self.assertTrue(all(not path.exists() for path in self.targets().values()))
        self.assertTrue(self.config.exists())
        self.assertEqual(self.config.read_bytes(), self.config_before)

    def test_successful_foreign_health_does_not_hide_failed_user_unit(self):
        result = self.call_main([], FAIL_AFTER_START='1')
        self.assertEqual(result, 2)
        self.assertTrue(all(not path.exists() for path in self.targets().values()))
        self.assertEqual(self.config.read_bytes(), self.config_before)

    def test_health_from_another_pid_is_refused(self):
        self.assertIn('expected_pid', inspect.signature(self.installer.wait_for_health).parameters)

        class ForeignHealth(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({'status': 'ok', 'pid': os.getpid() + 1}).encode())

        server = ThreadingHTTPServer(('127.0.0.1', 0), ForeignHealth)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaises(self.installer.InstallError):
                self.installer.wait_for_health(url=f'http://127.0.0.1:{server.server_port}/health',
                                               expected_pid=os.getpid(), timeout=0.1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_health_uses_real_http_with_a_loopback_fixture(self):
        class Health(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'status': 'ok', 'pid': os.getpid()}).encode())

        server = ThreadingHTTPServer(('127.0.0.1', 0), Health)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.installer.wait_for_health(url=f'http://127.0.0.1:{server.server_port}/health',
                                           expected_pid=os.getpid(), timeout=1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
