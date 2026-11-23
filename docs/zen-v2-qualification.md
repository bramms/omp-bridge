# Zen / OpenCode 2 qualification, 2026-11-09

## Result

OpenCode 2.2.6 and qualified Bun 1.3.14 replace the old Zen 1.x wire identity
and Python HTTPS network stack. The Python server still admits only current,
verified zero-cost model/endpoint pairs and guards every SSE tool call. The
Bun process is a persistent, private transport; it never executes tools, reads
provider accounts, changes models, retries POSTs, or selects paid access.

Go keeps its separate previous identity and Python transport. This work does
not qualify Go or require its subscription/API key.

## Sources checked

* Official released `@opencode/cli-linux-x64@2.2.6`, checked against the official
  npm distribution's SHA-512 integrity. An isolated CLI used `public`, separate
  HOME/XDG/TMPDIR, `--standalone`, an explicit free model and supplied title.
  Tool permissions were `ask`, stdin closed, no `--auto`: tools were declared
  but no action could be approved. All processes were reaped before cleanup.
* [OpenCode model request](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/session/model-request.ts)
  and [environment instructions](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/instructions/builtins.ts).
* [Official free-tier guidance](https://github.com/anomalyco/opencode/issues/59387):
  old v1 clients no longer qualify; 429 does not establish a real usage balance.
* [Magpie Zen Free](https://github.com/magpie-community/plugins/tree/main/packages/zen-free),
  current package 0.1.26, especially [the November 1 comparison](https://github.com/yetone/magpie/issues/3491).
* [12errh/zen-proxy](https://github.com/12errh/zen-proxy): Bun network stack and
  native request-shape observations. We verified the relevant difference live;
  its full coding-agent preamble and tool filtering were **not** adopted.
* [9router analysis](https://github.com/luqman-v1/9router-go/issues/724) and
  [tool-name-dependent refusal](https://github.com/decolua/9router/issues/5319).

## Controlled observations on SILENT

The original bridge and initial v2-header-only candidate returned 429 through
Python. With the official CLI's entire tool catalog disabled, even the native
CLI got 429; this was not used as the final positive control.

With native tools declared and approval required, OpenCode 2.2.6 returned
completed `BRIDGE_OK` directly. A local recorder separately captured its wire
shape; the recorder's synthetic answer was labelled `local-contract-only` and
never counted as a real model success.

In a subsequent same-exit comparison:

| Request | Network implementation | Result |
|---|---|---|
| Captured native body and identity | Python HTTPS | 429 FreeUsageLimitError |
| Captured native body and identity | Bun 1.3.14 fetch | 200, completed BRIDGE_OK, reported cost 0 |
| Candidate bridge's own prompt, environment and guarded stand-in tools | Python HTTPS | 429 |
| Candidate bridge's own prompt, environment and guarded stand-in tools | Bun 1.3.14 fetch | 200, completed BRIDGE_OK, reported cost 0 |

This demonstrates a relevant **network-stack** difference; it does not isolate
TLS JA3/JA4 from HTTP behavior. The candidate needs neither OpenCode's full
persona nor a copied coding-agent system prompt.

The captured release sends `opencode/latest/2.2.6/cli`, `b3`, `traceparent`,
`x-opencode-client`, a project identifier, and four consistent session headers.
Read uses `path`; shell uses `command`. The CLI sends `max_completion_tokens`;
the candidate's existing `max_tokens` also qualified live and is preserved.

## Complete candidate bridge

Isolated full HTTP bridge on SILENT, 2026-11-09 01:05 Kyiv:

| Check | Result |
|---|---|
| DeepSeek V4.1 Flash Free | 200, BRIDGE_OK, terminal SSE, cost 0 |
| MiMo V2.6 Flash Free | 200, BRIDGE_OK, terminal SSE, cost 0 |
| GLM 5.3 Flash Free | 200, BRIDGE_OK, terminal SSE, cost 0 |
| DeepSeek actual bridge_echo call | Correct name and JSON arguments, terminal SSE, cost 0 |
| Tool result sent back, same conversation | BRIDGE_TOOL_OK, terminal SSE, cost 0 |

The echo tool is a pure string return in the diagnostic caller. No real shell,
file, Telegram, GPU or media action was executed by the test.

Offline validation on SILENT uses `choom -n 1000 --` for Python/Node tests;
all temporary checkouts, state and the downloaded runtime are under one
TemporaryDirectory in `/tmp`, with INT/TERM/HUP cleanup and child waits. The
runtime installer checks npm SHA-512 and writes the selected binary atomically.

Reproduction:

```sh
python3 -m unittest discover -s tests -p 'test_*.py' -q
node --test tests/extension.test.mjs tests/zen_transport.test.mjs
python3 deploy/install-zen-runtime.py
python3 tests/live_zen.py
python3 deploy/qualify-zen.py --compare-bun
```

## State and boundaries

* The November 9 build separated 429 deadlines by runtime profile. The
  current bridge retains the latest 429 for diagnostics only. Its routing
  discards historical quota deadlines on startup.
* WARP registrations, proxy pool configuration, account files and the local
  public-facing token are not replaced. The Bun RPC token is ephemeral and is
  never forwarded upstream or printed.
* Automatic version discovery accepts the official `@opencode/cli` v2 release
  only, at startup; outage or a different major uses qualified 2.2.6. Bun remains
  pinned to qualified 1.3.14 until a separate compatibility check.
* Native Responses/Anthropic SSE guards retain offline coverage. Real Responses,
  Anthropic and vision inference have not yet been qualified by this run.
* Arbitrary third-party tool names that Zen independently refuses are not
  silently renamed or dropped. A provider refusal remains visible.
* A real remaining quota is unknown. 429 and Retry-After are recorded as a
  refusal/deadline, not proof that generation will recover at that deadline.

## Final verification and WARP

At 01:11 Kyiv on November 9, the final candidate passed **152 Python tests** and
**20 JavaScript tests** on SILENT. DeepSeek, MiMo and GLM again returned
completed `BRIDGE_OK` directly; the tool round trip returned `BRIDGE_TOOL_OK`.
Each of these responses reported cost 0.

With the same candidate Bun transport and verified DeepSeek model, one separate
streaming request was sent through each of the ten existing WARP exits:

| WARP exits | Result |
|---|---|
| 2, 4, 5, 8, 10 | HTTP 200, completed BRIDGE_OK, reported cost 0 |
| 1, 3, 6, 7, 9 | HTTP 429 FreeUsageLimitError; Retry-After was preserved |

The working bridge is configured for the first five WARP exits. Its configuration
and running service were not changed by these checks. The test transport and
runtime lived only in the temporary qualification directory. No WARP restart
or registration change was needed for this result.

## OpenCode 2.3.0, 2026-11-16

The local recorder captured the official 2.3.0 release using the same trace,
project and four session headers as 2.2.6. Its chat request still used
`stream_options` and `max_completion_tokens`, and the core tools still used
`read.path` and `shell.command`. The official binary returned `BRIDGE_OK`
directly over Zen. With the bridge's existing request shape, the 2.3.0 profile
and Bun 1.3.14 returned completed streams for DeepSeek, MiMo and GLM at cost
zero. DeepSeek also completed the two-turn `bridge_echo` check. The new
`probe --route all` confirmed completed DeepSeek responses at cost zero
directly and through all ten configured WARP exits. The verified offline
version fallback is now 2.3.0.

* [OpenCode 2.3.0](https://github.com/anomalyco/opencode/releases/tag/v2.3.0)
  fixes classification of temporary request limits and preserves completed
  work when the client resumes a retryable stream. The bridge's retry is
  limited to errors before SSE begins.
* [9router](https://github.com/decolua/9router/commit/989b1eecb98f2e378fa8713f4c9b0fe40099e19b)
  reproduced an overload-specific 429. The bridge recognizes that response
  separately and allows one repeat with a wait of up to five seconds.
  `FreeUsageLimitError` can switch routes within the current request. Both forms of
  [HTTP Retry-After](https://www.rfc-editor.org/rfc/rfc9110.html#name-retry-after)
  are supported.
* [Magpie](https://github.com/yetone/magpie/issues/3491) and
  [zen-proxy](https://github.com/12errh/zen-proxy) corroborate the existing v2
  endpoints and request shape. Zen-proxy's model health checks also verify a
  real completed model answer. Our per-route probe uses the bridge's installed
  transport and reports the running service's recorded cooldown separately.

```sh
python3 deploy/qualify-zen.py --opencode-version 2.3.0
python3 tests/live_zen.py --opencode-version 2.3.0
python3 bridge.py probe --model deepseek-v4.1-flash-free --route all
```

### OMP `edit` compatibility, 2026-11-16

On OMP 18.20.2, a first-turn MiniMax M3.1 Flash Free request at `high` returned
`FreeUsageLimitError` with the default tools. All runs below used the same
direct exit and the OpenCode 2.3.0 Bun transport. The header and model were held
constant when comparing the two `edit` schemas.

| MiniMax request | Zen response |
|---|---|
| OMP with `--no-tools` | 200, completed model answer |
| Same request with the default OMP tool declarations | 429 |
| Default declarations with the output cap lowered to 1,024 | 429 |
| Default declarations after mapping the three `edit` fields | 200, completed model answer |
| Same mapped declaration with any one of the three fields restored to the OMP spelling | 429 |

OMP's `old_string`, `new_string`, and `replace_all` correspond to `oldString`,
`newString`, and `replaceAll` in [OpenCode 2.3.0's `edit` tool](https://raw.githubusercontent.com/anomalyco/opencode/v2.3.0/packages/core/src/tool/plugin/edit.ts).
The bridge maps the declaration and prior `edit` calls sent to Zen Chat
Completions, then restores OMP's argument names on streamed tool calls. An OMP
run with MiniMax at `high` passed with the default tool set. A separate run
read a disposable file, successfully edited it, and received the final answer
after passing the edit result back to MiniMax.

The [9Router DeepSeek report from November 14](https://github.com/decolua/9router/issues/5690)
reproduced the same error with a different tool declaration. A
[November 11 MiniMax report](https://github.com/decolua/9router/issues/5600)
shows the same response text. The OpenCode [free-tier issue thread](https://github.com/anomalyco/opencode/issues/59387)
also contains reports from official clients. The controlled comparison above
isolates the declaration that triggered the refusal in this bridge.
