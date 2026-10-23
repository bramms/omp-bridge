"""Offline unit + real loopback HTTP tests. No provider credentials or external calls."""
import calendar
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import socket
import socketserver
import ssl
import sys
import tempfile
import threading
import time
import unittest
import http.client
from types import SimpleNamespace
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
    def test_invalid_saved_cooldown_ip_binding_is_rejected(self):
        for ips in ([], {'a:80': 'invalid'}, {'a:80': 123}):
            with self.subTest(ips=ips), tempfile.TemporaryDirectory() as root:
                b.save_private(Path(root) / 'zen-cooldowns.json', b.encode({
                    'schema': 2, 'quotas': {}, 'transport': {}, 'ips': ips}))
                with self.assertRaises(b.BridgeError):
                    b.BridgeServer(('127.0.0.1', 0), StaticCatalog(), '', 'L' * 40,
                                   zen_proxies=(('a', 80),), state=root)

    def test_doctor_memory_uses_bound_ports_and_handles_docker_memory_units(self):
        health = {'ok': True, 'zen_proxies': [{'id': 1, 'endpoint': '127.0.0.1:18097'},
                                              {'id': 8, 'endpoint': '127.0.0.1:18090'},
                                              {'id': 3, 'endpoint': 'remote:18092'}]}
        containers = [{'Name': '/node-a', 'Config': {'Labels': {'com.docker.compose.service': 'warp-a'}},
                       'State': {'Running': True, 'StartedAt': '2026-10-21T10:59:43.18172338Z'},
                       'NetworkSettings': {'Ports': {'8888/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '18090'}]}}},
                      {'Name': '/node-h', 'Config': {'Labels': {'com.docker.compose.service': 'warp-h'}},
                       'State': {'Running': True, 'StartedAt': '2026-10-23T12:34:34.257340514Z'},
                       'NetworkSettings': {'Ports': {'8888/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '18097'}]}}}]
        values = [SimpleNamespace(stdout='aaaaaaaaaaaa\nbbbbbbbbbbbb\n'),
                  SimpleNamespace(stdout=json.dumps(containers)),
                  SimpleNamespace(stdout='\n'.join(json.dumps(row) for row in [
                      {'Name': 'node-a', 'MemUsage': '128MB / 8GiB'},
                      {'Name': 'node-h', 'MemUsage': '2GiB / 8GiB'}]))]
        with patch.object(b.sys, 'platform', 'darwin'), patch.dict(os.environ, {}, clear=True), \
             patch.object(b.time, 'time', return_value=calendar.timegm((2026, 10, 23, 13, 10, 0))), \
             patch.object(b.subprocess, 'run', side_effect=values) as run:
            memory = b.doctor_memory(health)
        self.assertEqual(memory['routes'], {'127.0.0.1:18090': 128000000, '127.0.0.1:18097': 2 * 1024 ** 3})
        self.assertEqual(memory['uptime_seconds'], {'127.0.0.1:18090': 180616, '127.0.0.1:18097': 2125})
        self.assertIsNone(memory['error'])
        self.assertTrue(all(call.args[0][:3] == ['docker', '--context', 'colima'] for call in run.call_args_list))
        self.assertIn('--no-stream', run.call_args.args[0])
        report = b.doctor_report(health, memory)
        model_table = report.partition('Перевірки IP')[0]
        self.assertIn('2048.0', next(line for line in model_table.splitlines() if line.startswith('WARP 1')))
        self.assertIn('35хв', next(line for line in model_table.splitlines() if line.startswith('WARP 1')))
        self.assertIn('122.1', next(line for line in model_table.splitlines() if line.startswith('WARP 8')))
        self.assertIn('2д 02г 10хв', next(line for line in model_table.splitlines() if line.startswith('WARP 8')))
        self.assertIn('н/д', next(line for line in model_table.splitlines() if line.startswith('WARP 3')))
        self.assertIn('2170.1+', next(line for line in model_table.splitlines() if line.startswith('Разом')))

    def test_doctor_memory_failure_does_not_fail_the_report_or_show_false_zero(self):
        health = {'ok': True, 'zen_proxies': [{'id': 1, 'endpoint': '127.0.0.1:18090'}]}
        for error in (FileNotFoundError(), b.subprocess.TimeoutExpired('docker', 3),
                      b.subprocess.CalledProcessError(1, 'docker')):
            with self.subTest(error=type(error).__name__), patch.object(b.subprocess, 'run', side_effect=error):
                memory = b.doctor_memory(health)
            self.assertEqual(memory['routes'], {})
            self.assertTrue(memory['error'])
            report = b.doctor_report(health, memory)
            row = next(line for line in report.splitlines() if line.startswith('WARP 1'))
            self.assertIn('н/д', row)
            self.assertIn("Пам'ять WARP / час роботи:", report)

    def test_doctor_memory_honors_explicit_context_and_ignores_stopped_container_samples(self):
        health = {'zen_proxies': [{'id': 1, 'endpoint': 'localhost:18090'}]}
        container = {'Name': '/node', 'Config': {'Labels': {'com.docker.compose.service': 'warp-a'}},
                     'State': {'Running': True, 'StartedAt': '2026-10-21T10:59:43Z'},
                     'NetworkSettings': {'Ports': {'8888/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '18090'}]}}}
        values = [SimpleNamespace(stdout='aaaaaaaaaaaa\n'), SimpleNamespace(stdout=json.dumps([container])),
                  SimpleNamespace(stdout=json.dumps({'Name': 'node', 'MemUsage': '0B / 0B', 'PIDs': '0'}))]
        with patch.object(b.sys, 'platform', 'darwin'), patch.dict(os.environ, {'DOCKER_CONTEXT': 'chosen'}), \
             patch.object(b.subprocess, 'run', side_effect=values) as run:
            memory = b.doctor_memory(health)
            self.assertEqual(memory['routes'], {})
            self.assertEqual(memory['uptime_seconds'], {})
        self.assertTrue(all(call.args[0][1] != '--context' for call in run.call_args_list))

    def test_doctor_uptime_rejects_missing_invalid_future_and_stopped_start_times(self):
        health = {'zen_proxies': [{'id': 1, 'endpoint': '127.0.0.1:18090'}]}
        container = {'Name': '/node', 'Config': {'Labels': {'com.docker.compose.service': 'warp-a'}},
                     'NetworkSettings': {'Ports': {'8888/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '18090'}]}}}
        for state in ({}, {'Running': False, 'StartedAt': '2026-10-21T10:59:43Z'},
                      *({'Running': True, 'StartedAt': value} for value in
                        (None, 'invalid', '0001-01-01T00:00:00Z', '2027-01-01T00:00:00Z', '2026-10-21T10:59:43'))):
            values = [SimpleNamespace(stdout='aaaaaaaaaaaa\n'),
                      SimpleNamespace(stdout=json.dumps([{**container, 'State': state}])),
                      SimpleNamespace(stdout=json.dumps({'Name': 'node', 'MemUsage': '70MiB / 8GiB'}))]
            with self.subTest(state=state), patch.object(b.time, 'time', return_value=1792761000), \
                 patch.object(b.subprocess, 'run', side_effect=values):
                memory = b.doctor_memory(health)
            self.assertEqual(memory['uptime_seconds'], {})
            self.assertEqual(memory['routes'], {'127.0.0.1:18090': 70 * 1024 ** 2})

    def test_doctor_uptime_survives_stats_failure_and_changes_with_container_restart(self):
        health = {'zen_proxies': [{'id': 1, 'endpoint': '127.0.0.1:18090'}]}
        container = {'Name': '/node', 'Config': {'Labels': {'com.docker.compose.service': 'warp-a'}},
                     'NetworkSettings': {'Ports': {'8888/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '18090'}]}}}
        now = calendar.timegm((2026, 10, 23, 13, 10, 0))
        for start, seconds, display in (('2026-10-23T12:10:00Z', 3600, '1г 00хв'),
                                        ('2026-10-23T13:09:45Z', 15, '15с'),
                                        ('2026-10-23T13:10:00Z', 0, '0с')):
            state = {'Running': True, 'StartedAt': start}
            values = [SimpleNamespace(stdout='aaaaaaaaaaaa\n'),
                      SimpleNamespace(stdout=json.dumps([{**container, 'State': state}])),
                      b.subprocess.TimeoutExpired('docker', 8)]
            with self.subTest(start=start), patch.object(b.time, 'time', return_value=now), \
                 patch.object(b.subprocess, 'run', side_effect=values):
                memory = b.doctor_memory(health)
            self.assertEqual(memory['routes'], {})
            self.assertEqual(memory['uptime_seconds'], {'127.0.0.1:18090': seconds})
            self.assertTrue(memory['error'])
            row = next(line for line in b.doctor_report(health, memory).splitlines() if line.startswith('WARP 1'))
            self.assertIn(display, row)
            self.assertIn('н/д', row)

    def test_proxy_config_accepts_http_hosts_and_ipv6(self):
        self.assertEqual(b.parse_proxy('http://127.0.0.1:18090'), ('127.0.0.1', 18090))
        self.assertEqual(b.parse_proxy('http://proxy.local:8080/'), ('proxy.local', 8080))
        self.assertEqual(b.parse_proxy('http://[::1]:40000'), ('::1', 40000))

    def test_invalid_proxy_is_rejected_before_credentials_or_network(self):
        invalid = ['', '127.0.0.1:80', 'https://proxy:80', 'socks5://proxy:80',
                   'http://proxy', 'http://proxy:0', 'http://proxy:65536', 'http://proxy:abc',
                   'http://:80', 'http://[::1:80', 'http://user:secret@proxy:80',
                   'http://proxy:80/path', 'http://proxy:80?', 'http://proxy:80#',
                   ' http://proxy:80', 'http://proxy:80\r\nHost: injected']
        with patch.object(b, 'key_from_user') as key, patch.object(b, 'Catalog') as catalog:
            for value in invalid:
                with self.subTest(value=value), self.assertRaises(b.BridgeError):
                    b.main(['serve', '--zen-proxy', value])
            key.assert_not_called()
            catalog.assert_not_called()

    def test_duplicate_or_nine_proxy_endpoints_are_rejected_before_network(self):
        for ports in ([18090, 18090], list(range(18090, 18099))):
            args = ['serve']
            for port in ports:
                args += ['--zen-proxy', f'http://127.0.0.1:{port}']
            with patch.object(b, 'key_from_user') as key, patch.object(b, 'Catalog') as catalog:
                with self.assertRaises(b.BridgeError):
                    b.main(args)
                key.assert_not_called()
                catalog.assert_not_called()

    def test_serve_cli_passes_all_eight_proxies(self):
        args = ['serve']
        for port in range(18090, 18098):
            args += ['--zen-proxy', f'http://127.0.0.1:{port}']
        with patch.object(b, 'BridgeServer') as server, patch.object(b, 'Catalog', return_value=StaticCatalog()), \
             patch.object(b, 'key_from_user', return_value=''), patch.object(b, 'token_from_user', return_value='L' * 40), \
             patch.object(b, 'save_private'), patch('builtins.print'):
            self.assertEqual(b.main(args), 0)
            self.assertEqual(server.call_args.kwargs['zen_proxies'], tuple(('127.0.0.1', p) for p in range(18090, 18098)))

    def test_serve_loads_saved_pool_and_cli_can_override_it(self):
        with tempfile.TemporaryDirectory() as root, patch.object(b, 'STATE', Path(root)), \
             patch.object(b, 'BridgeServer') as server, patch.object(b, 'Catalog', return_value=StaticCatalog()), \
             patch.object(b, 'key_from_user', return_value=''), patch.object(b, 'token_from_user', return_value='L' * 40), \
             patch.object(b, 'save_private'), patch('builtins.print'):
            config = Path(root) / 'zen-proxies.json'
            config.write_text(json.dumps([f'http://127.0.0.1:{p}' for p in range(18090, 18098)]))
            self.assertEqual(b.main(['serve']), 0)
            self.assertEqual(server.call_args.kwargs['zen_proxies'], tuple(('127.0.0.1', p) for p in range(18090, 18098)))
            config.write_text('broken JSON')
            self.assertEqual(b.main(['serve', '--zen-proxy', 'http://127.0.0.1:18092']), 0)
            self.assertEqual(server.call_args.kwargs['zen_proxies'], (('127.0.0.1', 18092),))
            config.write_text('[]')
            self.assertEqual(b.main(['serve']), 0)
            self.assertEqual(server.call_args.kwargs['zen_proxies'], ())
            config.unlink()
            self.assertEqual(b.main(['serve']), 0)
            self.assertEqual(server.call_args.kwargs['zen_proxies'], ())

    def test_bad_saved_pool_is_rejected_before_credentials_or_network(self):
        invalid = ['{', 'null', '{}', '"http://proxy:80"', '[false]',
                   '["https://proxy:80"]', '["http://proxy:80", "http://proxy:80"]',
                   json.dumps([f'http://{c}:80' for c in 'abcdefghi'])]
        with tempfile.TemporaryDirectory() as root, patch.object(b, 'STATE', Path(root)), \
             patch.object(b, 'key_from_user') as key, patch.object(b, 'Catalog') as catalog:
            for raw in invalid:
                (Path(root) / 'zen-proxies.json').write_text(raw)
                with self.subTest(config=raw), self.assertRaises(b.BridgeError):
                    b.main(['serve'])
            key.assert_not_called()
            catalog.assert_not_called()

    def test_unreadable_saved_pool_does_not_fall_back_to_direct(self):
        with patch.object(Path, 'read_bytes', side_effect=PermissionError), \
             patch.object(b, 'key_from_user') as key, patch.object(b, 'Catalog') as catalog:
            with self.assertRaisesRegex(b.BridgeError, 'no direct fallback'):
                b.main(['serve'])
            key.assert_not_called()
            catalog.assert_not_called()

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


@contextlib.contextmanager
def connect_proxy(reject=False):
    """Loopback CONNECT endpoint followed by a real, certificate-checked TLS origin."""
    fixture = Path(__file__).parent / 'fixtures/connect-test.pem'
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(fixture)

    class Proxy(socketserver.StreamRequestHandler):
        def handle(self):
            self.connection.settimeout(3)
            self.server.connects.append((self.rfile.readline(), dict(http.client.parse_headers(self.rfile))))
            if reject:
                self.wfile.write(b'HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n')
                return
            self.wfile.write(b'HTTP/1.1 200 Connection established\r\n\r\n')
            try:
                with tls.wrap_socket(self.connection, server_side=True) as origin:
                    with origin.makefile('rb') as stream:
                        line = stream.readline()
                        headers = http.client.parse_headers(stream)
                        body = stream.read(int(headers['Content-Length']))
                        self.server.posts.append((line, dict(headers), b.load_json(body)))
                        if self.server.quota:
                            raw = b'{"error":{"type":"FreeUsageLimitError","message":"Test quota exhausted"}}'
                            origin.sendall(b'HTTP/1.1 429 Too Many Requests\r\nContent-Type: application/json\r\n'
                                           b'Retry-After: 19184\r\n' + f'Content-Length: {len(raw)}\r\n\r\n'.encode() + raw)
                            return
                        first = frame({'choices': [{'delta': {'content': 'BRIDGE_OK'}}]})
                        last = b'data: [DONE]\n\n'
                        origin.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n'
                                       + f'Content-Length: {len(first) + len(last)}\r\n\r\n'.encode() + first)
                        self.server.release.wait(2)
                        origin.sendall(last)
            except OSError:
                pass  # A rejected certificate intentionally ends the TLS handshake.

    server = socketserver.ThreadingTCPServer(('127.0.0.1', 0), Proxy)
    server.daemon_threads = True
    server.connects, server.posts, server.names = [], [], []
    server.quota = False
    server.release = threading.Event()
    server.release.set()
    tls.set_servername_callback(lambda sock, name, context: server.names.append(name))
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
    thread.start()
    try:
        yield server, ssl.create_default_context(cafile=str(fixture))
    finally:
        server.release.set()
        server.shutdown()
        server.server_close()
        thread.join(2)


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
                outer.calls.append({'method': method, 'path': path, 'body': b.load_json(body),
                                    'wire_body': body, 'headers': headers})
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

    def request(self, payload=None, path='/zen/v1/chat/completions', headers=None, with_headers=False):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        h = {'Authorization': 'Bearer ' + 'L' * 40, 'Content-Type': 'application/json'}
        h.update(headers or {})
        data = payload if payload is not None else {'model': 'demo-chat-completions', 'stream': True, '_omp_free_session': 'http-test'}
        conn.request('POST', path, b.encode(data), h)
        r = conn.getresponse()
        result = (r.status, r.read())
        if with_headers:
            result += (dict(r.getheaders()),)
        conn.close()
        return result

    def set_proxies(self, *proxies, direct_available=False):
        self.server.zen_proxies = tuple(proxies)
        self.server.proxy_until = [0.0] * len(proxies)
        # Most pool tests isolate WARP by marking only the direct transport down.
        self.server.direct_transport_until = 0.0 if direct_available else time.time() + 20000

    @contextlib.contextmanager
    def trace_responses(self, values):
        calls = []
        class Connection:
            def __init__(self, proxy): self.proxy = proxy
            def request(self, method, path, headers):
                calls.append((self.proxy, method, path))
                if isinstance(values[self.proxy], Exception):
                    raise values[self.proxy]
            def getresponse(self): return FakeResponse(values[self.proxy])
            def close(self): pass
        with patch.object(b, 'upstream_connection', side_effect=Connection):
            yield calls

    def test_ip_monitor_clears_only_changed_warp_and_preserves_direct_and_other_seven(self):
        self.set_proxies(*((f'warp-{i}', 80) for i in range(8)))
        until = time.time() + 19000
        self.server.zen_route_quotas = dict.fromkeys(['direct', *(self.server.route_key(i) for i in range(8))], until)
        self.server.proxy_until = [until] * 8
        direct_transport = self.server.direct_transport_until
        values = {None: b'ip=192.0.2.1\nwarp=off\n', **{
            (f'warp-{i}', 80): f'ip=2001:db8::{i+1}\nwarp=on\n'.encode() for i in range(8)}}
        with self.trace_responses(values) as calls:
            self.server.refresh_zen_ips()
            self.assertEqual(self.server.zen_route_quotas['warp-2:80'], until)  # First observation is not a change.
            values[('warp-2', 80)] = b'ip=2001:db8::99\nwarp=on\n'
            values[None] = b'ip=192.0.2.2\nwarp=off\n'
            self.server.refresh_zen_ips()
        observed = self.server.ip_observations['warp-2:80']
        self.assertEqual(observed['ip'], '2001:db8::99')
        self.assertEqual(observed['previous_ip'], '2001:db8::3')
        self.assertEqual(observed['changes'], 1)
        self.assertIn('cooldown_reset_at', observed)
        self.assertEqual(self.server.zen_route_quotas,
                         dict.fromkeys(['direct', *(self.server.route_key(i) for i in range(8) if i != 2)], until))
        self.assertEqual(self.server.proxy_until, [until, until, 0, until, until, until, until, until])
        self.assertEqual(self.server.direct_transport_until, direct_transport)
        self.assertEqual(self.server.select_proxy('zen', 'stable'), 2)
        self.assertTrue(all(method == 'GET' and path == '/cdn-cgi/trace' for _, method, path in calls))
        self.assertTrue(all(row['inference']['attempts'] == 0 for row in self.server.stats.snapshot()['routes'].values()))
        self.assertFalse(self.calls)

    def test_same_ip_failed_trace_and_unconfirmed_warp_keep_existing_cooldowns(self):
        self.set_proxies(('a', 80))
        until = time.time() + 19000
        self.server.zen_route_quotas = {'a:80': until}
        self.server.proxy_until = [until]
        values = {None: b'ip=192.0.2.1\nwarp=off\n', ('a', 80): b'ip=2001:db8::1\nwarp=on\n'}
        with self.trace_responses(values):
            self.server.refresh_zen_ips()
            for response in (values[('a', 80)], OSError('offline'), b'ip=2001:db8::2\nwarp=off\n',
                             b'ip=invalid\nwarp=on\n'):
                with self.subTest(response=response):
                    values[('a', 80)] = response
                    self.server.refresh_zen_ips()
                    self.assertEqual(self.server.zen_route_quotas, {'a:80': until})
                    self.assertEqual(self.server.proxy_until, [until])
                    self.assertEqual(self.server.zen_route_ips, {'a:80': '2001:db8::1'})

    def test_ip_reset_persists_and_stale_ip_history_does_not_clear_a_new_refusal(self):
        self.set_proxies(('a', 80), ('b', 80))
        values = {None: b'ip=192.0.2.1\nwarp=off\n', ('a', 80): b'ip=2001:db8::1\nwarp=on\n',
                  ('b', 80): b'ip=2001:db8::3\nwarp=on\n'}
        until = time.time() + 19000
        with tempfile.TemporaryDirectory() as root:
            self.server.cooldown_path = Path(root) / 'zen-cooldowns.json'
            self.server.ip_path = Path(root) / 'zen-ips.json'
            self.server.zen_route_quotas = {'direct': until, 'a:80': until, 'b:80': until}
            self.server.proxy_until = [until, until]
            with self.trace_responses(values):
                self.server.refresh_zen_ips()
                old_history = self.server.ip_path.read_bytes()
                values[('a', 80)] = b'ip=2001:db8::2\nwarp=on\n'
                self.server.refresh_zen_ips()
            with b.BridgeServer(('127.0.0.1', 0), StaticCatalog(), '', 'L' * 40,
                                zen_proxies=(('b', 80), ('a', 80)), state=root) as restored:
                self.assertEqual(restored.zen_route_quotas, {'direct': until, 'b:80': until})
                self.assertEqual(restored.proxy_until, [until, 0])
            self.server.zen_route_quotas['a:80'] = until + 100
            self.server.save_zen_cooldowns()
            b.save_private(self.server.ip_path, old_history)
            with b.BridgeServer(('127.0.0.1', 0), StaticCatalog(), '', 'L' * 40,
                                zen_proxies=(('b', 80), ('a', 80)), state=root) as restored:
                with self.trace_responses(values): restored.refresh_zen_ips()
                self.assertEqual(restored.zen_route_quotas['a:80'], until + 100)
                self.assertEqual(restored.zen_route_ips['a:80'], '2001:db8::2')
            self.server.cooldown_path = self.server.ip_path = None

    def test_failed_cooldown_save_does_not_consume_the_confirmed_ip_change(self):
        self.set_proxies(('a', 80))
        values = {None: b'ip=192.0.2.1\nwarp=off\n', ('a', 80): b'ip=2001:db8::1\nwarp=on\n'}
        until = time.time() + 19000
        self.server.zen_route_quotas = {'a:80': until}
        self.server.proxy_until = [until]
        with self.trace_responses(values):
            self.server.refresh_zen_ips()
            values[('a', 80)] = b'ip=2001:db8::2\nwarp=on\n'
            with patch.object(self.server, 'save_zen_cooldowns', side_effect=OSError('disk full')):
                with self.assertRaises(OSError): self.server.refresh_zen_ips()
            self.assertEqual(self.server.zen_route_quotas, {'a:80': until})
            self.assertEqual(self.server.proxy_until, [until])
            self.assertEqual(self.server.ip_observations['a:80']['ip'], '2001:db8::1')
            self.server.refresh_zen_ips()
        self.assertNotIn('a:80', self.server.zen_route_quotas)
        self.assertEqual(self.server.proxy_until, [0])

    def test_late_refusal_from_previous_ip_cannot_reblock_the_changed_route(self):
        self.set_proxies(('a', 80), ('b', 80))
        self.server.zen_route_ips['a:80'] = '2001:db8::1'
        values = {None: b'ip=192.0.2.1\nwarp=off\n', ('a', 80): b'ip=2001:db8::2\nwarp=on\n',
                  ('b', 80): b'ip=2001:db8::3\nwarp=on\n'}
        def connect(lane, proxy):
            conn = self.server.connector()
            if proxy == 0:
                def response():
                    with self.trace_responses(values): self.server.refresh_zen_ips()
                    return FakeResponse(b'{"error":{"type":"FreeUsageLimitError"}}', 429,
                                        {'Retry-After': '19000'})
                conn.getresponse = response
            return conn
        with patch.object(self.server, 'connect_upstream', side_effect=connect):
            status, raw, headers = self.request(with_headers=True)
        self.assertEqual(status, 200)
        self.assertIn(b'BRIDGE_OK', raw)
        self.assertEqual(headers['X-OMP-Zen-Proxy'], '2')
        self.assertNotIn('a:80', self.server.zen_route_quotas)
        self.server.cool_proxy(0, 30, revision=0)
        self.assertEqual(self.server.proxy_until[0], 0)

    def test_changed_warp_reenters_automatic_quota_failover_in_configured_order(self):
        self.set_proxies(*((f'warp-{i}', 80) for i in range(8)))
        until = time.time() + 19000
        self.server.zen_route_quotas = dict.fromkeys(['direct', *(self.server.route_key(i) for i in range(8))], until)
        values = {None: b'ip=192.0.2.1\nwarp=off\n', **{
            (f'warp-{i}', 80): f'ip=2001:db8::{i+1}\nwarp=on\n'.encode() for i in range(8)}}
        with self.trace_responses(values):
            self.server.refresh_zen_ips()
            for i in (2, 3): values[(f'warp-{i}', 80)] = f'ip=2001:db8::{i+20}\nwarp=on\n'.encode()
            self.server.refresh_zen_ips()
        def connect(lane, proxy):
            conn = self.server.connector()
            if proxy == 2:
                conn.getresponse = lambda: FakeResponse(b'{"error":{"type":"FreeUsageLimitError"}}', 429)
            return conn
        with patch.object(self.server, 'connect_upstream', side_effect=connect) as connections:
            status, raw, headers = self.request(with_headers=True)
        self.assertEqual(status, 200)
        self.assertIn(b'BRIDGE_OK', raw)
        self.assertEqual(headers['X-OMP-Zen-Proxy'], '4')
        self.assertEqual([call.args[1] for call in connections.call_args_list], [2, 3])
        self.assertEqual(self.calls[-2]['wire_body'], self.calls[-1]['wire_body'])
        self.assertEqual(self.server.zen_route_quotas['direct'], until)
        self.assertGreater(self.server.zen_route_quotas['warp-2:80'], time.time())
        self.assertNotIn('warp-3:80', self.server.zen_route_quotas)

    def test_ip_monitor_exposes_duplicates_and_keeps_errors_visible(self):
        self.set_proxies(('a', 80), ('b', 80), direct_available=True)
        values = {None: b'ip=192.0.2.1\nwarp=off\n', ('a', 80): b'ip=2001:db8::1\nwarp=on\n',
                  ('b', 80): b'ip=2001:db8::1\nwarp=on\n'}
        with self.trace_responses(values):
            self.server.refresh_zen_ips()
            self.assertEqual(set(self.get('/health')[1]['duplicate_ips']['2001:db8::1']), {'a:80', 'b:80'})
            values[('a', 80)] = OSError('offline')
            self.server.refresh_zen_ips()
        observed = self.server.ip_observations['a:80']
        self.assertEqual(observed['ip'], '2001:db8::1')
        self.assertEqual(observed['error'], 'OSError')
        self.assertFalse(self.get('/health')[1]['duplicate_ips'])

    def test_ip_monitor_requires_confirmed_warp_and_valid_ip(self):
        self.set_proxies(('a', 80), direct_available=True)
        values = {None: b'ip=invalid\nwarp=off\n', ('a', 80): b'ip=2001:db8::1\nwarp=off\n'}
        with self.trace_responses(values):
            self.server.refresh_zen_ips()
        self.assertTrue(all(row.get('error') for row in self.server.ip_observations.values()))
        self.assertFalse(any(row.get('ip') for row in self.server.ip_observations.values()))

    def test_ip_change_history_survives_restart(self):
        self.set_proxies(('a', 80), direct_available=True)
        values = {None: b'ip=192.0.2.1\nwarp=off\n', ('a', 80): b'ip=2001:db8::1\nwarp=on\n'}
        with tempfile.TemporaryDirectory() as root:
            self.server.ip_path = Path(root) / 'zen-ips.json'
            with self.trace_responses(values):
                self.server.refresh_zen_ips()
                values[('a', 80)] = b'ip=2001:db8::2\nwarp=on\n'
                self.server.refresh_zen_ips()
            with b.BridgeServer(('127.0.0.1', 0), StaticCatalog(), '', 'L' * 40,
                               zen_proxies=(('a', 80),), state=root) as restored:
                self.assertEqual(restored.ip_observations['a:80']['changes'], 1)
                self.assertEqual(restored.ip_observations['a:80']['previous_ip'], '2001:db8::1')
            self.assertEqual(self.server.ip_path.stat().st_mode & 0o777, 0o600)
            self.server.ip_path = None

    def test_bad_ip_snapshot_is_reported_and_recovered_without_blocking_startup(self):
        values = {None: b'ip=192.0.2.1\nwarp=off\n', ('a', 80): b'ip=2001:db8::1\nwarp=on\n'}
        with tempfile.TemporaryDirectory() as root:
            b.save_private(Path(root) / 'zen-ips.json', b'{broken')
            with b.BridgeServer(('127.0.0.1', 0), StaticCatalog(), '', 'L' * 40,
                               zen_proxies=(('a', 80),), state=root) as restored:
                self.assertEqual(restored.ip_monitor_error, 'InvalidSavedIPSnapshot')
                with self.trace_responses(values):
                    restored.refresh_zen_ips()
                self.assertIsNone(restored.ip_monitor_error)
                self.assertEqual(restored.ip_observations['a:80']['ip'], '2001:db8::1')

    def test_doctor_default_tables_map_all_eight_routes_and_keep_ip_counts_separate(self):
        self.set_proxies(*((f'warp-{i}', 80) for i in reversed(range(8))), direct_available=True)
        self.server.stats.record('warp-0:80', 'inference', 'attempts')
        for _ in range(3): self.server.stats.record('direct', 'ip_checks', 'attempts')
        self.server.stats.persistence_error = 'PermissionError'
        health = self.get('/health')[1]
        out = io.StringIO()
        with patch.object(b, 'local_request', return_value=contextlib.nullcontext(FakeResponse(b.encode(health)))), \
             contextlib.redirect_stdout(out):
            self.assertEqual(b.main(['doctor']), 0)
        text = out.getvalue()
        model_table, _, ip_table = text.partition('Перевірки IP')
        self.assertIn('Модельні запити', model_table)
        self.assertIn('Транспортні помилки', model_table)
        self.assertIn('Помилка збереження: PermissionError', text)
        self.assertNotIn('"zen_stats"', text)
        self.assertEqual(next(line for line in model_table.splitlines() if line.startswith('WARP 8')).split()[2:6],
                         ['1', '0', '0', '0'])
        self.assertEqual(next(line for line in model_table.splitlines() if line.startswith('direct')).split()[1:5],
                         ['0', '0', '0', '0'])
        self.assertEqual(next(line for line in ip_table.splitlines() if line.startswith('direct')).split()[1:4],
                         ['3', '0', '0'])
        for label in ('direct', *(f'WARP {i}' for i in range(1, 9))):
            self.assertEqual(sum(line.startswith(label + ' ') for line in text.splitlines()), 2)

    def test_doctor_json_preserves_the_full_health_snapshot_beyond_16_kib(self):
        health = self.get('/health')[1]
        health['extra_diagnostics'] = 'x' * 20000
        out = io.StringIO()
        with patch.object(b, 'local_request', return_value=contextlib.nullcontext(FakeResponse(b.encode(health)))), \
             patch.object(b, 'doctor_memory') as memory, \
             contextlib.redirect_stdout(out):
            self.assertEqual(b.main(['doctor', '--json']), 0)
        memory.assert_not_called()
        self.assertEqual(b.load_json(out.getvalue()), health)

    def test_route_stats_count_quota_failover_and_ignore_local_cooldowns_and_go(self):
        self.set_proxies(('a', 80), ('b', 80), direct_available=True)
        self.response = FakeResponse(b'{"error":{"type":"FreeUsageLimitError"}}', 429)
        def connect(proxy):
            self.response = (FakeResponse(b'{"error":{"type":"FreeUsageLimitError"}}', 429)
                             if proxy == ('a', 80) else FakeResponse(b'data: [DONE]\n\n'))
            return self.server.connector()
        with patch.object(b, 'upstream_connection', side_effect=connect):
            self.assertEqual(self.request()[0], 200)
        counters = self.get('/health')[1]['zen_stats']['routes']
        self.assertEqual(counters['direct']['inference'],
                         {'attempts': 1, 'successes': 0, 'http_429': 1, 'transport_errors': 0})
        self.assertEqual(counters['a:80']['inference'], counters['direct']['inference'])
        self.assertEqual(counters['b:80']['inference'],
                         {'attempts': 1, 'successes': 1, 'http_429': 0, 'transport_errors': 0})
        self.server.zen_route_quotas['b:80'] = time.time() + 60
        self.assertEqual(self.request()[0], 503)
        self.server.catalog.data['models'].append(dict(self.server.catalog.data['models'][0], lane='go'))
        self.response = FakeResponse(b'data: [DONE]\n\n')
        self.assertEqual(self.request(path='/go/v1/chat/completions')[0], 200)
        self.assertEqual(self.get('/health')[1]['zen_stats']['routes'], counters)

    def test_ip_check_stats_are_separate_and_include_connection_setup_failures(self):
        self.set_proxies(('a', 80), ('b', 80))
        def connect(proxy):
            if proxy == ('b', 80): raise OSError('offline')
            values = {None: b'ip=192.0.2.1\nwarp=off\n', ('a', 80): b'ip=2001:db8::1\nwarp=on\n'}
            response = FakeResponse(values[proxy])
            class Connection:
                def request(self, *args, **kwargs): pass
                def getresponse(self): return response
                def close(self): pass
            return Connection()
        with patch.object(b, 'upstream_connection', side_effect=connect):
            self.server.refresh_zen_ips()
        rows = self.get('/health')[1]['zen_stats']['routes']
        self.assertEqual(rows['direct']['ip_checks'], {'attempts': 1, 'successes': 1, 'errors': 0})
        self.assertEqual(rows['a:80']['ip_checks'], rows['direct']['ip_checks'])
        self.assertEqual(rows['b:80']['ip_checks'], {'attempts': 1, 'successes': 0, 'errors': 1})
        self.assertTrue(all(not any(row['inference'].values()) for row in rows.values()))

    def test_route_stats_survive_restart_and_pool_reordering(self):
        self.set_proxies(('a', 80), ('b', 80), direct_available=True)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'zen-stats.json'
            self.server.stats = b.RouteStats(path, ['direct', 'a:80', 'b:80'])
            self.assertEqual(self.request()[0], 200)
            self.server.cool_direct_transport()
            with patch.object(b, 'upstream_connection', side_effect=OSError('offline')):
                self.assertEqual(self.request()[0], 502)
            self.response = FakeResponse(b'{"error":{"type":"FreeUsageLimitError"}}', 429)
            with patch.object(b, 'upstream_connection', side_effect=lambda _: self.server.connector()):
                self.assertEqual(self.request()[0], 503)
            values = {None: b'ip=192.0.2.1\nwarp=off\n', ('a', 80): b'ip=2001:db8::1\nwarp=on\n',
                      ('b', 80): OSError('trace failed')}
            with self.trace_responses(values): self.server.refresh_zen_ips()
            original = self.server.stats.snapshot()
            with b.BridgeServer(('127.0.0.1', 0), StaticCatalog(), '', 'L' * 40,
                                zen_proxies=(('b', 80), ('a', 80), ('new', 80)), state=root) as restored:
                after = restored.stats.snapshot()
                self.assertEqual(after['started_at'], original['started_at'])
                for route in original['routes']:
                    self.assertEqual(after['routes'][route], original['routes'][route])
                self.assertEqual(after['routes']['new:80'], b.RouteStats.empty_route())
                self.assertEqual(after['routes']['direct']['inference']['successes'], 1)
                self.assertEqual(after['routes']['a:80']['inference']['transport_errors'], 1)
                self.assertEqual(after['routes']['b:80']['inference']['http_429'], 1)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_invalid_saved_stats_are_not_silently_reset(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'zen-stats.json'
            value = b.RouteStats(path, ['direct']).snapshot()
            value['routes']['direct']['inference']['attempts'] = -1
            raw = b.encode(value)
            b.save_private(path, raw)
            with self.assertRaisesRegex(b.BridgeError, 'refusing to discard saved counters'):
                b.BridgeServer(('127.0.0.1', 0), StaticCatalog(), '', 'L' * 40, state=root)
            self.assertEqual(path.read_bytes(), raw)

    def test_stats_write_failure_is_visible_without_failing_or_replaying_a_model_answer(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'zen-stats.json'
            self.server.stats = b.RouteStats(path, ['direct'])
            with patch.object(b, 'save_private', side_effect=PermissionError('test failure')):
                status, body = self.request()
            self.assertEqual(status, 200)
            self.assertIn(b'BRIDGE_OK', body)
            snapshot = self.get('/health')[1]['zen_stats']
            self.assertEqual(snapshot['persistence_error'], 'PermissionError')
            self.assertEqual(snapshot['routes']['direct']['inference']['successes'], 1)
            self.assertEqual(len(self.calls), 1)
            self.server.stats.record('direct', 'ip_checks', 'attempts')
            saved = b.load_json(path.read_bytes())
            self.assertEqual(saved['routes']['direct']['inference']['successes'], 1)
            self.assertIsNone(self.server.stats.snapshot()['persistence_error'])

    def test_truncated_stream_counts_transport_error_and_provider_error_frame_is_not_success(self):
        self.response = FakeResponse(frame({'choices': [{'delta': {'content': 'partial'}}]}))
        self.assertEqual(self.request()[0], 200)
        self.server.direct_transport_until = 0
        self.response = FakeResponse(frame({'error': {'message': 'provider error'}}))
        self.assertEqual(self.request()[0], 200)
        self.response = FakeResponse(frame({'choices': [{'delta': {}, 'finish_reason': 'error'}]}) + b'data: [DONE]\n\n')
        self.assertEqual(self.request()[0], 200)
        counts = self.get('/health')[1]['zen_stats']['routes']['direct']['inference']
        self.assertEqual(counts, {'attempts': 3, 'successes': 0, 'http_429': 0, 'transport_errors': 1})

    def test_direct_is_preferred_even_when_every_warp_exit_is_cooling(self):
        self.set_proxies(*(('127.0.0.1', p) for p in range(18090, 18098)), direct_available=True)
        self.server.proxy_until = [time.time() + 19000] * 8
        with patch.object(b, 'upstream_connection') as connect:
            status, raw, headers = self.request(with_headers=True)
            self.assertEqual(status, 200)
            self.assertIn(b'BRIDGE_OK', raw)
            self.assertEqual(headers['X-OMP-Zen-Route'], 'direct')
            self.assertNotIn('X-OMP-Zen-Proxy', headers)
            self.assertEqual(len(self.calls), 1)
            connect.assert_not_called()
        health = self.get('/health')[1]
        self.assertEqual(health['zen_routing'], 'direct-first')
        self.assertEqual(health['zen_direct']['cooldown_seconds'], 0)

    def test_direct_quota_replays_the_unchanged_request_to_first_warp(self):
        self.set_proxies(*((f'warp-{i}', 80) for i in range(8)), direct_available=True)
        self.response = FakeResponse(b'{"error":{"type":"FreeUsageLimitError"}}', 429,
                                     {'Retry-After': '19184'})
        def warp_connection(_):
            self.response = FakeResponse(b'data: [DONE]\n\n')
            return self.server.connector()
        with patch.object(b, 'upstream_connection', side_effect=warp_connection) as connect:
            status, _, headers = self.request(with_headers=True)
            self.assertEqual(status, 200)
            self.assertEqual(headers['X-OMP-Zen-Proxy'], '1')
            self.assertEqual(len(self.calls), 2)
            self.assertEqual(self.calls[0]['wire_body'], self.calls[1]['wire_body'])
            self.assertEqual(self.calls[0]['headers'], self.calls[1]['headers'])
            self.assertGreater(self.server.zen_route_quotas['direct'] - time.time(), 19000)
            self.assertEqual(self.request()[0], 200)
            self.assertEqual(connect.call_count, 2)
            self.assertEqual([call.args[0] for call in connect.call_args_list], [('warp-0', 80)] * 2)
            self.assertEqual(set(self.server.zen_route_quotas), {'direct'})
            self.assertFalse(any(self.server.proxy_until))

    def test_direct_and_all_eight_warp_quotas_exhaust_once_in_configured_order(self):
        proxies = tuple((f'warp-{i}', 80) for i in range(8))
        self.set_proxies(*proxies, direct_available=True)
        self.response = FakeResponse(b'{"error":{"type":"FreeUsageLimitError"}}', 429,
                                     {'Retry-After': '19184'})
        def exhausted(_):
            self.response = FakeResponse(b'{"error":{"type":"FreeUsageLimitError"}}', 429,
                                         {'Retry-After': '19184'})
            return self.server.connector()
        with patch.object(b, 'upstream_connection', side_effect=exhausted) as connect:
            status, raw = self.request()
            self.assertEqual(status, 503)
            self.assertIn(b'ZenProxyCooldown', raw)
            self.assertEqual([call.args[0] for call in connect.call_args_list], list(proxies))
            self.assertEqual(len(self.calls), 9)
            self.assertEqual(len({call['wire_body'] for call in self.calls}), 1)
            self.assertEqual(self.request()[0], 503)
            self.assertEqual(connect.call_count, 8)
            self.assertEqual(len(self.calls), 9)
            health = self.get('/health')[1]
            self.assertGreater(health['zen_direct']['quota_cooldown_seconds'], 19000)
            self.assertEqual(sum(p['quota_cooldown_seconds'] > 19000 for p in health['zen_proxies']), 8)
            self.assertNotIn('zen', self.server.lane_cooldowns)

    def test_one_warp_quota_immediately_uses_the_next_healthy_route(self):
        self.set_proxies(('a', 80), ('b', 80))
        def warp_connection(proxy):
            self.response = (FakeResponse(b'{"error":{"type":"FreeUsageLimitError"}}', 429,
                                          {'Retry-After': '19184'}) if proxy == ('a', 80)
                             else FakeResponse(b'data: [DONE]\n\n'))
            return self.server.connector()
        with patch.object(b, 'upstream_connection', side_effect=warp_connection) as connect:
            status, _, headers = self.request(with_headers=True)
            self.assertEqual(status, 200)
            self.assertEqual(headers['X-OMP-Zen-Proxy'], '2')
            self.assertEqual(connect.call_count, 2)
            self.assertEqual(set(self.server.zen_route_quotas), {'a:80'})
            self.assertEqual(self.server.zen_route_quotas.get('direct', 0), 0)
            # Recovery of the original direct transport restores its independent route.
            self.server.direct_transport_until = 0
            self.response = FakeResponse(b'data: [DONE]\n\n')
            self.assertEqual(self.request(with_headers=True)[2]['X-OMP-Zen-Route'], 'direct')

    def test_legacy_quota_migration_does_not_create_a_global_zen_pause(self):
        until = time.time() + 19000
        with tempfile.TemporaryDirectory() as root:
            b.save_private(Path(root) / 'zen-cooldowns.json', b.encode({
                'schema': 1, 'direct_until': until, 'proxies': {'a:80': until, 'b:80': 0}}))
            with b.BridgeServer(('127.0.0.1', 0), StaticCatalog(), '', 'L' * 40,
                               zen_proxies=(('a', 80), ('b', 80)), state=root) as restored:
                self.assertNotIn('zen', restored.lane_cooldowns)
                self.assertEqual(restored.zen_route_quotas, {'direct': until, 'a:80': until, 'b:80': 0})
                self.assertEqual(restored.proxy_until, [0, 0])
                restored.check_model('zen', 'demo-chat-completions', 'chat/completions')

    def test_direct_errors_other_than_free_quota_do_not_replay_to_warp(self):
        for status, kind in [(429, 'RateLimitError'), (403, 'FreeTierError'), (503, 'Unavailable')]:
            with self.subTest(status=status):
                self.server.blocked.clear()
                self.server.cooldowns.clear()
                self.server.zen_route_quotas.clear()
                self.set_proxies(('a', 80), direct_available=True)
                self.response = FakeResponse(b.encode({'error': {'type': kind}}), status)
                before = len(self.calls)
                with patch.object(b, 'upstream_connection') as connect:
                    self.assertEqual(self.request()[0], status)
                    self.assertEqual(len(self.calls), before + 1)
                    connect.assert_not_called()

    def test_lane_stop_between_quota_attempts_prevents_the_next_post(self):
        self.set_proxies(('a', 80), direct_available=True)
        self.response = FakeResponse(b'{"error":{"type":"FreeUsageLimitError"}}', 429)
        outer = self
        class StopsLaneOnClose(self.server.connector):
            def close(self):
                super().close()
                with outer.server.lock:
                    outer.server.blocked['zen'] = 403
        self.server.connector = StopsLaneOnClose
        with patch.object(b, 'upstream_connection') as connect:
            status, raw = self.request()
            self.assertEqual(status, 409)
            self.assertIn(b'UpstreamLaneStopped', raw)
            self.assertEqual(self.request()[0], 409)
            self.assertEqual(len(self.calls), 1)
            connect.assert_not_called()

    def test_quota_failover_rechecks_model_admission_before_the_next_post(self):
        self.set_proxies(('a', 80), direct_available=True)
        self.response = FakeResponse(b'{"error":{"type":"FreeUsageLimitError"}}', 429)
        outer = self
        class RemovesModelOnClose(self.server.connector):
            def close(self):
                super().close()
                outer.server.catalog.data['models'] = []
        self.server.connector = RemovesModelOnClose
        with patch.object(b, 'upstream_connection') as connect:
            status, raw = self.request()
            self.assertEqual(status, 403)
            self.assertIn(b'PaidOrUnknownModelBlocked', raw)
            self.assertEqual(len(self.calls), 1)
            connect.assert_not_called()

    def test_known_quota_and_transport_cooldowns_are_skipped_without_probing(self):
        self.set_proxies(*((f'warp-{i}', 80) for i in range(8)), direct_available=True)
        until = time.time() + 19000
        self.server.zen_route_quotas = {'direct': until, 'warp-1:80': until}
        self.server.proxy_until[0] = until
        with patch.object(b, 'upstream_connection', side_effect=lambda _: self.server.connector()) as connect:
            status, _, headers = self.request(with_headers=True)
            self.assertEqual(status, 200)
            self.assertEqual(headers['X-OMP-Zen-Proxy'], '3')
            self.assertEqual([call.args[0] for call in connect.call_args_list], [('warp-2', 80)])
            self.assertEqual(self.server.zen_route_quotas, {'direct': until, 'warp-1:80': until})

    def test_zen_cooldowns_survive_restart_and_pool_reordering(self):
        self.set_proxies(('a', 80), ('b', 80))
        with tempfile.TemporaryDirectory() as root:
            self.server.cooldown_path = Path(root) / 'zen-cooldowns.json'
            self.server.cool_proxy(0, 19000)
            self.server.zen_route_quotas['direct'] = time.time() + 19000
            self.server.save_zen_cooldowns()
            direct_until = self.server.zen_route_quotas['direct']
            proxy_until = self.server.proxy_until[0]
            with b.BridgeServer(('127.0.0.1', 0), StaticCatalog(), '', 'L' * 40,
                                zen_proxies=(('b', 80), ('a', 80), ('new', 80)), state=root) as restored:
                self.assertEqual(restored.zen_route_quotas['direct'], direct_until)
                self.assertEqual(restored.proxy_until, [0, proxy_until, 0])
                self.assertEqual(restored.select_proxy('zen', 'stable'), 0)
                restored.direct_transport_until = 0
                self.assertEqual(restored.select_proxy('zen', 'stable'), 0)
                restored.zen_route_quotas['direct'] = 0
                self.assertIsNone(restored.select_proxy('zen', 'stable'))
            self.assertEqual(self.server.cooldown_path.stat().st_mode & 0o777, 0o600)
            self.server.cooldown_path = None

    def test_direct_transport_failure_uses_fixed_warp_on_the_next_request(self):
        self.set_proxies(('a', 80), direct_available=True)
        original = self.server.connector
        def failed_direct():
            raise OSError('network unavailable')
        self.server.connector = failed_direct
        with patch.object(b, 'upstream_connection', side_effect=lambda _: original()) as connect:
            self.assertEqual(self.request()[0], 502)
            self.assertGreater(self.server.direct_transport_until, time.time())
            self.assertEqual(connect.call_count, 0)
            self.assertEqual(self.request()[0], 200)
            self.assertEqual(connect.call_count, 1)
            self.assertFalse(self.server.lane_cooldowns)

    def test_pool_order_is_independent_of_session_and_returns_to_recovered_direct(self):
        self.set_proxies(('a', 80), ('b', 80), ('c', 80))
        chosen = {self.server.select_proxy('zen', f'session-{i}') for i in range(50)}
        self.assertEqual(chosen, {0})
        self.server.zen_route_quotas['a:80'] = time.time() + 60
        self.assertEqual(self.server.select_proxy('zen', 'stable'), 1)
        self.assertEqual(self.server.select_proxy('zen', 'different'), 1)
        self.server.zen_route_quotas['b:80'] = time.time() + 60
        self.assertEqual(self.server.select_proxy('zen', 'stable'), 2)
        self.assertIsNone(self.server.select_proxy('go', 'stable'))
        self.server.direct_transport_until = 0
        self.assertIsNone(self.server.select_proxy('zen', 'stable'))

    def test_pool_quota_preserves_full_independent_reset_deadlines(self):
        self.set_proxies(('a', 80), ('b', 80), ('c', 80))
        self.response = FakeResponse(b'{"error":{"type":"FreeUsageLimitError"}}', 429,
                                     {'Retry-After': '19184'})
        def exhausted(_):
            self.response = FakeResponse(b'{"error":{"type":"FreeUsageLimitError"}}', 429,
                                         {'Retry-After': '19184'})
            return self.server.connector()
        with patch.object(b, 'upstream_connection', side_effect=exhausted) as connect:
            status, _, headers = self.request(with_headers=True)
            self.assertEqual(status, 503)
            self.assertEqual(connect.call_count, 3)
            self.assertEqual(headers['X-OMP-Zen-Proxy'], '3')
            self.assertTrue(all(until - time.time() > 19000 for until in self.server.zen_route_quotas.values()))
            self.assertEqual(self.request()[0], 503)
            self.assertEqual(len(self.calls), 3)

    def test_pool_all_transport_cooling_is_local_and_go_stays_direct(self):
        self.set_proxies(('a', 80), ('b', 80), ('c', 80))
        with patch.object(b, 'upstream_connection', side_effect=lambda _: self.server.connector()) as connect:
            for _ in range(3):
                self.response = FakeResponse(b'{"error":{"type":"Unavailable"}}', 503)
                self.assertEqual(self.request()[0], 503)
            status, raw = self.request()
            self.assertEqual(status, 503)
            self.assertIn(b'ZenProxyCooldown', raw)
            self.assertEqual(len({call.args[0] for call in connect.call_args_list}), 3)
            self.assertEqual(len(self.calls), 3)
            health = self.get('/health')[1]
            self.assertTrue(health['zen_proxy'])
            self.assertEqual([p['id'] for p in health['zen_proxies']], [1, 2, 3])
            self.assertTrue(all(p['cooldown_seconds'] > 0 for p in health['zen_proxies']))
            self.server.catalog.data['models'].append(dict(self.server.catalog.data['models'][0], lane='go'))
            self.response = FakeResponse(b'data: [DONE]\n\n')
            self.assertEqual(self.request(path='/go/v1/chat/completions')[0], 200)
            self.assertEqual(connect.call_count, 3)
            self.assertEqual(len(self.calls), 4)
            self.server.proxy_until[1] = 0
            self.response = FakeResponse(b'data: [DONE]\n\n')
            self.assertEqual(self.request()[0], 200)
            self.assertEqual(connect.call_args.args[0], ('b', 80))

    def test_pool_transport_failure_switches_next_request_without_replay(self):
        self.set_proxies(('a', 80), ('b', 80), ('c', 80))
        with patch.object(b, 'upstream_connection', side_effect=[OSError('offline'), self.server.connector()]) as connect:
            self.assertEqual(self.request()[0], 502)
            self.assertEqual(connect.call_count, 1)
            self.assertEqual(self.request()[0], 200)
            self.assertNotEqual(connect.call_args_list[0].args, connect.call_args_list[1].args)
            self.assertEqual(len(self.calls), 1)

    def test_pool_5xx_cools_exit_without_shortening_existing_quota(self):
        self.set_proxies(('a', 80), ('b', 80), ('c', 80))
        with patch.object(b, 'upstream_connection', side_effect=lambda _: self.server.connector()) as connect:
            self.response = FakeResponse(b'{"error":{"message":"Unavailable"}}', 503)
            self.assertEqual(self.request()[0], 503)
            first = self.server.zen_proxies.index(connect.call_args.args[0])
            self.assertGreater(self.server.proxy_until[first] - time.time(), 29)
            self.server.cool_proxy(first, 19000)
            until = self.server.proxy_until[first]
            self.server.cool_proxy(first, 30)
            self.assertEqual(self.server.proxy_until[first], until)
            self.response = FakeResponse(b'data: [DONE]\n\n')
            self.assertEqual(self.request()[0], 200)
            self.assertEqual(self.server.proxy_until[first], until)

    def test_pool_policy_denial_stops_all_exits(self):
        self.set_proxies(('a', 80), ('b', 80), ('c', 80))
        with patch.object(b, 'upstream_connection', side_effect=lambda _: self.server.connector()) as connect:
            self.response = FakeResponse(b'{"error":{"type":"FreeTierError"}}', 403)
            self.assertEqual(self.request()[0], 403)
            self.assertEqual(self.request()[0], 409)
            self.assertEqual(connect.call_count, 1)
            self.assertFalse(any(self.server.proxy_until))

    def test_pool_truncated_sse_is_not_replayed(self):
        self.set_proxies(('a', 80), ('b', 80), ('c', 80))
        with patch.object(b, 'upstream_connection', side_effect=lambda _: self.server.connector()) as connect:
            self.response = FakeResponse(frame({'choices': [{'delta': {'content': 'partial'}}]}))
            status, raw = self.request()
            self.assertEqual(status, 200)
            self.assertIn(b'TruncatedStream', raw)
            self.assertEqual(connect.call_count, 1)
            self.assertEqual(len(self.calls), 1)
            self.response = FakeResponse(b'data: [DONE]\n\n')
            self.assertEqual(self.request()[0], 200)
            self.assertNotEqual(connect.call_args_list[0].args, connect.call_args_list[1].args)

    def test_pool_midstream_reset_cools_exit_without_replay(self):
        self.set_proxies(('a', 80), ('b', 80), ('c', 80))
        first = frame({'choices': [{'delta': {'content': 'partial'}}]})
        class Resetting(FakeResponse):
            def readline(self, limit=-1):
                if self.tell() >= len(first):
                    raise ConnectionResetError('upstream reset during SSE')
                return super().readline(limit)
        with patch.object(b, 'upstream_connection', side_effect=lambda _: self.server.connector()) as connect:
            self.response = Resetting(first)
            status, raw = self.request()
            self.assertEqual(status, 200)
            self.assertIn(b'partial', raw)
            self.assertIn(b'BridgeTransportError', raw)
            self.assertEqual(len(self.calls), 1)
            self.assertEqual(connect.call_count, 1)
            self.response = FakeResponse(b'data: [DONE]\n\n')
            self.assertEqual(self.request()[0], 200)
            self.assertNotEqual(connect.call_args_list[0].args, connect.call_args_list[1].args)

    def test_pool_does_not_bypass_admission(self):
        self.set_proxies(('a', 80), ('b', 80), ('c', 80))
        with patch.object(b, 'upstream_connection') as connect:
            self.assertEqual(self.request({'model': 'paid', 'stream': True, '_omp_free_session': 'test'})[0], 403)
            connect.assert_not_called()
            self.assertFalse(self.server.zen_route_quotas)

    def test_three_real_connect_exits_keep_session_and_fail_over(self):
        with contextlib.ExitStack() as stack:
            nodes = [stack.enter_context(connect_proxy()) for _ in range(3)]
            self.set_proxies(*(proxy.server_address for proxy, _ in nodes))
            stack.enter_context(patch.object(b.ssl, 'create_default_context', return_value=nodes[0][1]))
            first = self.server.select_proxy('zen', b.session_token('http-test'))
            self.assertEqual(self.request()[0], 200)
            self.assertEqual(self.request()[0], 200)
            self.assertEqual([len(proxy.posts) for proxy, _ in nodes], [2 if i == first else 0 for i in range(3)])
            self.server.cool_proxy(first, 60)
            self.assertEqual(self.request()[0], 200)
            second = (first + 1) % 3
            self.assertEqual(len(nodes[second][0].posts), 1)
            self.assertFalse(self.calls)

    def test_direct_then_eight_real_connect_exits_keep_request_and_session_during_quota_failover(self):
        with contextlib.ExitStack() as stack:
            nodes = [stack.enter_context(connect_proxy()) for _ in range(8)]
            self.set_proxies(*(proxy.server_address for proxy, _ in nodes), direct_available=True)
            stack.enter_context(patch.object(b.ssl, 'create_default_context', return_value=nodes[0][1]))
            self.response = FakeResponse(b'{"error":{"type":"FreeUsageLimitError"}}', 429, {'Retry-After': '19184'})
            for proxy, _ in nodes[:-1]:
                proxy.quota = True
            status, raw, headers = self.request(with_headers=True)
            self.assertEqual(status, 200)
            self.assertIn(b'BRIDGE_OK', raw)
            self.assertIn(b'[DONE]', raw)
            self.assertEqual(headers['X-OMP-Zen-Proxy'], '8')
            self.assertEqual([len(proxy.posts) for proxy, _ in nodes], [1] * 8)
            self.assertEqual(len(self.calls), 1)
            for proxy, _ in nodes:
                _, headers, body = proxy.posts[0]
                self.assertEqual(body, self.calls[0]['body'])
                self.assertEqual(headers['x-opencode-session'], self.calls[0]['headers']['x-opencode-session'])
            self.assertEqual(len(self.server.zen_route_quotas), 8)
            self.assertEqual(self.request()[0], 200)
            self.assertEqual([len(proxy.posts) for proxy, _ in nodes], [1] * 7 + [2])
            self.assertEqual(len(self.calls), 1)

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

    def test_connect_proxy_keeps_origin_tls_and_incremental_sse(self):
        with connect_proxy() as (proxy, trusted), patch.object(b.ssl, 'create_default_context', return_value=trusted):
            self.set_proxies(proxy.server_address)
            proxy.release.clear()
            conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
            self.addCleanup(conn.close)
            payload = {'model': 'demo-chat-completions', 'stream': True,
                       'tools': real_tools(), '_omp_free_session': 'connect-test'}
            conn.request('POST', '/zen/v1/chat/completions', b.encode(payload),
                         {'Authorization': 'Bearer ' + 'L' * 40})
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn(b'BRIDGE_OK', response.readline())
            self.assertFalse(proxy.release.is_set())
            proxy.release.set()
            self.assertIn(b'[DONE]', response.read())
            self.assertEqual(len(proxy.connects), 1)
            self.assertTrue(proxy.connects[0][0].startswith(b'CONNECT opencode.ai:443 HTTP/'))
            self.assertNotIn('Authorization', proxy.connects[0][1])
            self.assertEqual(proxy.names, ['opencode.ai'])
            self.assertEqual(len(proxy.posts), 1)
            line, headers, body = proxy.posts[0]
            self.assertTrue(line.startswith(b'POST /inference/openai/v1/chat/completions '))
            self.assertEqual(headers['Host'], 'opencode.ai')
            self.assertEqual(headers['Authorization'], 'Bearer public')
            self.assertEqual(body['tools'], payload['tools'])
            self.assertNotIn('_omp_free_session', body)
            self.assertFalse(self.calls)  # Direct connector was never used.

    def test_connect_denial_does_not_retry_fall_back_or_block_zen(self):
        with connect_proxy(reject=True) as (proxy, _):
            self.set_proxies(proxy.server_address)
            status, raw = self.request()
            self.assertEqual(status, 502)
            self.assertIn(b'BridgeTransportError', raw)
            self.assertEqual(len(proxy.connects), 1)
            self.assertFalse(proxy.posts)
            self.assertFalse(self.calls)
            self.assertFalse(self.server.blocked)

    def test_connect_requires_a_trusted_origin_certificate(self):
        with connect_proxy() as (proxy, _):
            self.set_proxies(proxy.server_address)
            status, raw = self.request()
            self.assertEqual(status, 502)
            self.assertIn(b'SSLCertVerificationError', raw)
            self.assertFalse(proxy.posts)
            self.assertFalse(self.calls)

    def test_unreachable_zen_proxy_leaves_go_direct(self):
        with socket.socket() as unused:
            unused.bind(('127.0.0.1', 0))  # Reserved but not listening: connection refused.
            self.set_proxies(unused.getsockname())
            unused.close()
            self.assertEqual(self.request()[0], 502)
            self.assertFalse(self.calls)
            self.server.catalog.data['models'].append(dict(self.server.catalog.data['models'][0], lane='go'))
            self.assertEqual(self.request(path='/go/v1/chat/completions')[0], 200)
            self.assertEqual(len(self.calls), 1)
            self.assertEqual(self.calls[0]['headers']['Authorization'], 'Bearer test-upstream-key-ONLY-FIXTURE')

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
        self.assertEqual(self.request()[0], 503)
        self.assertEqual(len(self.calls), 1)

    def test_free_quota_429_pauses_the_direct_route_across_models(self):
        # A quota refusal applies to the same direct route, not to all Zen exits.
        self.response = FakeResponse(b'{"type":"error","error":{"type":"FreeUsageLimitError","message":"Rate limit exceeded."}}',
                                     429, {'Retry-After': '19184'})
        status, raw = self.request()
        self.assertEqual(status, 429)
        self.assertIn(b'Resets at', raw)
        status, raw = self.request({'model': 'demo-responses', 'stream': True, 'input': 'x', '_omp_free_session': 'x'},
                                   path='/zen/v1/responses')
        self.assertEqual(status, 503)
        self.assertIn(b'Resets at', raw)
        self.assertIn(b'ZenProxyCooldown', raw)
        self.assertEqual(len(self.calls), 1)

    def test_free_quota_cooldown_honors_retry_after_beyond_an_hour(self):
        self.response = FakeResponse(b'{"error":{"type":"FreeUsageLimitError","message":"Limit"}}', 429, {'Retry-After': '19184'})
        self.request()
        self.assertGreater(self.server.zen_route_quotas['direct'] - time.time(), 19000)

    def test_free_quota_cooldown_leaves_go_lane_alone(self):
        self.server.catalog.data['models'].append(dict(self.server.catalog.data['models'][0], lane='go'))
        self.response = FakeResponse(b'{"error":{"type":"FreeUsageLimitError","message":"Limit"}}', 429, {'Retry-After': '600'})
        self.request()
        self.response = FakeResponse(frame({'choices': [{'delta': {'content': 'BRIDGE_OK'}}]}) + b'data: [DONE]\n\n')
        self.assertEqual(self.request(path='/go/v1/chat/completions')[0], 200)
        self.assertEqual(len(self.calls), 2)

    def test_other_429_keeps_the_direct_route_quota_without_poisoning_warp(self):
        self.set_proxies(('a', 80), direct_available=True)
        self.response = FakeResponse(b'{"error":{"type":"RateLimitError","message":"Slow down"}}', 429,
                                     {'Retry-After': '90'})
        self.assertEqual(self.request()[0], 429)
        self.response = FakeResponse(b'data: [DONE]\n\n')
        with patch.object(b, 'upstream_connection', side_effect=lambda _: self.server.connector()) as connect:
            status, _ = self.request({'model': 'demo-responses', 'stream': True, 'input': 'x', '_omp_free_session': 'x'},
                                     path='/zen/v1/responses')
            self.assertEqual(status, 200)
            self.assertEqual(connect.call_args.args[0], ('a', 80))
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(set(self.server.zen_route_quotas), {'direct'})
        self.assertEqual(self.get('/health')[1]['zen_proxies'][0]['quota_cooldown_seconds'], 0)

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
                if self.tell() >= len(first):
                    outer.release_stream.wait(2)
                    return b''  # A real shutdown can produce EOF without [DONE].
                return super().readline(limit)
        self.response = Gated(first + b'data: [DONE]\n\n')
        self.set_proxies(('a', 80), ('b', 80), ('c', 80))
        proxy_patch = patch.object(b, 'upstream_connection', side_effect=lambda _: self.server.connector())
        proxy_patch.start()
        self.addCleanup(proxy_patch.stop)
        handled = threading.Event()
        fail_patch = patch.object(b.Handler, 'fail', autospec=True, side_effect=lambda *args: handled.set())
        failed = fail_patch.start()
        self.addCleanup(fail_patch.stop)
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=2)
        conn.request('POST', '/zen/v1/chat/completions', b.encode({'model': 'demo-chat-completions', 'stream': True, '_omp_free_session': 's'}),
                     {'Authorization': 'Bearer ' + 'L' * 40})
        response = conn.getresponse()
        response.readline()
        response.close()
        conn.close()
        self.assertTrue(self.closed.wait(1.5), 'upstream was not cancelled after client disconnect')
        self.assertTrue(handled.wait(1.5), 'EOF after shutdown must be handled')
        self.assertEqual(failed.call_args.args[1].code, 'TruncatedStream')
        self.assertFalse(any(self.server.proxy_until), 'client cancellation must not cool an exit')
        counts = self.server.stats.snapshot()['routes']['a:80']['inference']
        self.assertEqual(counts['successes'], 0)
        self.assertEqual(counts['transport_errors'], 0)

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
        self.set_proxies(('a', 80), ('b', 80), ('c', 80))
        proxy_patch = patch.object(b, 'upstream_connection', side_effect=lambda _: self.server.connector())
        proxy_mock = proxy_patch.start()
        self.addCleanup(proxy_patch.stop)
        with ThreadPoolExecutor(max_workers=count) as pool:
            results = list(pool.map(lambda _: self.request(), range(count)))
        self.assertEqual(len(calls), count)
        self.assertEqual([status for status, _ in results], [200] * count)
        self.assertEqual({call.args[0] for call in proxy_mock.call_args_list}, {('a', 80)})
        counts = self.get('/health')[1]['zen_stats']['routes']['a:80']['inference']
        self.assertEqual(counts['attempts'], count)
        self.assertEqual(counts['successes'], count)
        self.assertTrue(all(b'LocalConcurrencyLimit' not in body for _, body in results))

    def test_no_upstream_secret_in_errors(self):
        self.response = FakeResponse(b'{"error":{"message":"test-upstream-key-ONLY-FIXTURE rejected"}}', 401)
        raw = self.request()[1]
        self.assertNotIn(b'test-upstream-key-ONLY-FIXTURE', raw)
        self.assertIn(b'REDACTED', raw)


if __name__ == '__main__':
    unittest.main(verbosity=2)
