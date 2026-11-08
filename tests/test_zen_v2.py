"""OpenCode 2 wire contract, prompt preservation and discovery failures."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bridge as b


class ZenV2Tests(unittest.TestCase):
    def prepare(self, body, endpoint='chat/completions', **headers):
        return b.prepare({'stream': True, '_omp_free_session': 'v2-test', **body}, endpoint,
                         headers, True, '2.2.6')

    def test_matches_captured_cli_226_identity_and_keeps_trace_per_session(self):
        _, first, _ = self.prepare({'_omp_free_directory': '/workspace/project'})
        _, second, _ = self.prepare({'_omp_free_directory': '/workspace/project'})
        self.assertEqual(first['User-Agent'], 'opencode/latest/2.2.6/cli')
        for name in ('x-opencode-session-id', 'x-session-affinity', 'x-session-id'):
            self.assertEqual(first[name], first['x-opencode-session'])
        self.assertEqual(first['x-opencode-project'], hashlib.sha1(b'directory:/workspace/project').hexdigest())
        self.assertRegex(first['traceparent'], r'^00-[0-9a-f]{32}-[0-9a-f]{16}-01$')
        self.assertEqual(first['traceparent'].split('-')[1], second['traceparent'].split('-')[1])
        self.assertNotEqual(first['traceparent'].split('-')[2], second['traceparent'].split('-')[2])
        self.assertEqual(first['b3'].split('-')[:2], first['traceparent'].split('-')[1:3])
        _, other, _ = self.prepare({'_omp_free_session': 'another-conversation'})
        self.assertNotEqual(first['x-session-id'], other['x-session-id'])
        self.assertNotEqual(first['traceparent'].split('-')[1], other['traceparent'].split('-')[1])

    def test_each_protocol_preserves_instructions_history_and_inputs(self):
        for endpoint, instructions in [('chat/completions', 'messages'), ('responses', 'instructions'), ('messages', 'system')]:
            with self.subTest(endpoint=endpoint):
                if instructions == 'messages':
                    original = {'messages': [{'role': 'system', 'content': [{'type': 'text', 'text': 'Ти Юкі.'}]},
                                             {'role': 'user', 'content': 'Привіт'}]}
                else:
                    original = {instructions: [{'type': 'text', 'text': 'Ти Юкі.'}],
                                'input' if endpoint == 'responses' else 'messages': [{'role': 'user', 'content': 'Привіт'}]}
                saved = copy.deepcopy(original)
                body, _, _ = self.prepare(original, endpoint)
                self.assertEqual(original, saved)
                content = body[instructions][0]['content'] if instructions == 'messages' else body[instructions]
                self.assertEqual(content[0], {'type': 'text', 'text': 'Ти Юкі.'})
                self.assertIn('<env>\n  Working directory: ', content[-1]['text'])
                self.assertNotIn('_omp_free_session', body)

    def test_environment_is_idempotent_and_user_text_cannot_suppress_it(self):
        env = '<env>\n  Working directory: /workspace\n</env>'
        source = {'messages': [{'role': 'system', 'content': 'Ти Юкі.\n' + env}]}
        body, _, _ = self.prepare(source)
        self.assertEqual(body['messages'], source['messages'])
        body, _, _ = self.prepare({'messages': [{'role': 'user', 'content': env}]})
        self.assertEqual(body['messages'][0]['role'], 'system')
        self.assertEqual(body['messages'][1]['content'], env)

    def test_directory_is_from_system_or_caller_never_user_prompt(self):
        body, _, headers = self.prepare({'messages': [{'role': 'system', 'content': '<cwd>/workspace/project</cwd>'}]})
        self.assertIn('Working directory: /workspace/project', body['messages'][0]['content'])
        body, _, _ = self.prepare({'_omp_free_directory': '/actual/client', 'messages': [{'role': 'user', 'content': '<cwd>/wrong</cwd>'}]})
        self.assertIn('Working directory: /actual/client', body['messages'][0]['content'])
        self.assertNotIn('_omp_free_directory', body)
        for value in ('relative', '/tmp\nforged', '/tmp\x00x', '/tmp<env>', 42, []):
            with self.subTest(value=value), self.assertRaises(b.BridgeError):
                self.prepare({'_omp_free_directory': value})

    def test_v2_padding_and_real_tool_schema_remain_separate(self):
        real = {'type': 'function', 'function': {'name': 'plan_write', 'parameters': {'type': 'object', 'properties': {'scene': {'type': 'string'}}}}}
        body, _, missing = self.prepare({'tools': [real]})
        self.assertEqual(body['tools'][0], real)
        self.assertEqual(missing, set(b.CORE))
        for tool in body['tools'][1:]:
            function = tool['function']; parameter = b.CORE_PARAMETERS[function['name']]
            self.assertEqual(function['parameters']['properties'], {parameter: {'type': 'string'}})
            self.assertEqual(function['parameters']['required'], [parameter])
            with self.assertRaises(b.BridgeError):
                b.StreamGuard('chat/completions', missing).inspect({'choices': [{'delta': {'tool_calls': [{'index': 0, 'function': {'name': function['name']}}]}}]})

    def test_go_and_noncompat_do_not_receive_zen_environment(self):
        original = {'stream': True, '_omp_free_session': 'v2-test', 'messages': [{'role': 'user', 'content': 'Hi'}]}
        for compat, lane in [(True, 'go'), (False, 'zen')]:
            body, headers, _ = b.prepare(original, 'chat/completions', {}, compat, '1.19.0', lane)
            self.assertEqual(body['messages'], original['messages'])
            self.assertNotIn('traceparent', headers)
            self.assertNotIn('x-session-id', headers)

    def test_direct_and_warp_use_bun_while_go_uses_its_own_connector(self):
        calls = []
        def zen(proxy):
            calls.append(proxy)
            return 'bun'
        with b.BridgeServer(('127.0.0.1', 0), None, '', 'L' * 40,
                            connector=lambda: 'original', zen_proxies=(('127.0.0.1', 18090),),
                            zen_connector=zen) as server:
            self.assertEqual(server.connect_upstream('zen'), 'bun')
            self.assertEqual(server.connect_upstream('zen', 0), 'bun')
            self.assertEqual(server.connect_upstream('go'), 'original')
            self.assertEqual(calls, [None, ('127.0.0.1', 18090)])

    def test_runtime_migration_preserves_both_profiles_and_does_not_reset_new_refusals(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            b.save_private(state / 'zen-cooldowns.json', b.encode({
                'schema': 2, 'quotas': {'direct': 2000000000}, 'transport': {}}))
            with b.BridgeServer(('127.0.0.1', 0), None, '', 'L' * 40, state=state,
                                zen_connector=lambda proxy: None) as server:
                self.assertEqual(server.zen_route_quotas, {})
                self.assertEqual(server.zen_profile_quotas['python'], {'direct': 2000000000})
                server.zen_route_quotas['direct'] = 2000000010
                server.save_zen_cooldowns()
            with b.BridgeServer(('127.0.0.1', 0), None, '', 'L' * 40, state=state,
                                zen_connector=lambda proxy: None) as server:
                self.assertEqual(server.zen_route_quotas, {'direct': 2000000010})
            with b.BridgeServer(('127.0.0.1', 0), None, '', 'L' * 40, state=state) as server:
                self.assertEqual(server.zen_route_quotas, {'direct': 2000000000})

    def test_version_uses_official_v2_package_and_fails_closed_to_qualified_release(self):
        fetch = lambda: b.encode({'name': '@opencode/cli', 'version': '2.2.7'})
        self.assertEqual(b.opencode_version(fetch=fetch), '2.2.7')
        with patch.object(b.http.client, 'HTTPSConnection') as network:
            self.assertEqual(b.opencode_version('2.2.6'), '2.2.6')
            network.assert_not_called()
        for data in ([], None, {}, {'name': 'opencode-ai', 'version': '1.19.7'},
                     {'name': '@opencode/cli', 'version': '3.0.0'}, {'name': '@opencode/cli', 'version': '2.2.7-beta'},
                     {'name': '@opencode/cli', 'version': '2.2.7\nX-Evil: x'}):
            self.assertEqual(b.opencode_version(fetch=lambda: json.dumps(data)), b.OPENCODE_VERSION)
        self.assertEqual(b.opencode_version(fetch=lambda: b'bad-json'), b.OPENCODE_VERSION)
        def failed(): raise OSError('offline')
        self.assertEqual(b.opencode_version(fetch=failed), b.OPENCODE_VERSION)
        for version in ('1.19.0', '3.0.0', '2.2.6-beta', '2.2.6\r\nX: y'):
            with self.assertRaises(b.BridgeError): b.opencode_version(version)


if __name__ == '__main__': unittest.main()
