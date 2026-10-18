"""Offline unit + real loopback HTTP tests. No provider credentials or external calls."""
import calendar
import copy
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
import http.client
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bridge as b


def metadata(cost=None, npm='@ai-sdk/openai-compatible'):
    return {'opencode': {'npm': npm, 'models': {'demo-free': {
        'id': 'demo-free', 'name': 'Demo Free', 'cost': cost if cost is not None else {'input': 0, 'output': 0, 'cache_read': 0},
        'limit': {'context': 100000, 'output': 16000}, 'reasoning': True,
        'tool_call': True, 'modalities': {'input': ['text', 'image']},
    }}}}


def listing(lane='zen', model='demo-free'):
    return {lane: {'data': [{'id': model}]}}


def catalog_rows():
    now = time.time()
    rows = []
    for endpoint, api in b.APIS.items():
        rows.append({'lane': 'zen', 'id': 'demo-' + endpoint.replace('/', '-'), 'name': 'Demo',
                     'endpoint': endpoint, 'api': api, 'input': ['text'], 'reasoning': False,
                     'contextWindow': 32768, 'maxTokens': 8192,
                     'cost': {'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0}})
    return {'schema': 1, 'checked_at': now, 'expires_at': now + 900,
            'models': rows, 'excluded': [], 'notes': []}


def frame(value, event=None):
    return (b'event: ' + event.encode() + b'\n' if event else b'') + b'data: ' + b.encode(value) + b'\n\n'


def real_tools(endpoint='chat/completions'):
    return [({'type': 'function', 'function': {'name': n, 'description': 'real', 'parameters': {'type': 'object'}}}
             if endpoint == 'chat/completions' else {'type': 'function', 'name': n, 'parameters': {'type': 'object'}}) for n in b.CORE]


class CoreTests(unittest.TestCase):
    def test_metadata_placeholders_and_retired_editions_are_not_free(self):
        for update in ({'name': 'Unpriced preview'}, {'status': 'deprecated'}):
            meta = metadata()
            meta['opencode']['models']['demo-free'].update(update)
            self.assertFalse(b.compile_catalog(listing(), meta, {})['models'])

    def test_null_stream_fields_preserve_stub_guard(self):
        guard = b.StreamGuard('chat/completions', {'bash'})
        for event in ({'choices': None}, {'choices': [{'delta': None, 'message': None}]},
                      {'choices': [{'delta': {'tool_calls': None}}]}):
            guard.inspect(event)
        with self.assertRaisesRegex(b.BridgeError, 'stub'):
            guard.inspect({'choices': [{'delta': {'tool_calls': [{'function': {'name': 'bash'}}]}}]})
        b.StreamGuard('responses', {'bash'}).inspect({'response': {'output': None}})

    def test_session_stable_and_canonical(self):
        token = b.session_token('conversation-a')
        self.assertRegex(token, b.SESSION_RE)
        self.assertEqual(token, b.session_token('conversation-a'))
        self.assertNotEqual(token, b.session_token('conversation-b'))
        self.assertEqual(token, b.session_token(token))
        # The timestamp's low 12 bits are OpenCode's small same-ms counter.
        self.assertLessEqual((~int(token[4:16], 16)) & 0xFFF, 0x100)

    def test_zen_can_start_without_a_go_key(self):
        with tempfile.TemporaryDirectory() as root, patch.object(b, 'STATE', Path(root)), \
             patch.dict(os.environ, {'OPENCODE_API_KEY': ''}):
            self.assertEqual(b.key_from_user(required=False), '')
            self.assertEqual(b.error_info(403, b'{"error":{"message":"denied"}}', '')[0].args[0].split(' [')[0], 'denied')

    def test_zero_cost_only_explicit_finite_zeros(self):
        self.assertTrue(b.zero_cost({'input': 0, 'output': 0, 'cache_read': 0}))
        for value in [None, {}, {'input': 0}, {'input': False, 'output': 0},
                      {'input': '0', 'output': 0}, {'input': 0, 'output': float('nan')},
                      {'input': 0, 'output': 0, 'cache_write': 0.01},
                      {'input': 0, 'output': 0, 'context_over_200k': {'input': 1}},
                      {'input': -1, 'output': 0}]:
            with self.subTest(value=value): self.assertFalse(b.zero_cost(value))

    def test_catalog_allows_zero_cost_metadata(self):
        c = b.compile_catalog(listing(), metadata(), {})
        self.assertEqual(c['models'][0]['api'], 'openai-completions')
        self.assertEqual(c['models'][0]['input'], ['text', 'image'])
        self.assertEqual(c['models'][0]['maxTokens'], 16000)

    def test_paid_free_named_model_rejected(self):
        c = b.compile_catalog(listing(), metadata({'input': 0.2, 'output': 1}), {})
        self.assertFalse(c['models'])
        self.assertIn('not verified', c['excluded'][0]['reason'])

    def test_unknown_free_suffix_rejected(self):
        self.assertFalse(b.compile_catalog(listing(), {}, {})['models'])

    def test_official_docs_can_establish_free_route(self):
        docs = {'zen': {'demo-free': {'name': 'Demo', 'free': True,
                'endpoint_url': 'https://opencode.ai/zen/v1/responses'}}}
        c = b.compile_catalog(listing(), {}, docs)
        self.assertEqual(c['models'][0]['api'], 'openai-responses')
        self.assertEqual(c['models'][0]['limits_source'], 'conservative local defaults, not measured limits')

    def test_v2_documented_route_and_systemone_exclusion(self):
        docs = {'zen': {'demo-free': {'name': 'Demo', 'free': True,
                'endpoint_url': 'https://opencode.ai/inference/v1/chat/completions'}}}
        self.assertEqual(b.compile_catalog(listing(), {}, docs)['models'][0]['api'], 'openai-completions')
        docs['zen']['demo-free']['endpoint_url'] = 'https://opencode.ai/inference/v1/systemone'
        self.assertFalse(b.compile_catalog(listing(), metadata(), docs)['models'])

    def test_disagreement_is_fail_closed(self):
        docs = {'zen': {'demo-free': {'free': True}}}
        c = b.compile_catalog(listing(), metadata({'input': 1, 'output': 0}), docs)
        self.assertEqual(c['excluded'][0]['reason'], 'pricing conflict')
        docs['zen']['demo-free']['free'] = False
        self.assertFalse(b.compile_catalog(listing(), metadata(), docs)['models'])

    def test_lanes_are_not_substituted(self):
        self.assertFalse(b.compile_catalog(listing('go'), metadata(), {})['models'])

    def test_live_listing_removes_retired_model(self):
        self.assertFalse(b.compile_catalog({'zen': {'data': []}}, metadata(), {})['models'])

    def test_unknown_protocol_is_excluded(self):
        c = b.compile_catalog(listing(), metadata(npm='not-supported'), {})
        self.assertIn('protocol', c['excluded'][0]['reason'])

    def test_unsupported_documented_endpoint_never_falls_back_to_the_npm_guess(self):
        # A System One decision API must not be relabelled as a chat model by the
        # provider-wide @ai-sdk/openai-compatible default.
        docs = {'zen': {'demo-free': {'free': True, 'endpoint_url': 'https://opencode.ai/zen/v1/systemone'}}}
        c = b.compile_catalog(listing(), metadata(), docs)
        self.assertEqual(c['models'], [])
        self.assertIn('protocol', c['excluded'][0]['reason'])

    def test_docs_parser_uses_exact_price_and_endpoint_tables(self):
        html = '''<table><tr><th>Model</th><th>Model ID</th><th>Endpoint</th><th>AI SDK Package</th></tr>
        <tr><td>Demo &amp; Free</td><td><code>demo-free</code></td><td>https://opencode.ai/zen/v1/responses</td><td>sdk</td></tr></table>
        <table><tr><th>Model</th><th>Input</th><th>Output</th><th>Cached Read</th><th>Cached Write</th></tr>
        <tr><td>Demo &amp; Free</td><td>Free</td><td>Free</td><td>Free</td><td>-</td></tr></table>'''
        self.assertTrue(b.docs_models(html)['demo-free']['free'])
        self.assertFalse(b.docs_models(html.replace('<td>Free</td><td>Free</td>', '<td>$0.10</td><td>Free</td>'))['demo-free']['free'])

    def test_paid_sibling_sharing_a_display_name_is_not_priced_as_free(self):
        html = '''<table><tr><th>Model</th><th>Model ID</th><th>Endpoint</th></tr>
        <tr><td>Demo</td><td>demo-paid</td><td>https://opencode.ai/zen/v1/responses</td></tr>
        <tr><td>Demo</td><td>demo-free</td><td>https://opencode.ai/zen/v1/responses</td></tr></table>
        <table><tr><th>Model</th><th>Input</th><th>Output</th></tr>
        <tr><td>Demo</td><td>$1.50</td><td>$3.00</td></tr>
        <tr><td>Demo</td><td>Free</td><td>Free</td></tr></table>'''
        parsed = b.docs_models(html)
        self.assertFalse(parsed['demo-paid']['free'])
        self.assertFalse(parsed['demo-free']['free'])

    def test_pricing_table_with_ids_is_joined_by_id(self):
        html = '''<table><tr><th>Model</th><th>Model ID</th><th>Endpoint</th></tr>
        <tr><td>Demo</td><td>demo-paid</td><td>https://opencode.ai/zen/v1/responses</td></tr>
        <tr><td>Demo</td><td>demo-free</td><td>https://opencode.ai/zen/v1/responses</td></tr></table>
        <table><tr><th>Model</th><th>Model ID</th><th>Input</th><th>Output</th></tr>
        <tr><td>Demo</td><td>demo-paid</td><td>$1.50</td><td>$3.00</td></tr>
        <tr><td>Demo</td><td>demo-free</td><td>Free</td><td>Free</td></tr></table>'''
        parsed = b.docs_models(html)
        self.assertFalse(parsed['demo-paid']['free'])
        self.assertTrue(parsed['demo-free']['free'])

    def test_missing_doc_row_allows_active_named_free_metadata(self):
        docs = {'zen': {'other-model': {'name': 'Other', 'free': True,
                                        'endpoint_url': 'https://opencode.ai/zen/v1/chat/completions'}}}
        # models.dev also uses cost 0 as a placeholder for unpriced models.
        c = b.compile_catalog(listing(), metadata(), docs)
        self.assertEqual([m['id'] for m in c['models']], ['demo-free'])

    def test_unreadable_pricing_table_still_accepts_explicit_metadata_zero(self):
        c = b.compile_catalog(listing(), metadata(), {'zen': {}})
        self.assertEqual([m['id'] for m in c['models']], ['demo-free'])

    def test_finish_reason_terminates_a_chat_completions_stream(self):
        self.assertTrue(b.is_terminal({'choices': [{'delta': {}, 'finish_reason': 'stop'}]}, 'chat/completions'))
        self.assertFalse(b.is_terminal({'choices': [{'delta': {'content': 'x'}, 'finish_reason': None}]}, 'chat/completions'))
        # Not an OpenAI-shaped stream: the field must not end a Responses/Messages stream.
        self.assertFalse(b.is_terminal({'choices': [{'finish_reason': 'stop'}]}, 'responses'))
        self.assertTrue(b.is_terminal({'type': 'message_stop'}, 'messages'))

    def test_malformed_anthropic_version_is_refused(self):
        with self.assertRaisesRegex(b.BridgeError, 'anthropic-version'):
            b.prepare({'_omp_free_session': 'x', 'stream': True}, 'messages',
                      {'anthropic-version': '2023-06-01\r\n x-injected: 1'}, False, '1.18.31')

    def test_synchronous_inference_rejected(self):
        with self.assertRaisesRegex(b.BridgeError, 'stream:true'):
            b.prepare({'_omp_free_session': 'x'}, 'responses', {}, True, '1.18.31')

    def test_missing_session_rejected(self):
        with self.assertRaisesRegex(b.BridgeError, 'session'):
            b.prepare({'stream': True}, 'responses', {}, True, '1.18.31')

    def test_extension_helper_header_fallback(self):
        body, headers, missing = b.prepare({'stream': True, '_omp_free_bridge': True}, 'responses', {'x-omp-free-session': 'helper'}, True, '1.18.31')
        self.assertEqual(headers['x-opencode-session'], b.session_token('helper'))
        self.assertNotIn('_omp_free_bridge', body)

    def test_compat_disabled_does_not_impersonate_or_pad(self):
        body, headers, missing = b.prepare({'stream': True, '_omp_free_session': 's'}, 'responses', {}, False, '1.18.31')
        self.assertTrue(headers['User-Agent'].startswith('omp-free-bridge/'))
        self.assertNotIn('tools', body)
        self.assertFalse(missing)

    def test_toolless_all_three_protocols(self):
        for endpoint in ['chat/completions', 'responses', 'messages']:
            with self.subTest(endpoint=endpoint):
                body, h, missing = b.prepare({'stream': True, '_omp_free_session': 's'}, endpoint, {}, True, '1.18.31')
                declared = {(t.get('function') or {}).get('name') if endpoint == 'chat/completions' else t.get('name')
                            for t in body['tools']}
                self.assertEqual(declared, set(b.CORE))
                self.assertEqual(missing, set(b.CORE))
                self.assertNotIn('tool_choice', body)
                self.assertEqual(h['User-Agent'], 'opencode/1.18.31')
                self.assertEqual(h['x-session-affinity'], h['x-opencode-session'])
                self.assertEqual(h['x-opencode-session-id'], h['x-opencode-session'])
                self.assertNotIn('x-opencode-request', h)
                self.assertTrue(all((t.get('function') or t).get('parameters', (t.get('function') or t).get('input_schema'))['properties'] for t in body['tools']))
                self.assertNotIn('_omp_free_session', body)

    def test_go_keeps_its_own_five_tool_and_auth_contract(self):
        body, h, missing = b.prepare({'stream': True, '_omp_free_session': 's'},
                                      'chat/completions', {}, True, '1.18.31', 'go')
        self.assertEqual(missing, set(b.GO_CORE))
        self.assertEqual(body['tool_choice'], 'none')
        self.assertNotIn('x-session-affinity', h)

    def test_zen_drops_unsupported_tool_choice(self):
        for choice in ('none', 'required', {'type': 'function', 'function': {'name': 'bash'}}):
            with self.subTest(choice=choice):
                body, _, _ = b.prepare({'stream': True, '_omp_free_session': 's',
                                        'tools': real_tools(), 'tool_choice': choice},
                                       'chat/completions', {}, True, '1.19.0', 'zen')
                self.assertNotIn('tool_choice', body)

    def test_real_tools_and_hashline_arguments_preserved(self):
        original = {'stream': True, '_omp_free_session': 's', 'tools': real_tools(), 'tool_choice': 'auto',
                    'messages': [{'role': 'assistant', 'reasoning_content': 'preserved',
                        'tool_calls': [{'id': 'call_1', 'type': 'function', 'function': {'name': 'edit', 'arguments': '{"anchor":"12:AB","text":"й"}'}}]}]}
        saved = copy.deepcopy(original)
        body, _, missing = b.prepare(original, 'chat/completions', {}, True, '1.18.31')
        self.assertEqual(body['tools'], saved['tools'])
        self.assertEqual(body['messages'], saved['messages'])
        self.assertEqual(original, saved)
        self.assertFalse(missing)
        self.assertEqual(body['tool_choice'], 'auto')

    def test_partial_tools_pad_only_missing(self):
        original = real_tools()[0:1]
        body, _, missing = b.prepare({'stream': True, 'tools': original, '_omp_free_session': 's'}, 'chat/completions', {}, True, '1.18.31')
        self.assertEqual(len(body['tools']), 6)
        self.assertEqual(body['tools'][0], original[0])
        self.assertNotIn('bash', missing)
        self.assertNotIn('tool_choice', body)

    def test_non_function_native_tool_not_disabled(self):
        body, _, _ = b.prepare({'stream': True, 'tools': [{'type': 'web_search'}], '_omp_free_session': 's'}, 'responses', {}, True, '1.18.31')
        self.assertNotIn('tool_choice', body)
        self.assertEqual(body['tools'][0], {'type': 'web_search'})

    def test_stub_guard_fragmented_chat_name(self):
        guard = b.StreamGuard('chat/completions', {'bash'})
        guard.inspect({'choices': [{'delta': {'tool_calls': [{'index': 0, 'function': {'name': 'ba'}}]}}]})
        with self.assertRaisesRegex(b.BridgeError, 'stub'):
            guard.inspect({'choices': [{'delta': {'tool_calls': [{'index': 0, 'function': {'name': 'sh'}}]}}]})

    def test_stub_guard_responses_and_messages(self):
        for endpoint, event in [('responses', {'type': 'response.output_item.added', 'item': {'type': 'function_call', 'name': 'read'}}),
                                ('messages', {'type': 'content_block_start', 'content_block': {'type': 'tool_use', 'name': 'read'}})]:
            with self.subTest(endpoint=endpoint), self.assertRaises(b.BridgeError):
                b.StreamGuard(endpoint, {'read'}).inspect(event)

    def test_guard_does_not_block_real_tool(self):
        b.StreamGuard('responses', {'bash'}).inspect({'item': {'name': 'read', 'type': 'function_call'}})
        b.StreamGuard('responses', {'bash'}).inspect({'type': 'response.created', 'item': None})

    def test_sse_crlf_and_split_data_lines(self):
        wire = b': ping\r\n\r\ndata: {"text":\r\ndata: "hello"}\r\n\r\n'
        frames = list(b.sse_events(io.BytesIO(wire)))
        self.assertEqual(len(frames), 2)
        self.assertEqual(b.load_json(frames[1][1]), {'text': 'hello'})

    def test_sse_truncated_frame(self):
        with self.assertRaisesRegex(b.BridgeError, 'middle'):
            list(b.sse_events(io.BytesIO(b'data: {}\n')))

    def test_sse_frame_limit(self):
        with patch.object(b, 'MAX_FRAME', 8), self.assertRaises(b.BridgeError):
            list(b.sse_events(io.BytesIO(b'data: {"long":1}\n\n')))

    def test_json_rejects_nan(self):
        with self.assertRaises(ValueError): b.load_json('{"x":NaN}')

    def test_error_redaction_and_429_not_success(self):
        exc, wait = b.error_info(429, b'{"error":{"message":"secret-key quota","type":"FreeUsageLimitError"}}', 'secret-key')
        self.assertNotIn('secret-key', str(exc))
        self.assertIn('NOT a successful', str(exc))
        self.assertGreaterEqual(wait, 60)

    def test_quota_reset_names_utc_time_and_remaining_wait(self):
        # The live 429 of 2026-10-18 18:40:16 UTC carried Retry-After: 19184.
        now = float(calendar.timegm((2026, 10, 18, 18, 40, 16, 0, 0, 0)))
        text = b.quota_reset(now + 19184, now)
        self.assertIn('2026-10-19 00:00 UTC', text)
        self.assertIn('in 5h 19m', text)

    def test_private_file_permissions_and_symlink(self):
        with tempfile.TemporaryDirectory() as root:
            p = Path(root) / 'key'
            b.save_private(p, b'example')
            self.assertEqual(b.read_private(p), 'example')
            if os.name == 'posix':
                p.chmod(0o644)
                with self.assertRaises(b.BridgeError): b.read_private(p)
                p.chmod(0o600)
                link = Path(root) / 'link'
                link.symlink_to(p)
                with self.assertRaises(b.BridgeError): b.read_private(link)

    def test_public_fetch_rejects_redirect_target_hosts_without_network(self):
        for url in ['http://opencode.ai/zen', 'https://example.com/', 'https://opencode.ai:443/', 'https://localhost/']:
            with self.subTest(url=url), self.assertRaises(b.BridgeError): b.public_get(url)

    def test_catalog_expired_failure_never_uses_stale_models(self):
        with tempfile.TemporaryDirectory() as root:
            calls = []
            def failing(url):
                calls.append(url)
                raise OSError('offline')
            c = b.Catalog(Path(root), failing)
            c.snapshot = catalog_rows()
            c.snapshot['checked_at'] = 0
            c.snapshot['expires_at'] = 900
            with self.assertRaisesRegex(b.BridgeError, 'No stale'): c.get()
            self.assertEqual(len(calls), 2)

    def test_catalog_fresh_cache_needs_no_network(self):
        with tempfile.TemporaryDirectory() as root:
            c = b.Catalog(Path(root), lambda url: self.fail('unexpected external fetch'))
            c.snapshot = catalog_rows()
            self.assertEqual(c.get()['models'], c.snapshot['models'])

    def test_catalog_full_refresh_from_mock_sources(self):
        with tempfile.TemporaryDirectory() as root:
            def fake(url):
                if url.endswith('/zen/v1/models'): return b.encode(listing()['zen'])
                if url.endswith('/zen/go/v1/models'): return b.encode({'data': []})
                if url.endswith('/api.json'): return b.encode(metadata())
                return b'<html></html>'
            c = b.Catalog(Path(root), fake)
            self.assertEqual(c.get(True)['models'][0]['id'], 'demo-free')
            self.assertTrue((Path(root) / 'catalog.json').exists())


class FakeResponse(io.BytesIO):
    def __init__(self, raw, status=200, headers=None):
        super().__init__(raw)
        self.status = status
        self.headers = headers or {'Content-Type': 'text/event-stream'}
    def getheader(self, key, default=None): return self.headers.get(key, default)


class StaticCatalog:
    def __init__(self): self.data = catalog_rows()
    def get(self): return copy.deepcopy(self.data)


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.closed = threading.Event()
        self.release_stream = threading.Event()
        self.response = FakeResponse(frame({'choices': [{'delta': {'content': 'BRIDGE_OK'}}]}) + b'data: [DONE]\n\n')
        outer = self
        class Connection:
            sock = None
            def request(self, method, path, body, headers):
                outer.calls.append({'method': method, 'path': path, 'body': b.load_json(body), 'headers': headers})
            def getresponse(self): return outer.response
            def close(self):
                outer.closed.set()
                outer.release_stream.set()
        self.server = b.BridgeServer(('127.0.0.1', 0), StaticCatalog(), 'test-upstream-key-ONLY-FIXTURE', 'L' * 40, True, connector=Connection)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.release_stream.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, payload=None, path='/zen/v1/chat/completions', headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        h = {'Authorization': 'Bearer ' + 'L' * 40, 'Content-Type': 'application/json'}
        h.update(headers or {})
        data = payload if payload is not None else {'model': 'demo-chat-completions', 'stream': True, '_omp_free_session': 'http-test'}
        conn.request('POST', path, b.encode(data), h)
        r = conn.getresponse()
        result = (r.status, r.read())
        conn.close()
        return result

    def test_pre_stream_error_uses_the_endpoint_error_shape(self):
        status, body = self.request({'model': 'demo-messages', 'stream': False, '_omp_free_session': 'x'},
                                    path='/zen/v1/messages')
        self.assertEqual(status, 400)
        value = b.load_json(body)
        # Anthropic clients parse {"type": "error", "error": {...}}, not the OpenAI envelope.
        self.assertEqual(value['type'], 'error')
        self.assertEqual(value['error']['code'], 'StreamingRequired')

    def test_chat_completions_stream_without_done_is_not_truncated(self):
        self.response = FakeResponse(frame({'choices': [{'delta': {'content': 'BRIDGE_OK'},
                                                         'finish_reason': 'stop'}]}))
        status, body = self.request()
        self.assertEqual(status, 200)
        self.assertIn(b'BRIDGE_OK', body)
        self.assertNotIn(b'TruncatedStream', body)

    def test_actual_loopback_http_stream_success(self):
        status, raw = self.request()
        self.assertEqual(status, 200)
        self.assertIn(b'BRIDGE_OK', raw)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]['path'], '/inference/openai/v1/chat/completions')
        self.assertEqual(self.calls[0]['headers']['Authorization'], 'Bearer public')
        self.assertNotIn('_omp_free_session', self.calls[0]['body'])

    def get(self, path):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        conn.request('GET', path, headers={'Authorization': 'Bearer ' + 'L' * 40})
        r = conn.getresponse()
        result = (r.status, b.load_json(r.read()))
        conn.close()
        return result

    def test_removed_unified_route_is_refused_by_name_never_resolved(self):
        # The model exists on both lanes: the old route must still pick neither.
        for row in copy.deepcopy(self.server.catalog.data['models']):
            row['lane'] = 'go'
            self.server.catalog.data['models'].append(row)
        for endpoint in b.APIS:
            with self.subTest(endpoint=endpoint):
                model = 'demo-' + endpoint.replace('/', '-')
                status, raw = self.request({'model': model, 'stream': True, '_omp_free_session': 's'},
                                           '/free/v1/' + endpoint)
                self.assertEqual(status, 404)
                value = b.load_json(raw)
                detail = value if endpoint == 'responses' else value['error']
                self.assertEqual(detail['code'], 'RouteRemoved')
                self.assertIn('opencode-zen-free', detail['message'])
                self.assertIn('opencode-go-free', detail['message'])
                if endpoint == 'messages':
                    self.assertEqual(value['type'], 'error')
        for path in ['/free', '/free/v1/models']:
            with self.subTest(path=path):
                status, value = self.get(path)
                self.assertEqual(status, 404)
                self.assertEqual(value['error']['code'], 'RouteRemoved')
                self.assertIn('opencode-go-free', value['error']['message'])
        self.assertFalse(self.calls)

    def test_only_zen_and_go_listings_are_served(self):
        go = copy.deepcopy(self.server.catalog.data['models'][0])
        go.update(lane='go', id='go-only')
        self.server.catalog.data['models'].append(go)
        status, zen = self.get('/zen/v1/models')
        self.assertEqual(status, 200)
        self.assertNotIn('go-only', [m['id'] for m in zen['data']])
        self.assertEqual(len(zen['data']), len(b.APIS))
        status, value = self.get('/go/v1/models')
        self.assertEqual((status, [m['id'] for m in value['data']]), (200, ['go-only']))
        self.assertEqual(self.get('/opencode-free/v1/models')[0], 404)

    def test_actual_loopback_responses_protocol(self):
        self.response = FakeResponse(frame({'type': 'response.output_text.delta', 'delta': 'BRIDGE_OK'}) + frame({'type': 'response.completed', 'response': {'output': []}}))
        status, raw = self.request({'model': 'demo-responses', 'stream': True, '_omp_free_session': 'http-test'}, '/zen/v1/responses')
        self.assertEqual(status, 200)
        self.assertIn(b'response.completed', raw)
        self.assertEqual(self.calls[0]['path'], '/inference/openai/v1/responses')

    def test_actual_loopback_anthropic_protocol_and_auth(self):
        go = copy.deepcopy(self.server.catalog.data['models'][-1])
        go['lane'] = 'go'
        self.server.catalog.data['models'].append(go)
        self.response = FakeResponse(frame({'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': 'OK'}}) + frame({'type': 'message_stop'}))
        status, raw = self.request({'model': 'demo-messages', 'stream': True}, '/go/v1/messages',
            {'Authorization': '', 'x-api-key': 'L' * 40, 'X-Claude-Code-Session-Id': 'anthropic-session'})
        self.assertEqual(status, 200)
        self.assertIn(b'message_stop', raw)
        self.assertEqual(self.calls[0]['path'], '/zen/go/v1/messages')
        self.assertEqual(self.calls[0]['headers']['X-Api-Key'], 'test-upstream-key-ONLY-FIXTURE')
        self.assertNotIn('Authorization', self.calls[0]['headers'])

    def test_go_without_key_is_rejected_before_upstream(self):
        go = copy.deepcopy(self.server.catalog.data['models'][0])
        go['lane'] = 'go'
        self.server.catalog.data['models'].append(go)
        self.server.key = ''
        status, raw = self.request(path='/go/v1/chat/completions')
        self.assertEqual(status, 401)
        self.assertIn(b'GoCredentialsMissing', raw)
        self.assertFalse(self.calls)

    def test_unknown_local_endpoint_is_refused(self):
        status, _ = self.request({'model': 'demo-chat-completions', 'stream': True, '_omp_free_session': 's'},
                                 '/zen/v1/systemone')
        self.assertEqual(status, 404)
        self.assertEqual(self.calls, [])

    def test_paid_model_blocked_before_connect(self):
        status, _ = self.request({'model': 'paid-model', 'stream': True})
        self.assertEqual(status, 403)
        self.assertFalse(self.calls)

    def test_wrong_lane_blocked(self):
        self.assertEqual(self.request(path='/go/v1/chat/completions')[0], 403)
        self.assertFalse(self.calls)

    def test_bad_local_token_blocked(self):
        self.assertEqual(self.request(headers={'Authorization': 'Bearer wrong'})[0], 401)
        self.assertFalse(self.calls)

    def test_bad_host_and_browser_origin_blocked(self):
        for headers in [{'Host': 'evil.invalid'}, {'Origin': 'https://evil.invalid'}]:
            self.assertEqual(self.request(headers=headers)[0], 403)
        self.assertFalse(self.calls)

    def test_path_injection_blocked(self):
        for path in ['/zen/v1/responses?url=https://evil.invalid', '/zen/v1/../models', '/https://evil.invalid']:
            self.assertEqual(self.request(path=path)[0], 404)
        self.assertFalse(self.calls)

    def test_compressed_body_rejected(self):
        self.assertEqual(self.request(headers={'Content-Encoding': 'gzip'})[0], 400)
        self.assertFalse(self.calls)

    def test_upstream_403_stops_lane_and_never_retries(self):
        self.response = FakeResponse(b'{"error":{"type":"FreeTierError","message":"Denied"}}', 403)
        self.assertEqual(self.request()[0], 403)
        self.assertEqual(self.request()[0], 409)
        self.assertEqual(len(self.calls), 1)

    def test_upstream_402_no_paid_fallback(self):
        self.response = FakeResponse(b'{"error":{"message":"Insufficient funds"}}', 402)
        status, raw = self.request()
        self.assertEqual(status, 402)
        self.assertIn(b'No retry', raw)
        self.assertEqual(self.request()[0], 409)
        self.assertEqual(len(self.calls), 1)

    def test_upstream_429_cooldown_not_success(self):
        self.response = FakeResponse(b'{"error":{"type":"FreeUsageLimitError","message":"Limit"}}', 429, {'Retry-After': '90'})
        status, raw = self.request()
        self.assertEqual(status, 429)
        self.assertIn(b'NOT a successful', raw)
        self.assertEqual(self.request()[0], 409)
        self.assertEqual(len(self.calls), 1)

    def test_free_quota_429_pauses_the_whole_zen_lane(self):
        # Zen's free quota is shared by every model: switching models must not re-hit upstream.
        self.response = FakeResponse(b'{"type":"error","error":{"type":"FreeUsageLimitError","message":"Rate limit exceeded."}}',
                                     429, {'Retry-After': '19184'})
        status, raw = self.request()
        self.assertEqual(status, 429)
        self.assertIn(b'Resets at', raw)
        status, raw = self.request({'model': 'demo-responses', 'stream': True, 'input': 'x', '_omp_free_session': 'x'},
                                   path='/zen/v1/responses')
        self.assertEqual(status, 409)
        self.assertIn(b'Resets at', raw)
        self.assertIn(b'FreeQuotaCooldown', raw)
        self.assertEqual(len(self.calls), 1)

    def test_free_quota_cooldown_honors_retry_after_beyond_an_hour(self):
        self.response = FakeResponse(b'{"error":{"type":"FreeUsageLimitError","message":"Limit"}}', 429, {'Retry-After': '19184'})
        self.request()
        self.assertGreater(self.server.lane_cooldowns['zen'] - time.time(), 19000)

    def test_free_quota_cooldown_leaves_go_lane_alone(self):
        self.server.catalog.data['models'].append(dict(self.server.catalog.data['models'][0], lane='go'))
        self.response = FakeResponse(b'{"error":{"type":"FreeUsageLimitError","message":"Limit"}}', 429, {'Retry-After': '600'})
        self.request()
        self.response = FakeResponse(frame({'choices': [{'delta': {'content': 'BRIDGE_OK'}}]}) + b'data: [DONE]\n\n')
        self.assertEqual(self.request(path='/go/v1/chat/completions')[0], 200)
        self.assertEqual(len(self.calls), 2)

    def test_other_429_keeps_a_per_model_cooldown(self):
        self.response = FakeResponse(b'{"error":{"type":"RateLimitError","message":"Slow down"}}', 429, {'Retry-After': '90'})
        self.request()
        self.response = FakeResponse(frame({'choices': [{'delta': {'content': 'BRIDGE_OK'}}]}) + b'data: [DONE]\n\n')
        status, _ = self.request({'model': 'demo-responses', 'stream': True, 'input': 'x', '_omp_free_session': 'x'},
                                 path='/zen/v1/responses')
        self.assertEqual(status, 200)
        self.assertEqual(len(self.calls), 2)

    def test_redirect_not_followed(self):
        self.response = FakeResponse(b'', 302, {'Location': 'https://evil.invalid'})
        self.assertEqual(self.request()[0], 302)
        self.assertEqual(len(self.calls), 1)

    def test_sse_stub_call_stopped_before_forward(self):
        bad = {'choices': [{'delta': {'tool_calls': [{'index': 0, 'function': {'name': 'bash', 'arguments': '{}'}}]}}]}
        self.response = FakeResponse(frame(bad) + b'data: [DONE]\n\n')
        status, raw = self.request()
        self.assertEqual(status, 200)  # Headers already started; protocol error event follows.
        self.assertIn(b'BridgeStubToolError', raw)
        self.assertNotIn(b'"name":"bash"', raw)
        self.assertNotIn(b'[DONE]', raw)

    def test_truncated_stream_reports_failure(self):
        self.response = FakeResponse(frame({'choices': [{'delta': {'content': 'partial'}}]}))
        self.assertIn(b'TruncatedStream', self.request()[1])

    def test_truly_incremental_sse_not_whole_answer_buffered(self):
        outer = self
        first = frame({'choices': [{'delta': {'content': 'first'}}]})
        class Gated(FakeResponse):
            def readline(self, limit=-1):
                if self.tell() >= len(first): outer.release_stream.wait(2)
                return super().readline(limit)
        self.response = Gated(first + b'data: [DONE]\n\n')
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=1)
        conn.request('POST', '/zen/v1/chat/completions', b.encode({'model': 'demo-chat-completions', 'stream': True, '_omp_free_session': 's'}),
                     {'Authorization': 'Bearer ' + 'L' * 40})
        response = conn.getresponse()
        self.assertIn(b'first', response.readline())
        self.assertFalse(self.release_stream.is_set())
        self.release_stream.set()
        self.assertIn(b'[DONE]', response.read())
        conn.close()

    def test_client_disconnect_closes_upstream(self):
        outer = self
        first = frame({'choices': [{'delta': {'content': 'first'}}]})
        class Gated(FakeResponse):
            def readline(self, limit=-1):
                if self.tell() >= len(first): outer.release_stream.wait(2)
                return super().readline(limit)
        self.response = Gated(first + b'data: [DONE]\n\n')
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=2)
        conn.request('POST', '/zen/v1/chat/completions', b.encode({'model': 'demo-chat-completions', 'stream': True, '_omp_free_session': 's'}),
                     {'Authorization': 'Bearer ' + 'L' * 40})
        response = conn.getresponse()
        response.readline()
        response.close()
        conn.close()
        self.assertTrue(self.closed.wait(1.5), 'upstream was not cancelled after client disconnect')

    def test_parallel_requests_all_reach_upstream(self):
        from concurrent.futures import ThreadPoolExecutor
        count = 6
        barrier = threading.Barrier(count)
        calls = []

        class Connection:
            sock = None
            def request(self, method, path, body, headers):
                calls.append(path)
            def getresponse(self):
                # No response is released until all six calls reach upstream.
                barrier.wait(timeout=2)
                return FakeResponse(frame({'choices': [{'delta': {'content': 'OK'}}]}) + b'data: [DONE]\n\n')
            def close(self):
                pass

        self.server.connector = Connection
        with ThreadPoolExecutor(max_workers=count) as pool:
            results = list(pool.map(lambda _: self.request(), range(count)))
        self.assertEqual(len(calls), count)
        self.assertEqual([status for status, _ in results], [200] * count)
        self.assertTrue(all(b'LocalConcurrencyLimit' not in body for _, body in results))

    def test_no_upstream_secret_in_errors(self):
        self.response = FakeResponse(b'{"error":{"message":"test-upstream-key-ONLY-FIXTURE rejected"}}', 401)
        raw = self.request()[1]
        self.assertNotIn(b'test-upstream-key-ONLY-FIXTURE', raw)
        self.assertIn(b'REDACTED', raw)


if __name__ == '__main__':
    unittest.main(verbosity=2)
