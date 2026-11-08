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

* Old Python 429 deadlines are retained under their client profile. The qualified
  Bun profile is tried independently once; its new 429s retain the full deadline
  across restarts. Server-side quotas are not reset or bypassed by state migration.
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
