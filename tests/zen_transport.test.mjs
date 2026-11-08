import test from 'node:test';
import assert from 'node:assert/strict';
import { createTransportServer } from '../zen_transport.mjs';

const token = 'local-test-token-'.repeat(3);
const body = { path: '/inference/openai/v1/chat/completions', headers: { Authorization: 'Bearer public', 'User-Agent': 'opencode/latest/2.2.6/cli' }, body: '{"model":"demo-free","stream":true}', proxy: null };
async function fixture(fn, fetchImpl) {
  const server = createTransportServer(token, fetchImpl);
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const request = (value = body, headers = {}) => fetch(base + '/request', { method: 'POST',
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json', ...headers }, body: JSON.stringify(value) });
  try { await fn(request, base); }
  finally { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); }
}

test('relays SSE incrementally, preserves upstream status and keeps private auth local', async () => {
  let release;
  const later = new Promise(resolve => { release = resolve; });
  await fixture(async request => {
    const response = await request();
    assert.equal(response.status, 200);
    const reader = response.body.getReader();
    const first = await reader.read();
    assert.match(new TextDecoder().decode(first.value), /first/);
    release();
    let rest = ''; while (true) { const next = await reader.read(); if (next.done) break; rest += new TextDecoder().decode(next.value); }
    assert.match(rest, /DONE/);
  }, async (url, options) => {
    assert.equal(url, 'https://opencode.ai/inference/openai/v1/chat/completions');
    assert.equal(options.headers.get('authorization'), 'Bearer public');
    assert.ok(!JSON.stringify(options).includes(token));
    assert.equal(options.body, body.body);
    assert.equal(options.redirect, 'error');
    return new Response(new ReadableStream({ async start(c) {
      c.enqueue(new TextEncoder().encode('data: first\n\n'));
      await later; c.enqueue(new TextEncoder().encode('data: [DONE]\n\n')); c.close();
    } }), { headers: { 'Content-Type': 'text/event-stream' } });
  });
});

test('denied inputs never reach upstream', async () => {
  let calls = 0;
  await fixture(async (request, base) => {
    for (const headers of [{ Authorization: 'Bearer wrong' }, { Origin: 'https://evil.invalid' }])
      assert.equal((await request(body, headers)).status, 403);
    assert.equal((await fetch(base + '/request')).status, 403);
    for (const value of [{ ...body, path: '/zen/go/v1/chat/completions' },
      { ...body, path: 'https://evil.invalid/' }, { ...body, headers: { Authorization: 'Bearer paid-key' } },
      { ...body, proxy: 'http://user:password@localhost:80' }, { ...body, proxy: 'https://localhost:80' }]) {
      const response = await request(value); assert.equal(response.status, 502); await response.text();
    }
    assert.equal(calls, 0);
  }, async () => { calls++; return new Response('unexpected'); });
});

test('preserves 429 and Retry-After without retrying or changing model', async () => {
  let calls = 0;
  await fixture(async request => {
    const response = await request({ ...body, proxy: 'http://127.0.0.1:18090' });
    assert.equal(response.status, 429);
    assert.equal(response.headers.get('retry-after'), '19000');
    assert.match(await response.text(), /FreeUsageLimitError/);
    assert.equal(calls, 1);
  }, async (url, options) => {
    calls++; assert.equal(options.proxy, 'http://127.0.0.1:18090');
    return Response.json({ error: { type: 'FreeUsageLimitError' } }, { status: 429, headers: { 'Retry-After': '19000' } });
  });
});

test('client cancellation aborts the upstream request', async () => {
  let upstreamSignal; let signalAborted;
  const aborted = new Promise(resolve => { signalAborted = resolve; });
  await fixture(async request => {
    const response = await request(); const reader = response.body.getReader();
    await reader.read(); await reader.cancel();
    await Promise.race([aborted, new Promise((_, reject) => setTimeout(() => reject(new Error('upstream not cancelled')), 2000))]);
    assert.equal(upstreamSignal.aborted, true);
  }, async (url, options) => {
    upstreamSignal = options.signal;
    return new Response(new ReadableStream({ start(c) {
      c.enqueue(new TextEncoder().encode('data: start\n\n'));
      options.signal.addEventListener('abort', () => { c.error(new Error('aborted')); signalAborted(); }, { once: true });
    } }), { headers: { 'Content-Type': 'text/event-stream' } });
  });
});
