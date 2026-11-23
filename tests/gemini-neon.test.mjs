import test from 'node:test';
import assert from 'node:assert/strict';
import registerGeminiNeon from '../gemini-neon.mjs';

test('explicit Gemini extension registers a single loopback model with its own key', () => {
  const previousKey = process.env.GEMINI_GATEWAY_KEY;
  const providers = new Map();
  const pi = { registerProvider: (id, config) => providers.set(id, config) };

  try {
    delete process.env.GEMINI_GATEWAY_KEY;
    assert.throws(() => registerGeminiNeon(pi), /GEMINI_GATEWAY_KEY/);
    assert.equal(providers.size, 0);

    process.env.GEMINI_GATEWAY_KEY = 'test-gateway-key';
    registerGeminiNeon(pi);
    assert.deepEqual([...providers.keys()], ['gemini-app']);
    const provider = providers.get('gemini-app');
    assert.equal(provider.baseUrl, 'http://127.0.0.1:18989/v1');
    assert.equal(provider.apiKey, 'test-gateway-key');
    assert.equal(provider.api, 'openai-completions');
    assert.equal(provider.authHeader, true);
    assert.deepEqual(provider.models.map(model => model.id), ['gemini-neon']);
  } finally {
    if (previousKey === undefined) delete process.env.GEMINI_GATEWAY_KEY;
    else process.env.GEMINI_GATEWAY_KEY = previousKey;
  }
});
