import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
URL = 'https://github.com/zinin/mimo-grok-adapter.git'


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.script = ROOT / 'install.sh'
        self.assertTrue(self.script.is_file(), 'shell bootstrap is missing')
        self.temp = tempfile.TemporaryDirectory(prefix='mimo-bootstrap-test-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'home with spaces'
        self.home.mkdir()
        self.config = self.home / '.grok/config.toml'
        self.config.parent.mkdir()
        self.config.write_bytes(b'[models]\ndefault="unchanged"\n')
        self.fakebin = self.home / 'fakebin'
        self.fakebin.mkdir()
        self.log = self.home / 'git.log'
        self.remote = self.home / 'remote-fixture'
        self.remote.mkdir()
        for relative in ('install.py', 'mimo-grok-adapter', 'check-mimo-grok', 'systemd/mimo-grok-adapter.service'):
            target = self.remote / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, target)
        fakegit = self.fakebin / 'git'
        fakegit.write_text('''#!/usr/bin/env python3
import json, os, pathlib, shutil, sys
args = sys.argv[1:]
with open(os.environ['GIT_LOG'], 'a') as log:
    log.write(json.dumps(args) + '\\n')
if args[0] == 'clone':
    target = pathlib.Path(args[-1])
    shutil.copytree(os.environ['REMOTE_FIXTURE'], target)
    (target / '.git').mkdir()
    sys.exit(0)
if args[:1] == ['-C']:
    directory, args = pathlib.Path(args[1]), args[2:]
    if args == ['rev-parse', '--show-toplevel']:
        print(directory.resolve())
    elif args == ['remote', 'get-url', 'origin']:
        print(os.environ.get('FAKE_ORIGIN', 'https://github.com/zinin/mimo-grok-adapter.git'))
    elif args == ['symbolic-ref', '--quiet', '--short', 'HEAD']:
        print(os.environ.get('FAKE_BRANCH', 'master'))
    elif args[:1] == ['status']:
        print(os.environ.get('FAKE_DIRTY', ''), end='')
    elif args != ['pull', '--ff-only', 'origin', 'master']:
        sys.exit(2)
    sys.exit(0)
sys.exit(2)
''')
        fakegit.chmod(0o755)
        self.env = {**os.environ, 'HOME': str(self.home), 'GIT_LOG': str(self.log),
                    'REMOTE_FIXTURE': str(self.remote),
                    'PATH': str(self.fakebin) + os.pathsep + os.environ.get('PATH', ''),
                    'PYTHONDONTWRITEBYTECODE': '1'}
        self.env.pop('MIMO_GROK_REPO_DIR', None)
        self.env.pop('XDG_DATA_HOME', None)

    def stream(self, **changes):
        return subprocess.run(['bash', '-s', '--', '--no-start'],
                              input=self.script.read_text(), env={**self.env, **changes},
                              text=True, capture_output=True, timeout=20)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_stream_clones_master_and_installs_to_standard_paths(self):
        result = self.stream()
        self.assertEqual(result.returncode, 0, result.stderr)
        checkout = self.home / '.local/share/mimo-grok-adapter'
        self.assertTrue((checkout / '.git').is_dir())
        call = self.calls()[0]
        self.assertEqual(call[0], 'clone')
        self.assertEqual(call[call.index('--branch') + 1], 'master')
        self.assertIn(URL, call)
        self.assertEqual(Path(call[-1]), checkout)
        for name in ('mimo-grok-adapter', 'check-mimo-grok'):
            self.assertEqual((self.home / '.local/bin' / name).read_bytes(), (ROOT / name).read_bytes())
        self.assertEqual(self.config.read_bytes(), b'[models]\ndefault="unchanged"\n')

    def test_repeat_stream_updates_clean_master_without_recloning(self):
        self.assertEqual(self.stream().returncode, 0)
        result = self.stream()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertEqual(sum(call[0] == 'clone' for call in calls), 1)
        self.assertEqual(calls[-1][2:], ['pull', '--ff-only', 'origin', 'master'])

    def test_explicit_checkout_path_with_spaces_is_supported(self):
        checkout = self.home / 'custom checkout'
        result = self.stream(MIMO_GROK_REPO_DIR=str(checkout))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((checkout / '.git').is_dir())
        self.assertEqual(Path(self.calls()[0][-1]), checkout)

    def test_existing_unrelated_directory_is_preserved(self):
        directory = self.home / 'unrelated'
        directory.mkdir()
        sentinel = directory / 'user-work.txt'
        sentinel.write_bytes(b'user work')
        result = self.stream(MIMO_GROK_REPO_DIR=str(directory))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(sentinel.read_bytes(), b'user work')
        self.assertFalse((self.home / '.local/bin/mimo-grok-adapter').exists())
        self.assertEqual(self.calls(), [])

    def test_dirty_checkout_is_preserved_without_updating_installed_files(self):
        self.assertEqual(self.stream().returncode, 0)
        installed = self.home / '.local/bin/mimo-grok-adapter'
        installed.write_bytes(b'previous installation')
        result = self.stream(FAKE_DIRTY=' M user-work.py\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(installed.read_bytes(), b'previous installation')
        self.assertFalse(any(call[2:3] == ['pull'] for call in self.calls() if call[0] == '-C'))

    def test_foreign_origin_is_refused(self):
        self.assertEqual(self.stream().returncode, 0)
        result = self.stream(FAKE_ORIGIN='https://example.test/other.git')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('https://example.test/other.git', result.stderr)
        self.assertFalse(any(call[2:3] == ['pull'] for call in self.calls() if call[0] == '-C'))

    def test_different_branch_is_preserved(self):
        self.assertEqual(self.stream().returncode, 0)
        result = self.stream(FAKE_BRANCH='feature')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(call[2:3] == ['pull'] for call in self.calls() if call[0] == '-C'))

    def test_repo_symlink_is_refused(self):
        directory = self.home / 'other-checkout'
        directory.mkdir()
        alias = self.home / 'checkout-link'
        alias.symlink_to(directory, target_is_directory=True)
        result = self.stream(MIMO_GROK_REPO_DIR=str(alias))
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(alias.is_symlink())
        self.assertEqual(self.calls(), [])

    def test_local_wrapper_uses_its_own_checkout_without_network(self):
        result = subprocess.run(['bash', str(self.script), '--no-start'], env=self.env,
                                cwd=self.home, text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertEqual((self.home / '.local/bin/mimo-grok-adapter').read_bytes(),
                         (ROOT / 'mimo-grok-adapter').read_bytes())


if __name__ == '__main__':
    unittest.main()
