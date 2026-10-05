import copy
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]


def load_adapter():
    loader = importlib.machinery.SourceFileLoader('adapter_test', str(ROOT / 'mimo-grok-adapter'))
    spec = importlib.util.spec_from_loader('adapter_test', loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = load_adapter()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def start_server(self, server):
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
            thread.join()

        self.addCleanup(stop)
        return 'http://127.0.0.1:' + str(server.server_port)

    def test_single_nullable_types_are_narrowed_and_required_is_preserved(self):
        schema = {'type': 'object', 'required': ['pattern'], 'properties': {
            'pattern': {'type': 'string'}, 'path': {'type': ['string', 'null']},
            'count': {'type': ['integer', 'null']},
        }}
        body = {'tools': [{'type': 'function', 'name': 'grep', 'parameters': schema}]}
        self.assertEqual(self.adapter.normalize_tools(body), 2)
        self.assertEqual(schema['properties']['path']['type'], 'string')
        self.assertEqual(schema['properties']['count']['type'], 'integer')
        self.assertEqual(schema['required'], ['pattern'])

    def test_input_and_irreducible_unions_are_unchanged(self):
        body = {'input': {'type': ['string', 'null']}, 'tools': [{'parameters': {
            'properties': {'x': {'type': ['string', 'integer', 'null']},
                           'y': {'type': ['null']}}}}]}
        before = copy.deepcopy(body)
        self.assertEqual(self.adapter.normalize_tools(body), 0)
        self.assertEqual(body, before)

    def test_namespaced_and_nested_schemas_are_normalized(self):
        body = {'tools': [{'type': 'namespace', 'tools': [{'parameters': {
            'properties': {'values': {'type': 'array', 'items': {'type': ['boolean', 'null']}}}}}]}]}
        self.assertEqual(self.adapter.normalize_tools(body), 1)

    def test_requests_and_responses_pass_through_with_only_tool_schema_changes(self):
        received = []
        wire = b'event: response.completed\ndata: {"type":"response.completed"}\n\n'

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                received.append((self.path, self.headers.get('Authorization'),
                                 json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(wire)))
                self.end_headers()
                self.wfile.write(wire)

        upstream = self.start_server(ThreadingHTTPServer(('127.0.0.1', 0), Upstream))
        proxy = self.start_server(self.adapter.make_server('127.0.0.1', 0, upstream + '/v1', 2))
        body = {'model': 'mimo-v2.6-pro', 'input': 'fixture', 'reasoning': {'effort': 'high'},
                'stream': True, 'tools': [{'parameters': {'properties': {
                    'path': {'type': ['string', 'null']}}}}]}
        request = urllib.request.Request(proxy + '/v1/responses', data=json.dumps(body).encode(),
                                         headers={'Authorization': 'Bearer fake-test-key',
                                                  'Content-Type': 'application/json'})
        with self.opener.open(request, timeout=3) as response:
            self.assertEqual(response.read(), wire)
        self.assertEqual(received[0][0], '/v1/responses')
        self.assertEqual(received[0][1], 'Bearer fake-test-key')
        expected = copy.deepcopy(body)
        expected['tools'][0]['parameters']['properties']['path']['type'] = 'string'
        self.assertEqual(received[0][2], expected)

    def test_stream_is_delivered_before_upstream_finishes(self):
        release = threading.Event()
        first, last = b'data: first\n\n', b'data: last\n\n'

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(first) + len(last)))
                self.end_headers()
                self.wfile.write(first)
                self.wfile.flush()
                release.wait(3)
                self.wfile.write(last)
                self.wfile.flush()

        upstream = self.start_server(ThreadingHTTPServer(('127.0.0.1', 0), Upstream))
        proxy = self.start_server(self.adapter.make_server('127.0.0.1', 0, upstream + '/v1', 2))
        self.addCleanup(release.set)
        request = urllib.request.Request(proxy + '/v1/responses',
                                         data=b'{"model":"mimo-v2.6-flash","stream":true}',
                                         headers={'Authorization': 'Bearer fake-test-key'})
        try:
            with self.opener.open(request, timeout=1) as response:
                self.assertEqual(response.read(len(first)), first)
                release.set()
                self.assertEqual(response.read(), last)
        finally:
            release.set()

    def test_http_errors_keep_their_status_and_body(self):
        wire = b'{"error":{"message":"fixture failure"}}'

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(429)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(wire)))
                self.end_headers()
                self.wfile.write(wire)

        upstream = self.start_server(ThreadingHTTPServer(('127.0.0.1', 0), Upstream))
        proxy = self.start_server(self.adapter.make_server('127.0.0.1', 0, upstream + '/v1', 2))
        request = urllib.request.Request(proxy + '/v1/responses', data=b'{"model":"mimo-v2.6-pro"}',
                                         headers={'Authorization': 'Bearer fake-test-key'})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.opener.open(request, timeout=3)
        with caught.exception as response:
            self.assertEqual(response.code, 429)
            self.assertEqual(response.read(), wire)

    def test_health_and_model_restrictions(self):
        proxy = self.start_server(self.adapter.make_server('127.0.0.1', 0, 'http://127.0.0.1:1/v1', 1))
        with self.opener.open(proxy + '/health', timeout=2) as response:
            self.assertEqual(json.load(response), {'status': 'ok', 'pid': os.getpid()})
        request = urllib.request.Request(proxy + '/v1/responses', data=b'{"model":"other"}',
                                         headers={'Authorization': 'Bearer fake-test-key'})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.opener.open(request, timeout=2)
        with caught.exception as response:
            self.assertEqual(response.code, 400)

    def test_public_binding_and_credentialed_upstream_are_refused(self):
        with self.assertRaises(ValueError):
            self.adapter.make_server('0.0.0.0', 0)
        with self.assertRaises(ValueError):
            self.adapter.make_server('127.0.0.1', 0, 'https://user:password@example.test/v1')
        with self.assertRaises(ValueError):
            self.adapter.make_server('127.0.0.1', 0, 'http://example.test/v1')


if __name__ == '__main__':
    unittest.main()
