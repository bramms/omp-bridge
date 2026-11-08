"""Opt-in live qualification: anonymous free Zen, isolated bridge, real tool loop.

Usage: python3 tests/live_zen.py --bun-bin /path/to/qualified/bun
Not discovered by unittest. Requires network and consumes free allowance.
"""
import argparse
import datetime
import http.client
import json
from pathlib import Path
import secrets
import signal
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bridge as b
from zen_transport import BunTransport


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bun-bin')
    args = parser.parse_args()
    def interrupted(signum, _):
        raise SystemExit(128 + signum)
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, interrupted)
    with tempfile.TemporaryDirectory(prefix='omp-zen-live-', dir='/tmp') as folder:
        root = Path(folder)
        catalog = b.Catalog(state=root / 'catalog')
        snapshot = catalog.get()
        print(json.dumps({'checked_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                          'catalog': [(r['id'], r['endpoint']) for r in snapshot['models'] if r['lane'] == 'zen']}), flush=True)
        token = secrets.token_urlsafe(32)
        with BunTransport(args.bun_bin, b.STATE) as transport:
            server = b.BridgeServer(('127.0.0.1', 0), catalog, '', token, True, '2.2.6',
                                    state=root / 'bridge', zen_connector=transport.connection)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            def call(model, messages, tools=None):
                payload = {'model': model, 'messages': messages, 'stream': True, 'stream_options': {'include_usage': True},
                           'max_tokens': 1024, '_omp_free_session': 'qualification-' + model, '_omp_free_directory': folder}
                if tools:
                    payload['tools'] = tools
                conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=60)
                try:
                    conn.request('POST', '/zen/v1/chat/completions', b.encode(payload),
                                 {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
                    response = conn.getresponse()
                    record = {'model': model, 'http': response.status, 'text': '', 'terminal': False, 'tools': {}, 'cost': None}
                    if response.status != 200:
                        record['error'] = b.load_json(response.read(16384))
                        return record
                    for _, data in b.sse_events(response):
                        if data == b'[DONE]':
                            record['terminal'] = True
                            continue
                        if not data:
                            continue
                        event = b.load_json(data)
                        if event.get('error'):
                            record['error'] = event['error']
                        if event.get('usage'):
                            record['cost'] = event['usage'].get('cost')
                        for choice in event.get('choices', []):
                            delta = choice.get('delta') or {}
                            record['text'] += delta.get('content') or ''
                            for tool in delta.get('tool_calls') or []:
                                item = record['tools'].setdefault(tool.get('index', 0),
                                    {'id': '', 'type': 'function', 'function': {'name': '', 'arguments': ''}})
                                item['id'] += tool.get('id') or ''
                                function = tool.get('function') or {}
                                for key in ('name', 'arguments'):
                                    item['function'][key] += function.get(key) or ''
                    return record
                finally:
                    conn.close()
            try:
                for model in ['deepseek-v4.1-flash-free', 'mimo-v2.6-flash-free', 'glm-5.3-flash-free']:
                    result = call(model, [{'role': 'user', 'content': 'Reply exactly BRIDGE_OK. Do not call tools.'}])
                    print(json.dumps({'live': result}, ensure_ascii=False), flush=True)
                    if model == 'deepseek-v4.1-flash-free':
                        assert result['http'] == 200 and result['text'].strip() == 'BRIDGE_OK' and result['terminal'] and not result.get('error')
                messages = [{'role': 'user', 'content': 'Call bridge_echo once with value BRIDGE_TOOL_OK. After receiving the tool result reply with that result only. Do not call any other tool.'}]
                tools = [{'type': 'function', 'function': {'name': 'bridge_echo', 'description': 'Returns the supplied value without any external action.',
                    'parameters': {'type': 'object', 'properties': {'value': {'type': 'string'}}, 'required': ['value'], 'additionalProperties': False}}}]
                first = call('deepseek-v4.1-flash-free', messages, tools)
                print(json.dumps({'tool_round_1': first}, ensure_ascii=False), flush=True)
                calls = list(first['tools'].values())
                assert len(calls) == 1 and calls[0]['function']['name'] == 'bridge_echo' and first['terminal']
                value = json.loads(calls[0]['function']['arguments'])
                assert value == {'value': 'BRIDGE_TOOL_OK'}
                messages += [{'role': 'assistant', 'content': first['text'] or None, 'tool_calls': calls},
                             {'role': 'tool', 'tool_call_id': calls[0]['id'], 'content': value['value']}]
                second = call('deepseek-v4.1-flash-free', messages, tools)
                print(json.dumps({'tool_round_2': second}, ensure_ascii=False), flush=True)
                assert second['terminal'] and second['text'].strip() == 'BRIDGE_TOOL_OK' and not second.get('error')
            finally:
                server.shutdown()
                server.server_close()
                worker.join()


if __name__ == '__main__':
    main()
