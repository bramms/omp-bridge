"""Private RPC boundary and runtime lifecycle, independent of provider access."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from zen_transport import BunConnection, BunTransport


class BunTransportTests(unittest.TestCase):
    def test_connection_keeps_auth_on_loopback_and_proxy_inside_rpc(self):
        process = Mock(); process.poll.return_value = None
        transport = SimpleNamespace(port=12345, token='LOCAL_PRIVATE_TOKEN', process=process)
        with patch('zen_transport.http.client.HTTPConnection') as connection:
            conn = BunConnection(transport, ('127.0.0.1', 18090))
            headers = {'Authorization': 'Bearer public', 'User-Agent': 'opencode/latest/2.2.6/cli'}
            conn.request('POST', '/inference/openai/v1/chat/completions', b'{"stream":true}', headers)
            args = connection.return_value.request.call_args.args
            self.assertEqual(args[:2], ('POST', '/request'))
            rpc = json.loads(args[2])
            self.assertEqual(rpc['headers'], headers)
            self.assertEqual(rpc['proxy'], 'http://127.0.0.1:18090')
            self.assertNotIn(transport.token, args[2])
            self.assertEqual(args[3]['Authorization'], 'Bearer ' + transport.token)
            self.assertEqual(conn.getresponse(), connection.return_value.getresponse.return_value)
            conn.close(); connection.return_value.close.assert_called_once()
            with self.assertRaises(ValueError): conn.request('GET', '/', b'', {})
            process.poll.return_value = 1
            with self.assertRaises(OSError): conn.request('POST', '/', b'{}', headers)

    def test_process_is_started_once_token_uses_stdin_and_credentials_do_not_enter_environment(self):
        with tempfile.TemporaryDirectory() as state:
            process = Mock(pid=123456)
            process.stdin = io.StringIO()
            process.stdout = io.StringIO('{"port":12345,"bun":"1.3.14"}\n')
            with patch('zen_transport.subprocess.Popen', return_value=process) as popen, \
                 patch('zen_transport.select.select', return_value=([process.stdout], [], [])), \
                 patch('zen_transport.os.killpg') as kill, \
                 patch.dict(os.environ, {'OPENCODE_API_KEY': 'NEVER_FORWARD', 'HTTPS_PROXY': 'http://wrong:80'}):
                transport = BunTransport('/qualified/bun', state)
                self.assertEqual(transport.port, 12345)
                environment = popen.call_args.kwargs['env']
                self.assertNotIn('OPENCODE_API_KEY', environment)
                self.assertNotIn('HTTPS_PROXY', environment)
                self.assertNotIn(transport.token, str(popen.call_args))
                transport.connection(); transport.connection(('127.0.0.1', 18090))
                popen.assert_called_once()
                transport.close(); transport.close()
                kill.assert_called_once()
                process.wait.assert_called_once()

    def test_wrong_runtime_is_rejected_and_child_is_reaped(self):
        process = Mock(pid=123456)
        process.stdin = io.StringIO(); process.stdout = io.StringIO('{"port":12345,"bun":"1.4.4"}\n')
        with patch('zen_transport.subprocess.Popen', return_value=process), \
             patch('zen_transport.select.select', return_value=([process.stdout], [], [])), \
             patch('zen_transport.os.killpg'), self.assertRaisesRegex(OSError, 'qualified Bun'):
            BunTransport('/wrong/bun', '/tmp')
        process.wait.assert_called_once()


if __name__ == '__main__': unittest.main()
