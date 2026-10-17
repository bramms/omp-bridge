/** OMP API contract tests using a local mock, not a real OMP session. */
import test, { beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, rm, chmod } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import extension from '../extension.mjs';

let home, originalFetch, previousHome, snapshot, requests, providers, handlers, commands, notices, pi;
const token = 'test-local-token-'.repeat(3);
function row(id, api = 'openai-responses', lane = 'zen') {
  return { id, name: id, api, lane, endpoint: api === 'systemone' ? 'systemone' : 'responses', reasoning: true,
    input: ['text', 'image'], contextWindow: 100000, maxTokens: 8192,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } };
}
function ctx(extra = {}) {
  return { model: { provider: 'opencode-zen-free' }, sessionManager: { getSessionId: () => 'native-session-123' },
    ui: { notify: (...args) => notices.push(args) }, ...extra };
}
beforeEach(async () => {
  home = await mkdtemp(join(tmpdir(), 'omp-free-tests-'));
  previousHome = process.env.OMP_FREE_HOME;
  process.env.OMP_FREE_HOME = home;
  await writeFile(join(home, 'local.token'), token, { mode: 0o600 });
  await writeFile(join(home, 'runtime.json'), JSON.stringify({ port: 8765 }), { mode: 0o600 });
  snapshot = { schema: 1, checked_at: Date.now() / 1000, expires_at: Date.now() / 1000 + 900,
    models: [row('muse-free'), row('chat-free', 'openai-completions'), row('jev-free', 'systemone')], excluded: [] };
  requests = []; providers = new Map(); handlers = new Map(); commands = new Map(); notices = [];
  originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    requests.push({ url, init });
    assert.ok(url.startsWith('http://127.0.0.1:8765/'));
    assert.equal(init.headers.Authorization, `Bearer ${token}`);
    assert.equal(init.redirect, 'error');
    return Response.json(url.endsWith('/catalog') ? snapshot : url.endsWith('/health')
      ? { ok: true, compat_opencode: true, blocked_lanes: [] } : { passed: true });
  };
  pi = { registerProvider: (name, config) => providers.set(name, config),
    unregisterProvider: name => providers.delete(name), on: (event, fn) => handlers.set(event, fn),
    registerCommand: (name, config) => commands.set(name, config) };
});
afterEach(async () => {
  globalThis.fetch = originalFetch;
  if (previousHome === undefined) delete process.env.OMP_FREE_HOME; else process.env.OMP_FREE_HOME = previousHome;
  await rm(home, { recursive: true, force: true });
});

test('registers native providers with per-model protocol; excludes unsupported APIs', async () => {
  await extension(pi);
  assert.deepEqual([...providers.keys()], ['opencode-zen-free']); // No Go rows, no go provider.
  const p = providers.get('opencode-zen-free');
  assert.equal(p.baseUrl, 'http://127.0.0.1:8765/zen/v1');
  assert.equal(p.apiKey, token);
  assert.equal(p.models.length, 2);
  assert.deepEqual(p.models.map(m => m.api), ['openai-responses', 'openai-completions']);
  assert.ok(p.headers['x-omp-free-session']);
  assert.ok(!providers.has('opencode-free')); // The unified alias was removed in 1.3.0.
  assert.ok(!providers.has('opencode-zen')); // Never overwrites the original provider.
});

test('payload hook returns replacement directly and preserves real tools', async () => {
  await extension(pi);
  const payload = { model: 'muse-free', tools: [{ name: 'edit' }], input: [] };
  const result = handlers.get('before_provider_request')({ payload }, ctx());
  assert.equal(result._omp_free_session, 'native-session-123');
  assert.deepEqual(result.tools, payload.tools);
  assert.equal(result.payload, undefined);
  assert.equal(payload._omp_free_session, undefined);
});

test('requests on another provider are untouched', async () => {
  await extension(pi);
  for (const model of [{ provider: 'unrelated-provider' }, { provider: 'opencode-free' }, undefined]) {
    const result = handlers.get('before_provider_request')({ payload: { model: 'other-model' } }, ctx({ model }));
    assert.equal(result, undefined);
  }
});

test('an openai-responses request gets the session ID without any payload marker', async () => {
  await extension(pi);
  const p = providers.get('opencode-zen-free');
  const responsesModel = p.models.find(m => m.api === 'openai-responses');
  // OMP 18.2.0 merges compat.extraBody for openai-completions only, so scoping by a
  // marker field silently skipped every Responses call.
  assert.equal(responsesModel.compat.extraBody, undefined);
  assert.ok(!JSON.stringify(p.models).includes('_omp_free_bridge'));
  const result = handlers.get('before_provider_request')({ payload: { model: 'muse-free' } }, ctx());
  assert.equal(result._omp_free_session, 'native-session-123');
});

test('session_start re-reads the catalog instead of re-registering an expired snapshot', async () => {
  await extension(pi);
  // The proxy TTL is 15 minutes; a session opened later must not throw "expired catalog".
  snapshot.checked_at = Date.now() / 1000 - 1000;
  snapshot.expires_at = Date.now() / 1000 - 100;
  const fresh = { ...snapshot, checked_at: Date.now() / 1000, expires_at: Date.now() / 1000 + 900 };
  const previous = globalThis.fetch;
  globalThis.fetch = async (url, init) => (url.endsWith('/catalog')
    ? Response.json(fresh) : previous(url, init));
  await handlers.get('session_start')({}, ctx());
  globalThis.fetch = previous;
  assert.equal(providers.get('opencode-zen-free').models.length, 2);
});

test('refresh removes disappeared providers and does not affect existing providers', async () => {
  await extension(pi);
  providers.set('unrelated', { keep: true });
  snapshot.models = [];
  await commands.get('free-refresh').handler('', ctx());
  assert.ok(!providers.has('opencode-zen-free'));
  assert.deepEqual(providers.get('unrelated'), { keep: true });
});

test('free Go route has its own provider and no unified alias', async () => {
  snapshot.models = [row('hypothetical-verified-zero-cost-model', 'openai-responses', 'go')];
  await extension(pi);
  assert.equal(providers.get('opencode-go-free').baseUrl, 'http://127.0.0.1:8765/go/v1');
  assert.deepEqual(providers.get('opencode-go-free').models.map(m => m.id), ['hypothetical-verified-zero-cost-model']);
  assert.deepEqual([...providers.keys()], ['opencode-go-free']);
});

test('invalid or nonzero catalog is rejected', async () => {
  snapshot.models[0].cost.input = 0.1;
  await assert.rejects(() => extension(pi), /ненульовий/);
});

test('expired catalog is rejected', async () => {
  snapshot.expires_at = 0;
  await assert.rejects(() => extension(pi), /прострочений/);
});

test('credential permissions are checked', async () => {
  if (process.platform === 'win32') return;
  await chmod(join(home, 'local.token'), 0o644);
  await assert.rejects(() => extension(pi), /0600/);
});

test('non-loopback runtime port injection is rejected before HTTP', async () => {
  await writeFile(join(home, 'runtime.json'), JSON.stringify({ port: '8765@evil.invalid' }));
  await assert.rejects(() => extension(pi), /порт/);
  assert.equal(requests.length, 0);
});

test('bridge outage is surfaced rather than silently selecting another model', async () => {
  globalThis.fetch = async () => { throw new Error('ECONNREFUSED'); };
  await assert.rejects(() => extension(pi), /ECONNREFUSED/);
  assert.equal(providers.size, 0);
});


test('only the Zen and Go providers exist; an ID on both lanes stays two models', async () => {
  snapshot.models = [row('same-free', 'openai-responses', 'zen'), row('same-free', 'openai-completions', 'go')];
  await extension(pi);
  assert.deepEqual([...providers.keys()], ['opencode-zen-free', 'opencode-go-free']);
  assert.deepEqual([...providers.values()].map(p => p.baseUrl),
    ['http://127.0.0.1:8765/zen/v1', 'http://127.0.0.1:8765/go/v1']);
  assert.equal(providers.get('opencode-zen-free').models[0].api, 'openai-responses');
  assert.equal(providers.get('opencode-go-free').models[0].api, 'openai-completions');
});

test('outage preserves cached route identity and loopback destination', async () => {
  snapshot.expires_at = 1;
  await writeFile(join(home, 'catalog.json'), JSON.stringify(snapshot));
  globalThis.fetch = async () => { throw new Error('ECONNREFUSED'); };
  await extension(pi);
  assert.equal(providers.get('opencode-zen-free').baseUrl, 'http://127.0.0.1:8765/zen/v1');
  await handlers.get('session_start')({}, ctx());
  assert.equal(notices.at(-1)[1], 'warning');
  assert.ok(providers.has('opencode-zen-free'));
});


test('chat editions without effort controls never emit unsupported reasoning_effort', async () => {
  snapshot.models = [row('big-pickle', 'openai-completions'),
    {...row('effort-free', 'openai-completions'), efforts: ['low', 'high']}];
  await extension(pi);
  const [plain, effort] = providers.get('opencode-zen-free').models;
  assert.equal(plain.compat.supportsReasoningEffort, false);
  assert.equal(plain.thinking, undefined);
  assert.equal(effort.compat.supportsReasoningEffort, true);
  assert.deepEqual(effort.thinking.efforts, ['low', 'high']);
});
