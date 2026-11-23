// Load explicitly with `omp -e /path/to/gemini-neon.mjs --model gemini-app/gemini-neon`.
export default function registerGeminiNeon(pi) {
  const apiKey = process.env.GEMINI_GATEWAY_KEY?.trim();
  if (!apiKey || /\s/.test(apiKey)) {
    throw new Error('Gemini Neon: задайте GEMINI_GATEWAY_KEY (ключ локального gemini-app2api).');
  }

  pi.registerProvider('gemini-app', {
    baseUrl: 'http://127.0.0.1:18989/v1',
    apiKey,
    api: 'openai-completions',
    authHeader: true,
    models: [{
      id: 'gemini-neon',
      name: 'Gemini 4 Neon (Gemini App)',
      reasoning: true,
      thinking: { mode: 'effort', efforts: ['low', 'medium', 'high'] },
      input: ['text', 'image'],
      contextWindow: 512000,
      maxTokens: 8192,
      compat: {
        supportsStore: false,
        supportsDeveloperRole: false,
        supportsReasoningEffort: true,
        maxTokensField: 'max_tokens',
      },
    }],
  });
}
