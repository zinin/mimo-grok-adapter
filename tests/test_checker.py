import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_checker():
    loader = importlib.machinery.SourceFileLoader('checker_test', str(ROOT / 'check-mimo-grok'))
    spec = importlib.util.spec_from_loader('checker_test', loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class CheckerTests(unittest.TestCase):
    def setUp(self):
        self.checker = load_checker()
        self.fixture = Path('/tmp/example/probe.py')
        self.token = 'FIXTURE_MARKER'

    def analyse(self, check, events, stderr='', code=0, timed_out=False, control_ok=True):
        return self.checker.analyse(check, '\n'.join(json.dumps(event) for event in events),
                                    stderr, code, timed_out, self.fixture, self.token, control_ok)

    def test_original_models_remain_the_default_selection(self):
        self.assertEqual(self.checker.MODELS,
                         {'flash': 'mimo-v2.6-flash', 'pro': 'mimo-v2.6-pro'})
        self.assertEqual(self.checker.ADAPTED_MODELS['pro'], 'mimo-v2.6-pro-adapted')

    def test_missing_init_is_inconclusive(self):
        status, detail, stop = self.analyse('control', [], code=1)
        self.assertEqual(status, 'ERROR')
        self.assertIn('init', detail)
        self.assertTrue(stop)

    def test_extra_mcp_tools_are_reported_briefly(self):
        events = [{'type': 'system', 'subtype': 'init',
                   'tools': ['list_dir'] + [f'mcp__tool{i}' for i in range(100)]}]
        status, detail, stop = self.analyse('control', events)
        self.assertEqual(status, 'ERROR')
        self.assertTrue(stop)
        self.assertLess(len(detail), 200)
        self.assertNotIn('mcp__tool', detail)

    def test_text_only_success_does_not_count_as_native_tool_success(self):
        events = [{'type': 'system', 'subtype': 'init', 'tools': ['grep', 'read_file']},
                  {'type': 'result', 'result': self.token, 'is_error': False}]
        status, detail, stop = self.analyse('tools', events)
        self.assertEqual(status, 'ERROR')
        self.assertFalse(stop)

    def test_broken_tool_json_is_classified_as_bug(self):
        events = [{'type': 'system', 'subtype': 'init', 'tools': ['grep', 'read_file']},
                  {'type': 'result', 'is_error': True, 'errors': ['EOF while parsing arguments']}]
        status, detail, stop = self.analyse('tools', events, code=1)
        self.assertEqual(status, 'BUG')
        self.assertFalse(stop)

    def test_success_requires_matching_arguments_and_returned_markers(self):
        events = [{'type': 'system', 'subtype': 'init', 'tools': ['grep', 'read_file']}]
        for ident, name, arguments in (
            ('a', 'grep', {'path': str(self.fixture), 'pattern': self.token}),
            ('b', 'read_file', {'target_file': str(self.fixture), 'offset': 1, 'limit': 5}),
        ):
            events.append({'type': 'assistant', 'message': {'content': [
                {'type': 'tool_use', 'id': ident, 'name': name, 'input': arguments}]}})
            events.append({'type': 'user', 'message': {'content': [
                {'type': 'tool_result', 'tool_use_id': ident, 'content': self.token, 'is_error': False}]}})
        events.append({'type': 'result', 'is_error': False, 'result': 'OK'})
        self.assertEqual(self.analyse('tools', events)[0], 'OK')
        events[1]['message']['content'][0]['input']['path'] = '/tmp/other.py'
        self.assertEqual(self.analyse('tools', events)[0], 'ERROR')

    def test_mcp_is_disabled_only_in_private_copy_and_policies_are_kept(self):
        with tempfile.TemporaryDirectory(prefix='mimo-checker-test-') as temporary:
            home = Path(temporary)
            source, private = home / '.grok', home / 'private'
            source.mkdir()
            raw = ('disabled_mcp_servers = [\n "already-off", # retained\n]\n'
                   '[models]\ndefault="kept"\n[mcp_servers.native]\ncommand="fixture"\n'
                   '[permission]\ndefault="auto"\n')
            config = source / 'config.toml'
            config.write_text(raw)
            (source / 'rules').mkdir()
            (source / 'rules' / 'policy.md').write_text('Keep security rules.\n')
            (home / '.mcp.json').write_text(json.dumps({'mcpServers': {'external': {}}}))
            plugin = source / 'installed-plugins' / 'fixture'
            (plugin / '.claude-plugin').mkdir(parents=True)
            (plugin / '.claude-plugin/plugin.json').write_text(json.dumps({'mcpServers': ['servers.json']}))
            (plugin / 'servers.json').write_text(json.dumps({'mcpServers': {'plugin-server': {}}}))
            with patch.dict(os.environ, {'HOME': str(home)}):
                self.checker.isolated_home(source, private)
                names = self.checker.isolate_mcp(source, private)
            self.assertTrue({'native', 'external', 'plugin-server', 'already-off'} <= set(names))
            actual = tomllib.loads((private / 'config.toml').read_text())
            self.assertEqual(actual.pop('disabled_mcp_servers'), names)
            expected = tomllib.loads(raw)
            expected.pop('disabled_mcp_servers')
            self.assertEqual(actual, expected)
            self.assertEqual(config.read_text(), raw)
            self.assertEqual((private / 'rules/policy.md').read_text(), 'Keep security rules.\n')
            self.assertFalse((private / 'rules').is_symlink())


if __name__ == '__main__':
    unittest.main()
