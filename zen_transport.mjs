/** Private persistent Bun fetch transport. Python owns admission and SSE guards. */
import http from 'node:http';
import { once } from 'node:events';
import { timingSafeEqual } from 'node:crypto';

export function createTransportServer(secret, upstreamFetch = fetch) {
if (typeof secret !== 'string' || secret.length < 32) throw new Error('Invalid local token');
const token = Buffer.from(`Bearer ${secret}`);
const active = new Set();
const paths = new Set(['/inference/openai/v1/chat/completions', '/inference/openai/v1/responses', '/inference/anthropic/v1/messages']);
const server = http.createServer(async (req, res) => {
  const given = Buffer.from(req.headers.authorization || '');
  if (req.method !== 'POST' || req.url !== '/request' || req.headers.origin
      || given.length !== token.length || !timingSafeEqual(given, token)
      || req.headers.host !== `127.0.0.1:${server.address().port}`) {
    res.writeHead(403).end(); return;
  }
  const controller = new AbortController();
  active.add(controller);
  let timer = setTimeout(() => controller.abort(), 30_000);
  const abort = () => controller.abort();
  req.on('aborted', abort);
  res.on('close', () => { if (!res.writableFinished) abort(); });
  try {
    let size = 0; const chunks = [];
    for await (const chunk of req) {
      size += chunk.length;
      if (size > 64 * 1024 * 1024) throw new Error('Body too large');
      chunks.push(chunk);
    }
    const value = JSON.parse(Buffer.concat(chunks).toString());
    const headers = new Headers(value.headers);
    if (!paths.has(value.path) || typeof value.body !== 'string'
        || headers.get('authorization') !== 'Bearer public') throw new Error('Invalid upstream request');
    if (value.proxy !== null) {
      const proxy = new URL(value.proxy);
      if (proxy.protocol !== 'http:' || proxy.username || proxy.password
          || proxy.pathname !== '/' || proxy.search || proxy.hash) throw new Error('Invalid proxy');
    }
    // The private transport token never becomes an upstream header. Redirects
    // and POST replays are not allowed; no inherited proxy environment is used.
    const upstream = await upstreamFetch(`https://opencode.ai${value.path}`, {
      method: 'POST', headers, body: value.body, redirect: 'error',
      proxy: value.proxy || undefined, signal: controller.signal, keepalive: false,
    });
    clearTimeout(timer);
    const output = {};
    for (const name of ['content-type', 'retry-after', 'cache-control']) {
      if (upstream.headers.has(name)) output[name] = upstream.headers.get(name);
    }
    res.writeHead(upstream.status, output);
    timer = setTimeout(abort, 120_000);
    if (upstream.body) {
      for await (const chunk of upstream.body) {
        clearTimeout(timer); timer = setTimeout(abort, 120_000);
        if (!res.write(chunk)) await once(res, 'drain', { signal: controller.signal });
      }
    }
    res.end();
  } catch {
    if (!res.headersSent && !res.destroyed) {
      res.writeHead(502, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ error: { code: 'BunTransportError', message: 'Zen transport failed; no automatic replay.' } }));
    } else res.destroy();
  } finally {
    clearTimeout(timer); active.delete(controller); controller.abort();
  }
});
return server;
}

if (import.meta.main) {
  const input = JSON.parse(await Bun.stdin.text());
  const server = createTransportServer(input.token);
  server.listen(0, '127.0.0.1', () => console.log(JSON.stringify({ port: server.address().port, bun: Bun.version })));
  for (const signal of ['SIGTERM', 'SIGINT', 'SIGHUP']) process.on(signal, () => {
    server.closeAllConnections();
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(0), 1000).unref();
  });
}
