# Gemini 4 Neon в Oh My Pi

`gemini-app/gemini-neon` у списку моделей OMP надсилає запити до локального
`gemini-app2api`. Шлюз використовує квоту Gemini App. Для проби виберіть один
власний акаунт, на якому Gemini 4 Neon уже відповідає в Gemini Web. Доступ Jio до
Gemini App перевіряйте окремо від доступу в Antigravity.

## 1. Запустити шлюз на SILENT

Запустіть із кореня репозиторію в Bash. Compose використовує `gemini-app2api`
версії `1.7.1`, окремий том і два порти на loopback.

```bash
umask 077
gemini_state="$HOME/.config/omp-gemini-neon"
install -d -m 700 "$gemini_state"
if [ ! -e "$gemini_state/gateway.env" ]; then
  gemini_password=$(openssl rand -hex 24) || exit 1
  printf 'GATEWAY_ADMIN_PASSWORD=%s\n' "$gemini_password" > "$gemini_state/gateway.env"
  unset gemini_password
fi
docker compose --env-file "$gemini_state/gateway.env" -f deploy/gemini/compose.yaml up -d
curl -fsS http://127.0.0.1:18989/health
```

З Mac відкрийте SSH-тунель і залиште його активним під час входу.

```bash
ssh -o ExitOnForwardFailure=yes -N \
  -L 127.0.0.1:18989:127.0.0.1:18989 -L 127.0.0.1:18765:127.0.0.1:18765 silent
```

У [локальній адмінпанелі](http://127.0.0.1:18989/admin/) увійдіть із паролем із
`~/.config/omp-gemini-neon/gateway.env` на SILENT. У **Accounts** підключіть лише
обраний акаунт через **Google sign-in** і натисніть **Test**. У **API keys**
створіть ключ клієнта для `gemini-neon`, якщо інтерфейс пропонує обмеження за
моделлю. Список у **Accounts** показує профілі цього шлюзу.

[Реліз gemini-app2api 1.7.1](https://github.com/ShawTim/gemini-app2api/releases/tag/v1.7.1) · [Налаштування шлюзу](https://github.com/ShawTim/gemini-app2api/blob/main/docs/OPERATIONS.md)

## 2. Перевірити Neon і завантажити модель в OMP

На SILENT введіть створений ключ у Bash без виведення на екран.

```bash
read -r -s -p 'Gemini gateway key: ' GEMINI_GATEWAY_KEY
printf '\n'
export GEMINI_GATEWAY_KEY

printf 'header = "Authorization: Bearer %s"\n' "$GEMINI_GATEWAY_KEY" |
  curl --config - --fail-with-body -sSN --max-time 240 -D - \
    -H 'Content-Type: application/json' \
    --data '{"model":"gemini-neon","messages":[{"role":"user","content":"Reply exactly NEON_OK."}],"stream":true}' \
    http://127.0.0.1:18989/v1/chat/completions
```

Очікуйте SSE із `NEON_OK`, фінальний `finish_reason: stop` та `data: [DONE]`.
Шлюз перевіряє атрибуцію Neon у відповіді Gemini App. При помилці entitlement
або `MODEL_DOWNGRADE_REJECTED` перевірте акаунт у Gemini Web та результат
**Accounts → Test**. `/v1/models` показує каталог шлюзу.

```bash
omp -e "$PWD/gemini-neon.mjs" --model gemini-app/gemini-neon
```

Для одночасного доступу до Zen/Go додайте `-e "$PWD/extension.mjs"` до команди
запуску. Перевірте `gemini-app/gemini-neon` у `/model`. Для тесту інструментів
скористайтеся тимчасовим файлом і обмежте виклики наявним тестовим розширенням.

```bash
(
  bridge_dir="$PWD"
  probe_dir=$(mktemp -d) || exit 1
  trap 'rm -rf -- "$probe_dir"' EXIT
  printf 'NEON_TOOL_OK\n' > "$probe_dir/sample.txt"
  touch "$probe_dir/allow-edit"
  printf 'retry:\n  modelFallback: false\n  maxRetries: 0\n' > "$probe_dir/probe.yml"
  OMP_PROBE_DIR="$probe_dir" PI_CODING_AGENT_DIR="$probe_dir/agent" \
    omp --cwd "$probe_dir" --no-session --offline --no-context-files \
    --no-extensions --no-lsp --tools read --approval-mode yolo \
    --config "$probe_dir/probe.yml" \
    -e "$bridge_dir/gemini-neon.mjs" -e "$bridge_dir/deploy/omp-probe-guard.mjs" \
    --model gemini-app/gemini-neon --smol gemini-app/gemini-neon --thinking low \
    -p 'Use the read tool to read sample.txt, then reply with the exact content.'
)
```

Успішний тест показує один виклик `read` і фінальну відповідь `NEON_TOOL_OK`.
Ліміт контексту `512000` у розширенні взятий із каталогу шлюзу. `maxTokens`
слугує підказкою для OMP. Шлюз приймає `max_tokens` для сумісності.

[API шлюзу](https://github.com/ShawTim/gemini-app2api/blob/main/docs/API_SUPPORT.md)

## Виклик інструментів OMP із Gemini Web через MCP

Цей напрямок доступний через **Connected Apps → Add a custom app**. Google
вказує вимоги до регіону, типу акаунта, мови й історії. Почніть з ізольованої
тестової машини та endpoint, який повертає один нешкідливий інструмент читання.
`a2mcp` передає реєстр інструментів із запущеної OMP-сесії та додає `xfer_*`.
Для публічного тесту ставте перед ним MCP-сервер, який віддає лише дозволений
інструмент і перевіряє його аргументи. OAuth контролює доступ клієнта до endpoint.

1. Увімкніть HTTPS та Funnel на тестовому вузлі. Публічну адресу визначайте
   `tailscale status --json | jq -er '.Self.DNSName | strings | rtrimstr(".") | select(length > 0)'`.
   Перед запуском перевірте `tailscale funnel status`, щоб використати вільний listener.
2. Якщо тестовий backend використовує `a2mcp`, залиште `host` рівним `127.0.0.1`,
   зафіксуйте `port: 38239` і перевірте фактичний порт після перезапуску OMP.
   Оновлюйте `a2mcp.json` через тимчасовий файл у тому самому каталозі з правами
   `0600`. Замінюйте оригінал лише після успішного запису `jq`. Для читання токена
   використайте `jq -er '.token | strings | select(length >= 32)'` і передайте
   значення як змінну середовища без друку в термінал.
3. Для `mcp-oauth-gateway` використайте образ `ghcr.io/xnyzer/mcp-oauth-gateway:0.5.0`
   з `EXTERNAL_URL=https://<домен-Funnel>`, `NO_AUTO_TLS=true` та
   `LISTEN=127.0.0.1:18082`. Задайте bcrypt `PASSWORD_HASH`, збережіть стан `/data`
   між перезапусками й передайте шлюзу URL обмеженого MCP endpoint. Якщо цей
   endpoint використовує Bearer, додайте `PROXY_BEARER_TOKEN`. Для Docker із
   `--network host` на Linux loopback хоста доступний контейнеру. Виберіть окреме
   ім’я контейнера для тесту. Запустіть `tailscale funnel --bg http://127.0.0.1:18082`.
4. Перевірте публічні `/.well-known/oauth-authorization-server` і
   `/.well-known/oauth-protected-resource`. Запит `initialize` без токена має
   повернути `401` і `WWW-Authenticate`. Після входу перевірте `initialize`,
   `notifications/initialized`, `tools/list` і один `tools/call`. Збережіть
   `Mcp-Session-Id` між викликами, якщо сервер його повертає. У Gemini Web можна
   явно обрати підключений застосунок через `@`.

[Gemini Connected Apps](https://support.google.com/gemini/answer/17209137) · [OAuth 2.1 для Gemini](https://developers.google.com/connected-apps/mcp/oauth) · [a2mcp](https://github.com/adam-ikari/a2mcp) · [mcp-oauth-gateway 0.5.0](https://github.com/xnyzer/mcp-oauth-gateway/releases/tag/v0.5.0) · [Tailscale Funnel](https://tailscale.com/docs/reference/tailscale-cli/funnel)
