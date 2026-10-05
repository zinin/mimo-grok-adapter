import copy
import importlib.machinery
import importlib.util
import inspect
import json
from pathlib import Path
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    loader = importlib.machinery.SourceFileLoader('route_' + name.replace('-', '_'), str(ROOT / name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.adapter = load('mimo-grok-adapter')
        self.assertIn('api_upstream', inspect.signature(self.adapter.make_server).parameters,
                      'paid API route configuration is missing')
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def start_server(self, server):
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def cleanup():
            server.shutdown()
            server.server_close()
            thread.join()

        self.addCleanup(cleanup)
        return f'http://127.0.0.1:{server.server_port}'

    def upstream(self, received, wire, status=200):
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                received.append((self.path, self.headers.get('Authorization'), json.loads(body)))
                self.send_response(status)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(wire)))
                self.end_headers()
                self.wfile.write(wire)

        return self.start_server(ThreadingHTTPServer(('127.0.0.1', 0), Upstream)) + '/v1'

    def test_both_routes_keep_their_own_upstream_and_authorization(self):
        plan_calls, api_calls = [], []
        plan_wire, api_wire = b'data: plan\n\n', b'data: api\n\n'
        plan = self.upstream(plan_calls, plan_wire)
        api = self.upstream(api_calls, api_wire)
        proxy = self.start_server(self.adapter.make_server('127.0.0.1', 0, plan, 2, api_upstream=api))
        body = {'model': 'mimo-v2.6-pro', 'input': 'fixture', 'stream': True,
                'reasoning': {'effort': 'high'}, 'tools': [{'parameters': {'type': 'object',
                'required': ['pattern'], 'properties': {'path': {'type': ['string', 'null']}}}}]}
        expected = copy.deepcopy(body)
        expected['tools'][0]['parameters']['properties']['path']['type'] = 'string'
        for path, credential, wire in (('/api/v1/responses', 'Bearer fake-api', api_wire),
                                       ('/v1/responses', 'Bearer fake-plan', plan_wire),
                                       ('/api/v1/responses', 'Bearer fake-api-2', api_wire)):
            request = urllib.request.Request(proxy + path, data=json.dumps(body).encode(),
                                             headers={'Authorization': credential})
            with self.opener.open(request, timeout=3) as response:
                self.assertEqual(response.read(), wire)
        self.assertEqual(plan_calls, [('/v1/responses', 'Bearer fake-plan', expected)])
        self.assertEqual(api_calls, [('/v1/responses', 'Bearer fake-api', expected),
                                     ('/v1/responses', 'Bearer fake-api-2', expected)])

    def test_ultraspeed_api_forwards_model_and_normalizes_tools(self):
        plan_calls, api_calls = [], []
        wire = b'event: response.completed\ndata: {"type":"response.completed"}\n\n'
        plan = self.upstream(plan_calls, b'plan fixture')
        api = self.upstream(api_calls, wire)
        proxy = self.start_server(self.adapter.make_server('127.0.0.1', 0, plan, 2, api_upstream=api))
        body = {'model': 'mimo-v2.6-pro-ultraspeed', 'input': 'fixture', 'stream': True,
                'reasoning': {'effort': 'max'}, 'tools': [{'type': 'function', 'name': 'grep',
                'parameters': {'type': 'object', 'required': ['pattern'], 'properties': {
                    'pattern': {'type': 'string'}, 'path': {'type': ['string', 'null']}}}}]}
        request = urllib.request.Request(proxy + '/api/v1/responses', data=json.dumps(body).encode(),
                                         headers={'Authorization': 'Bearer fake-ultraspeed-api'})
        try:
            response = self.opener.open(request, timeout=3)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            self.assertEqual(response.status, 200, 'Ultraspeed must be accepted on the API route')
            self.assertEqual(response.read(), wire)
        expected = copy.deepcopy(body)
        expected['tools'][0]['parameters']['properties']['path']['type'] = 'string'
        self.assertEqual(api_calls, [('/v1/responses', 'Bearer fake-ultraspeed-api', expected)])
        self.assertEqual(plan_calls, [])

    def test_ultraspeed_is_rejected_on_token_plan_route(self):
        calls = []
        upstream = self.upstream(calls, b'fixture')
        proxy = self.start_server(self.adapter.make_server('127.0.0.1', 0, upstream, 2,
                                                          api_upstream=upstream))
        request = urllib.request.Request(proxy + '/v1/responses',
                                         data=b'{"model":"mimo-v2.6-pro-ultraspeed"}',
                                         headers={'Authorization': 'Bearer fake-plan'})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.opener.open(request, timeout=3)
        with caught.exception as response:
            self.assertEqual(response.code, 400)
        self.assertEqual(calls, [])

    def test_unknown_api_model_is_rejected_before_upstream(self):
        calls = []
        upstream = self.upstream(calls, b'fixture')
        proxy = self.start_server(self.adapter.make_server('127.0.0.1', 0, upstream, 2,
                                                          api_upstream=upstream))
        request = urllib.request.Request(proxy + '/api/v1/responses', data=b'{"model":"other"}',
                                         headers={'Authorization': 'Bearer fake-api'})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.opener.open(request, timeout=3)
        with caught.exception as response:
            self.assertEqual(response.code, 400)
        self.assertEqual(calls, [])

    def test_api_errors_preserve_status_and_body(self):
        calls = []
        wire = b'{"error":{"message":"fixture rate limit"}}'
        api = self.upstream(calls, wire, status=429)
        proxy = self.start_server(self.adapter.make_server('127.0.0.1', 0, 'http://127.0.0.1:1/v1', 2,
                                                          api_upstream=api))
        request = urllib.request.Request(proxy + '/api/v1/responses', data=b'{"model":"mimo-v2.6-flash"}',
                                         headers={'Authorization': 'Bearer fake-api'})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.opener.open(request, timeout=3)
        with caught.exception as response:
            self.assertEqual(response.code, 429)
            self.assertEqual(response.read(), wire)

    def test_api_stream_is_forwarded_before_generation_finishes(self):
        release = threading.Event()
        first, last = b'data: first\n\n', b'data: last\n\n'

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(200)
                self.send_header('Content-Length', str(len(first) + len(last)))
                self.end_headers()
                self.wfile.write(first)
                self.wfile.flush()
                release.wait(3)
                self.wfile.write(last)
                self.wfile.flush()

        api = self.start_server(ThreadingHTTPServer(('127.0.0.1', 0), Upstream)) + '/v1'
        proxy = self.start_server(self.adapter.make_server('127.0.0.1', 0, 'http://127.0.0.1:1/v1', 2,
                                                          api_upstream=api))
        self.addCleanup(release.set)
        request = urllib.request.Request(proxy + '/api/v1/responses',
                                         data=b'{"model":"mimo-v2.6-pro","stream":true}',
                                         headers={'Authorization': 'Bearer fake-api'})
        try:
            with self.opener.open(request, timeout=1) as response:
                self.assertEqual(response.read(len(first)), first)
                release.set()
                self.assertEqual(response.read(), last)
        finally:
            release.set()

    def test_unknown_and_url_injected_paths_are_rejected(self):
        calls = []
        upstream = self.upstream(calls, b'fixture')
        proxy = self.start_server(self.adapter.make_server('127.0.0.1', 0, upstream, 2,
                                                          api_upstream=upstream))
        for path in ('/api/v1/responses?upstream=https://example.test', '/api/v1/chat/completions',
                     '/v1/responses/https://example.test'):
            request = urllib.request.Request(proxy + path, data=b'{"model":"mimo-v2.6-pro"}',
                                             headers={'Authorization': 'Bearer fake'})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.opener.open(request, timeout=2)
            with caught.exception as response:
                self.assertEqual(response.code, 404)
        self.assertEqual(calls, [])

    def test_api_upstream_validation_matches_token_plan(self):
        for upstream in ('https://user:password@example.test/v1', 'http://example.test/v1',
                         'https://example.test/v1?key=fake', 'https://example.test/v1#fragment'):
            with self.assertRaises(ValueError):
                self.adapter.make_server('127.0.0.1', 0, api_upstream=upstream)


class CheckerSelectionTests(unittest.TestCase):
    def setUp(self):
        self.checker = load('check-mimo-grok')
        self.assertTrue(hasattr(self.checker, 'select_models'), 'provider selection helper is missing')

    def test_default_selection_stays_direct_token_plan(self):
        self.assertEqual(self.checker.select_models(), ['mimo-v2.6-flash', 'mimo-v2.6-pro'])

    def test_api_flag_selects_direct_api_models(self):
        self.assertEqual(self.checker.select_models(api=True), ['mimo-v2.6-flash-api', 'mimo-v2.6-pro-api'])

    def test_api_and_adapted_flags_select_api_adapter_models(self):
        self.assertEqual(self.checker.select_models(api=True, adapted=True),
                         ['mimo-v2.6-flash-api-adapted', 'mimo-v2.6-pro-api-adapted'])

    def test_api_flag_translates_token_plan_adapted_full_slugs(self):
        self.assertEqual(self.checker.select_models(api=True, adapted=True,
                                                   names=['mimo-v2.6-flash-adapted', 'mimo-v2.6-pro-adapted']),
                         ['mimo-v2.6-flash-api-adapted', 'mimo-v2.6-pro-api-adapted'])

    def test_existing_adapted_selection_and_explicit_api_slugs_remain_consistent(self):
        self.assertEqual(self.checker.select_models(adapted=True, names=['pro']), ['mimo-v2.6-pro-adapted'])
        self.assertEqual(self.checker.select_models(adapted=True, names=['mimo-v2.6-pro-api', 'pro', 'pro']),
                         ['mimo-v2.6-pro-api-adapted', 'mimo-v2.6-pro-adapted'])


if __name__ == '__main__':
    unittest.main()
