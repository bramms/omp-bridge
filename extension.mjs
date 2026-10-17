/** Native OMP extension. No OpenCode CLI and no subprocesses.
 * Load explicitly: omp -e /absolute/path/extension.mjs --model opencode-zen-free/<id>
 * The only credential read here is the local proxy token, never a Zen API key.
 */
import { readFile, lstat } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join, resolve } from 'node:path';
import { randomUUID } from 'node:crypto';

// One provider per lane. An ID served by both lanes is two separate models, never merged.
const PROVIDERS = [
  { id: 'opencode-zen-free', lane: 'zen', route: 'zen', label: 'ZEN FREE' },
  { id: 'opencode-go-free', lane: 'go', route: 'go', label: 'GO FREE' },
];
const APIS = new Set(['openai-completions', 'openai-responses', 'anthropic-messages']);

export default async function opencodeFreeNative(pi) {
  const configured = process.env.OMP_FREE_HOME;
  const home = configured
    ? resolve(configured.replace(/^~(?=\/|$)/, homedir()))
    : join(homedir(), '.config', 'omp-free-bridge');
  const tokenPath = join(home, 'local.token');
  const stat = await lstat(tokenPath).catch(() => {
    throw new Error('Спочатку запустіть: python3 bridge.py serve --compat-opencode');
  });
  if (stat.isSymbolicLink() || (process.platform !== 'win32' && (stat.mode & 0o077))) {
    throw new Error('local.token: потрібен звичайний файл із правами 0600.');
  }
  const token = (await readFile(tokenPath, 'utf8')).trim();
  const runtime = JSON.parse(await readFile(join(home, 'runtime.json'), 'utf8'));
  if (token.length < 32 || !Number.isInteger(runtime.port) || runtime.port < 1 || runtime.port > 65535) {
    throw new Error('Некоректний локальний токен або порт проксі.');
  }
  const base = `http://127.0.0.1:${runtime.port}`;
  // Stable fallback for helper calls that do not run the session payload hook.
  const helperSession = `omp-extension-${randomUUID()}`;
  let catalog;
  const registered = new Set();

  async function request(path, payload, signal) {
    // GETs run inside hooks with OMP's 30 s extension-handler budget: fail before it,
    // so the error names the bridge instead of a killed handler.
    const timeout = AbortSignal.timeout(payload ? 120_000 : 20_000);
    const response = await fetch(base + path, {
      method: payload ? 'POST' : 'GET',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      body: payload ? JSON.stringify(payload) : undefined,
      signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
      redirect: 'error',
    });
    const value = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(`Free bridge HTTP ${response.status}: ${value.error?.message ?? 'Запит відхилено'}`);
    }
    return value;
  }

  function register(snapshot, allowExpired = false) {
    if (snapshot?.schema !== 1 || !Array.isArray(snapshot.models)
      || !Number.isFinite(snapshot.expires_at) || (!allowExpired && snapshot.expires_at * 1000 <= Date.now())) {
      throw new Error('Проксі повернув некоректний або прострочений каталог.');
    }
    for (const provider of PROVIDERS) {
      const rows = [...new Map(snapshot.models.filter(m => m.lane === provider.lane && APIS.has(m.api))
        .map(m => [m.id, m])).values()];
      const models = rows.map(row => {
        if (typeof row.id !== 'string' || typeof row.name !== 'string'
          || !Number.isFinite(row.contextWindow) || row.contextWindow <= 0
          || !Number.isFinite(row.maxTokens) || row.maxTokens <= 0
          || !Array.isArray(row.input) || row.input.some(x => !['text', 'image'].includes(x))
          || !row.cost || ['input', 'output', 'cacheRead', 'cacheWrite'].some(k => row.cost[k] !== 0)) {
          throw new Error('Неповний або ненульовий запис у free-only каталозі.');
        }
        // Using the exact upstream ID keeps OMP's own model-family detection: it merges
        // the bundled same-ID compatConfig and resolves thinking levels on its own.
        // No private marker field: OMP 18.2.0 applies compat.extraBody to openai-completions
        // only, so before_provider_request scopes by this extension's own provider instead.
        const compat = row.api === 'openai-completions'
          ? { supportsStore: false, supportsDeveloperRole: false, maxTokensField: 'max_tokens',
              supportsReasoningEffort: (row.efforts?.length ?? 0) > 0 }
          : {};
        return {
          id: row.id, name: `[${provider.label}] ${row.name}`, api: row.api,
          reasoning: row.reasoning === true, input: row.input,
          ...(row.efforts?.length ? { thinking: { mode: 'effort', efforts: row.efforts } } : {}),
          cost: row.cost, contextWindow: row.contextWindow, maxTokens: row.maxTokens,
          compat,
        };
      });
      if (models.length) {
        pi.registerProvider(provider.id, { baseUrl: `${base}/${provider.route}/v1`, apiKey: token,
          authHeader: true, headers: { 'x-omp-free-session': helperSession }, models });
        registered.add(provider.id);
      } else if (registered.has(provider.id)) {
        pi.unregisterProvider(provider.id);
        registered.delete(provider.id);
      }
    }
    catalog = snapshot;
  }
  let initial, offline = false;
  try {
    initial = await request('/catalog');
  } catch (error) {
    // Keep exact routes registered during an outage, pointing only at loopback.
    // A stale cache is naming information; the bridge revalidates before inference.
    const raw = await readFile(join(home, 'catalog.json'), 'utf8').catch(() => { throw error; });
    initial = JSON.parse(raw);
    offline = true;
  }
  register(initial, offline);

  pi.on('session_start', async (_event, ctx) => {
    // The snapshot expires after the proxy's TTL, so re-read it instead of
    // re-registering a cached one: register() rejects an expired catalog.
    try {
      register(await request('/catalog'));
      await request('/health');
      ctx.ui.notify(`OpenCode Free: ${catalog.models.length} маршрутів через локальний міст.`, 'info');
    } catch {
      ctx.ui.notify('OpenCode Free: міст недоступний. Запити залишаються на локальному маршруті.', 'warning');
    }
  });

  pi.on('before_provider_request', (event, ctx) => {
    // ctx.model is the model of THIS request (emitBeforeProviderRequest threads it),
    // so an advisor or subagent on another provider is never touched.
    if (!event.payload || !registered.has(ctx.model?.provider)) return;
    const id = ctx.sessionManager.getSessionId();
    if (typeof id !== 'string' || !id) throw new Error('OMP не передав ID сесії.');
    // OMP's documented hook returns the replacement payload directly, NOT {payload}.
    // The proxy removes this private field before forwarding to OpenCode.
    return { ...event.payload, _omp_free_session: id };
  });

  pi.registerCommand('free-models', {
    description: 'Показати дозволені безкоштовні маршрути та причини виключення',
    handler: async (_args, ctx) => {
      const snapshot = await request('/catalog');
      const lines = snapshot.models.map(m => `${m.lane}/${m.id} — ${m.api}`);
      ctx.ui.notify(lines.join('\n') + `\nВиключено: ${snapshot.excluded?.length ?? 0}. Повний звіт: python3 bridge.py catalog`, 'info');
    },
  });
  pi.registerCommand('free-refresh', {
    description: 'Перечитати поточний каталог проксі та оновити моделі цього розширення',
    handler: async (_args, ctx) => {
      register(await request('/catalog'));
      ctx.ui.notify(`Каталог перечитано: ${catalog.models.length} маршрутів. TTL каталогу — до 15 хв.`, 'info');
    },
  });
}
