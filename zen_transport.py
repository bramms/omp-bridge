"""One persistent Bun sidecar, started before serving; never per-request processes."""
import http.client
import json
import os
from pathlib import Path
import secrets
import select
import shutil
import signal
import subprocess

BUN_VERSION = '1.3.14'


class BunConnection:
    def __init__(self, transport, proxy):
        self.transport, self.proxy = transport, proxy
        self.inner = http.client.HTTPConnection('127.0.0.1', transport.port, timeout=120)

    @property
    def sock(self):
        return self.inner.sock

    def request(self, method, path, body, headers):
        if method != 'POST':
            raise ValueError('Zen transport accepts POST only')
        if self.transport.process.poll() is not None:
            raise OSError('Zen Bun transport exited; restart the bridge')
        value = {'path': path, 'headers': headers,
                 'body': body.decode() if isinstance(body, bytes) else body,
                 'proxy': f'http://{self.proxy[0]}:{self.proxy[1]}' if self.proxy else None}
        self.inner.request('POST', '/request', json.dumps(value),
                           {'Authorization': 'Bearer ' + self.transport.token, 'Content-Type': 'application/json'})

    def getresponse(self):
        return self.inner.getresponse()

    def close(self):
        self.inner.close()


class BunTransport:
    def __init__(self, binary=None, state=None):
        installed = Path(state) / 'runtime' / 'bun'
        candidate = binary or (str(installed) if installed.is_file() else shutil.which('bun'))
        if not candidate:
            raise OSError('Install qualified Bun with: python3 deploy/install-zen-runtime.py')
        self.token = secrets.token_urlsafe(32)
        self.port = None
        self._closed = False
        # Existing proxy variables and provider credentials must not enter Bun.
        env = {key: os.environ[key] for key in ('PATH', 'LANG', 'HOME', 'TMPDIR') if key in os.environ}
        self.process = subprocess.Popen([candidate, str(Path(__file__).with_suffix('.mjs'))],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        text=True, env=env, start_new_session=True)
        try:
            self.process.stdin.write(json.dumps({'token': self.token}))
            self.process.stdin.close()
            ready, _, _ = select.select([self.process.stdout], [], [], 10)
            if not ready:
                raise OSError('Zen Bun transport startup timed out')
            data = json.loads(self.process.stdout.readline(4096))
            if (data.get('bun') != BUN_VERSION or type(data.get('port')) is not int
                    or not 1 <= data['port'] <= 65535):
                raise OSError(f'Zen requires qualified Bun {BUN_VERSION}; install with deploy/install-zen-runtime.py')
            self.port = data['port']
        except BaseException:
            self.close()
            raise

    def connection(self, proxy=None):
        return BunConnection(self, proxy)

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait()
        for stream in (self.process.stdout, self.process.stdin):
            if stream is not None and not stream.closed:
                stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
