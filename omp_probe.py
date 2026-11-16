"""Run live bridge checks through an isolated OMP session with synthetic data."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import uuid


ROOT = Path(__file__).resolve().parent
OLD_TEXT = "BRIDGE_EDIT_BEFORE"
NEW_TEXT = "BRIDGE_EDIT_AFTER"
COMPACT_MARKER = "AMBER CARROT"
STEP_TIMEOUT = 90


class ProbeFailure(Exception):
    def __init__(self, message, http=None):
        super().__init__(message)
        self.http = http


class OmpRpc:
    def __init__(self, process):
        self.process = process
        self.events = queue.Queue()
        self.sequence = 0
        threading.Thread(target=self.read, daemon=True).start()

    def read(self):
        for line in self.process.stdout:
            try:
                frame = json.loads(line)
            except ValueError:
                continue
            if isinstance(frame, dict):
                self.events.put(frame)
        self.events.put(None)

    def call(self, command, **fields):
        self.sequence += 1
        identifier = str(self.sequence)
        self.process.stdin.write(json.dumps({"id": identifier, "type": command, **fields}) + "\n")
        self.process.stdin.flush()
        deadline = time.monotonic() + STEP_TIMEOUT
        messages = []
        while True:
            try:
                event = self.events.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty as exc:
                raise ProbeFailure(f"OMP {command} timed out after {STEP_TIMEOUT}s.") from exc
            if event is None:
                raise ProbeFailure(f"OMP exited before completing {command}.")
            kind = event.get("type")
            if kind == "retry_fallback_applied":
                raise ProbeFailure("OMP switched to a fallback model.")
            if kind == "message_end":
                messages.append(event["message"])
            if event.get("id") != identifier:
                continue
            if kind == "response":
                if not event.get("success"):
                    raise ProbeFailure(str(event.get("error") or "OMP rejected the command."))
                if command != "prompt":
                    return event.get("data") or {}
            if kind == "prompt_result":
                if event.get("status") != "completed":
                    error = event.get("error") or {}
                    raise ProbeFailure(error.get("message") or "OMP did not complete the turn.", error.get("httpStatus"))
                return messages


@contextmanager
def omp_session(executable, bridge_state, folder, model, *, resume=None):
    folder.mkdir(exist_ok=True)
    marker = folder / "guard-ready"
    config = folder / "probe.yml"
    config.write_text("compaction:\n  keepRecentTokens: 256\n  methodOrder: [soft]\n"
                      "retry:\n  modelFallback: false\n  maxRetries: 0\n", encoding="utf-8")
    env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "LANG", "TERM", "SystemRoot") if key in os.environ}
    env.update(PI_CODING_AGENT_DIR=str(folder / "agent"), OMP_FREE_HOME=str(bridge_state.resolve()),
               OMP_FREE_PROBE="1", OMP_PROBE_DIR=str(folder))
    args = [executable, "--mode", "rpc", "--no-ui", "--offline", "--no-extensions", "--no-context-files",
            "--no-lsp", "--approval-mode", "yolo", "--config", str(config), "--model", model, "--smol", model,
            "--thinking", "high", "-e", str(ROOT / "deploy" / "omp-probe-guard.mjs"),
            "-e", str(ROOT / "extension.mjs")]
    args += ["--resume", str(resume)] if resume else ["--no-session"]
    with subprocess.Popen(args, cwd=folder, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1) as process:
        try:
            client = OmpRpc(process)
            state = client.call("get_state")
            active = state.get("model") or {}
            if not marker.is_file() or f"{active.get('provider')}/{active.get('id')}" != model:
                raise ProbeFailure("The probe tool guard or the requested free model did not load in OMP.")
            yield client
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def verify_reply(messages, provider, model, expected):
    assistant = [message for message in messages if message.get("role") == "assistant"]
    if not assistant or any((m.get("provider"), m.get("model")) != (provider, model) for m in assistant):
        raise ProbeFailure("OMP did not answer using the requested model throughout the turn.")
    final = assistant[-1]
    answer = "".join(item.get("text", "") for item in final.get("content", []) if item.get("type") == "text")
    if final.get("stopReason") != "stop" or answer.strip() != expected:
        raise ProbeFailure(f"OMP did not complete the turn with {expected}.")


def seed_history(folder, provider, model):
    folder.mkdir()
    stamp = datetime.now(timezone.utc)
    identifier = str(uuid.uuid4())
    now = int(stamp.timestamp() * 1000)
    rows = [{"type": "session", "version": 3, "id": identifier, "timestamp": stamp.isoformat(), "cwd": str(folder)}]
    previous = None
    # Give the summary a prefix to compact without sending any real workspace data.
    for number in range(8):
        user = f"Review part {number + 1} of a fictional board game. Preserve its codename {COMPACT_MARKER}. " * 12
        answer = f"{COMPACT_MARKER} is a game for two. A star marks a safe tile. The far corner is the finish. " * 15
        for role, text in (("user", user), ("assistant", answer)):
            message = {"role": role, "content": [{"type": "text", "text": text}], "timestamp": now}
            if role == "assistant":
                message.update(api="openai-completions", provider=provider, model=model, stopReason="stop",
                               usage={"input": 300, "output": 220, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 520,
                                      "cost": dict.fromkeys(("input", "output", "cacheRead", "cacheWrite", "total"), 0)})
            entry = uuid.uuid4().hex[:8]
            rows.append({"type": "message", "id": entry, "parentId": previous, "timestamp": stamp.isoformat(), "message": message})
            previous = entry
    path = folder / f"{stamp.isoformat().replace(':', '-')}_{identifier}.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def probe_omp(bridge_state, row):
    """Yield live step outcomes, including a failure on the step that stopped the check."""
    model = row["id"]
    provider = "opencode-zen-free"
    executable = shutil.which("omp")
    if not executable:
        yield {"step": "startup", "result": "FAILED", "error": "Install OMP or add omp to PATH."}
        return
    step = "turn"
    try:
        with tempfile.TemporaryDirectory(prefix="omp-bridge-probe-") as temporary:
            folder = Path(temporary).resolve()
            target = folder / "sample.txt"
            target.write_text(OLD_TEXT + "\n", encoding="utf-8")
            with omp_session(executable, bridge_state, folder, f"{provider}/{model}") as rpc:
                answer = rpc.call("prompt", message="Reply exactly BRIDGE_OK. Do not call tools.")
                verify_reply(answer, provider, model, "BRIDGE_OK")
                yield {"step": step, "http": 200, "result": "MODEL_ANSWER_CONFIRMED"}

                step = "edit"
                (folder / "allow-edit").touch()
                answer = rpc.call("prompt", message=(
                    "Read sample.txt and use the edit tool to replace the exact text "
                    f"{OLD_TEXT} with {NEW_TEXT} without changing the trailing newline. "
                    "After the edit tool reports success, reply exactly BRIDGE_EDIT_OK."))
                verify_reply(answer, provider, model, "BRIDGE_EDIT_OK")
                tools = {m.get("toolName") for m in answer if m.get("role") == "toolResult" and not m.get("isError")}
                if not {"read", "edit"} <= tools or target.read_text(encoding="utf-8") != NEW_TEXT + "\n":
                    raise ProbeFailure("OMP did not read and edit the disposable file successfully.")
                yield {"step": step, "http": 200, "result": "MODEL_ANSWER_CONFIRMED"}

            step = "compact"
            folder = folder / "compaction"
            resume = seed_history(folder, provider, model)
            with omp_session(executable, bridge_state, folder, f"{provider}/{model}", resume=resume) as rpc:
                compact = rpc.call("compact", customInstructions=f"Preserve the codename {COMPACT_MARKER}.")
                if COMPACT_MARKER not in (compact.get("summary") or "").upper():
                    raise ProbeFailure("OMP did not return a summary containing the synthetic codename.")
                answer = rpc.call("prompt", message="Reply exactly BRIDGE_COMPACT_OK. Do not call tools.")
                verify_reply(answer, provider, model, "BRIDGE_COMPACT_OK")
                yield {"step": step, "http": 200, "result": "MODEL_ANSWER_CONFIRMED"}
    except (OSError, ProbeFailure) as exc:
        yield {"step": step, "result": "FAILED", "error": str(exc)[:1000],
               **({"http": exc.http} if isinstance(exc, ProbeFailure) and exc.http else {})}
