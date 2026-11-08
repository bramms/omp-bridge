#!/usr/bin/env python3
"""Opt-in Zen control using a verified, isolated official OpenCode 2 binary.

No existing accounts/config/plugins are read. Only Bearer public, one exact free
model, no paid fallback. All tool permissions require approval, stdin is closed,
and --auto is never used. Temporary files and process groups are cleaned up on
success, failure, timeout, INT, TERM and HUP. Run outside GPU/weight directories.
"""
import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import urlopen

VERSION = '2.2.6'
MODEL = 'deepseek-v4.1-flash-free'


def download(url):
    if not url.startswith('https://registry.npmjs.org/'):
        raise ValueError('Unapproved distribution host')
    with urlopen(url, timeout=60) as response:
        return response.read(350_000_001)


def cli_binary(root, bun=False):
    machine = {'x86_64': 'x64', 'arm64': 'arm64', 'aarch64': 'arm64'}[platform.machine()]
    system = {'Linux': 'linux', 'Darwin': 'darwin'}[platform.system()]
    if bun and machine == 'arm64':
        machine = 'aarch64'
    package = f'@oven/bun-{system}-{machine}' if bun else f'@opencode/cli-{system}-{machine}'
    version = '1.3.14' if bun else VERSION
    metadata = json.loads(download(f'https://registry.npmjs.org/{package}/{version}'))
    raw = download(metadata['dist']['tarball'])
    digest = 'sha512-' + base64.b64encode(hashlib.sha512(raw).digest()).decode()
    if len(raw) > 350_000_000 or digest != metadata['dist']['integrity']:
        raise ValueError('Official distribution integrity mismatch')
    name = 'bun' if bun else 'opencode'
    binary = root / name
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
        members = [m for m in archive.getmembers() if m.isfile() and m.name.endswith('/bin/' + name)]
        if len(members) != 1 or members[0].size > 500_000_000:
            raise ValueError('Unexpected binary archive')
        with archive.extractfile(members[0]) as source, binary.open('wb') as target:
            shutil.copyfileobj(source, target)
    binary.chmod(0o700)
    return binary, digest


def terminate(child):
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait()


class Recorder(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.captures.append((self.path, dict(self.headers), body))
        raw = (b'data: {"id":"capture","choices":[{"index":0,"delta":{"content":"BRIDGE_OK"},"finish_reason":null}]}\n\n'
               b'data: {"id":"capture","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'
               b'data: [DONE]\n\n')
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def compare_bridge(capture, directory, bun=None):
    """Same model, exit and minute: native body versus candidate bridge shape."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import bridge
    path, native_headers, native = capture
    variants = []
    raw_headers = {k: v for k, v in native_headers.items()
                   if k.lower() not in {"host", "content-length", "connection", "accept-encoding"}}
    def probe(label, body, headers, missing):
        conn = bridge.upstream_connection()
        conn.timeout = 35
        result = {"comparison": label}
        try:
            conn.request("POST", "/inference/openai/v1/chat/completions", body=bridge.encode(body), headers=headers)
            response = conn.getresponse()
            result["http"] = response.status
            if response.status != 200:
                result["error"] = json.loads(response.read(16384))
            else:
                text = []
                terminal = False
                guard = bridge.StreamGuard("chat/completions", missing)
                for _, data in bridge.sse_events(response):
                    if data == b"[DONE]":
                        terminal = True
                    elif data:
                        event = json.loads(data)
                        guard.inspect(event)
                        for choice in event.get("choices", []):
                            text.append(choice.get("delta", {}).get("content") or "")
                result.update(text="".join(text), terminal=terminal)
        except Exception as exc:
            result["failure"] = type(exc).__name__
        finally:
            conn.close()
        print(json.dumps(result, ensure_ascii=False), flush=True)
    probe("captured-native-body-over-python", native, raw_headers, set())
    variants.append({"label": "captured-native-body-over-bun", "body": native, "headers": raw_headers})
    request = {"model": MODEL, "messages": [{"role": "user", "content": "Reply exactly BRIDGE_OK. Do not call tools."}],
               "stream": True, "stream_options": {"include_usage": True}, "max_tokens": 512,
               "_omp_free_session": "zen-v2-qualification", "_omp_free_directory": directory}
    body, headers, missing = bridge.prepare(request, "chat/completions", {}, True, VERSION)
    headers.update(Authorization="Bearer public", Accept="*/*")
    probe("candidate-bridge", body, headers, missing)
    variants.append({"label": "candidate-bridge-over-bun", "body": json.loads(json.dumps(body)), "headers": headers})
    # Only use the factual environment block for this variant, not the native
    # agent's instructions: importing another agent's persona is not a fix.
    for message in native.get("messages", []):
        text = message.get("content", "")
        if message.get("role") == "system" and isinstance(text, str) and "<env>" in text:
            start = text.index("<env>")
            block = text[start:text.index("</env>", start) + 6]
            body["messages"][0]["content"] = block
            probe("candidate-with-captured-environment", body, headers, missing)
            variants.append({"label": "candidate-environment-over-bun", "body": body, "headers": headers})
            break


    if bun:
        script = Path(directory) / "compare.mjs"
        script.write_text("""const variants = JSON.parse(await Bun.stdin.text());
for (const variant of variants) {
  try {
    const response = await fetch('https://opencode.ai/inference/openai/v1/chat/completions', {
      method: 'POST', headers: variant.headers, body: JSON.stringify(variant.body),
      signal: AbortSignal.timeout(40000), redirect: 'error',
    });
    const raw = await response.text();
    console.log(JSON.stringify({comparison: variant.label, http: response.status, body: raw.slice(0, 5000)}));
  } catch (error) { console.log(JSON.stringify({comparison: variant.label, error: error.name})); }
}
""")
        child = subprocess.Popen([str(bun), str(script)], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
                                 cwd=directory, env={"PATH": "/usr/bin:/bin", "HOME": directory, "TMPDIR": str(Path(directory) / "tmp")})
        try:
            stdout, stderr = child.communicate(json.dumps(variants), timeout=130)
            print(stdout, end="", flush=True)
            if stderr:
                print(json.dumps({"bun_stderr": stderr[-1000:]}), flush=True)
        finally:
            terminate(child)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--proxy', action='append', default=[], help='Existing HTTP proxy; direct is always tested first')
    parser.add_argument('--compare-bridge', action='store_true', help='Also compare the candidate Python transport with the captured native request')
    parser.add_argument('--compare-bun', action='store_true', help='Also qualify verified Bun 1.3.14 in the temporary directory')
    args = parser.parse_args()
    for proxy in args.proxy:
        if not proxy.startswith('http://127.0.0.1:') or not proxy.rsplit(':', 1)[1].isdigit():
            parser.error('Only an existing loopback HTTP proxy is accepted')
    def interrupted(signum, _frame):
        raise SystemExit(128 + signum)
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, interrupted)
    children = []
    with tempfile.TemporaryDirectory(prefix='omp-zen-v2-control-', dir='/tmp') as folder:
        root = Path(folder)
        recorder = ThreadingHTTPServer(('127.0.0.1', 0), Recorder)
        recorder.daemon_threads = True
        recorder.captures = []
        thread = threading.Thread(target=recorder.serve_forever, daemon=True)
        thread.start()
        try:
            binary, digest = cli_binary(root)
            print(json.dumps({'official_cli': VERSION, 'integrity': digest, 'credential': 'public',
                              'existing_accounts': False, 'permissions': 'ask; closed stdin'}), flush=True)
            (root / 'tmp').mkdir()
            base_env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'LANG': 'C.UTF-8',
                        'HOME': folder, 'TMPDIR': str(root / 'tmp'),
                        **{f'XDG_{name.upper()}_HOME': str(root / name) for name in ('config', 'data', 'cache', 'state')},
                        'OPENCODE_DISABLE_AUTOUPDATE': '1', 'OPENCODE_DISABLE_TERMINAL_TITLE': '1'}
            for label, proxy in [('local-contract-only', None), ('direct', None), *[(p, p) for p in args.proxy]]:
                options = {'apiKey': 'public'}
                if label == 'local-contract-only':
                    options['baseURL'] = f'http://127.0.0.1:{recorder.server_port}/v1'
                config = {'permission': 'ask', 'enabled_providers': ['opencode'], 'provider': {'opencode': {'options': options}}}
                (root / 'opencode.json').write_text(json.dumps(config))
                env = {**base_env, 'OPENCODE_CONFIG_CONTENT': json.dumps(config)}
                if proxy:
                    env.update(HTTPS_PROXY=proxy, HTTP_PROXY=proxy)
                child = subprocess.Popen([str(binary), '--standalone', 'run', '-m', f'opencode/{MODEL}',
                                          '--title', 'Zen bridge control', '--format', 'json',
                                          'Reply exactly BRIDGE_OK. Do not call tools.'],
                                         cwd=folder, env=env, stdin=subprocess.DEVNULL,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
                children.append(child)
                try:
                    stdout, stderr = child.communicate(timeout=55)
                except subprocess.TimeoutExpired:
                    terminate(child)
                    stdout, stderr = child.communicate()
                events = []
                for line in stdout.splitlines():
                    try:
                        events.append(json.loads(line))
                    except ValueError:
                        pass
                text = ''.join(e.get('part', {}).get('text', '') for e in events if e.get('type') == 'text')
                errors = [e.get('error') for e in events if e.get('type') == 'error']
                print(json.dumps({'route': label, 'exit': child.returncode, 'text': text,
                                  'model_answer_confirmed': label != 'local-contract-only' and child.returncode == 0
                                      and text.strip() == 'BRIDGE_OK' and not errors,
                                  'errors': errors, 'stderr': stderr[-1000:]}, ensure_ascii=False), flush=True)
                terminate(child)
                if label == 'local-contract-only':
                    for path, headers, body in recorder.captures:
                        tools = [(t.get('function') or t) for t in body.get('tools', [])]
                        print(json.dumps({'contract': {'path': path,
                            'header_names': list(headers), 'user_agent': headers.get('User-Agent'),
                            'body_keys': list(body), 'tool_names': [t.get('name') for t in tools],
                            'parameters': {t.get('name'): t.get('parameters') for t in tools if t.get('name') in ('read', 'shell')},
                            'system_messages': sum(m.get('role') == 'system' for m in body.get('messages', []))}}), flush=True)
            if (args.compare_bridge or args.compare_bun) and recorder.captures:
                bun = cli_binary(root, bun=True)[0] if args.compare_bun else None
                compare_bridge(recorder.captures[0], folder, bun)
        finally:
            for child in children:
                terminate(child)
            recorder.shutdown()
            recorder.server_close()
            thread.join()


if __name__ == '__main__':
    main()
