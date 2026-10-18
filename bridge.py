#!/usr/bin/env python3
"""OMP OpenCode free-only bridge. Python 3.10+, standard library only.
No OpenCode CLI, subprocesses, key rotation, inference retries or paid fallback.
See docs/ДОСЛІДЖЕННЯ.md before enabling --compat-opencode.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import getpass
import hashlib
import hmac
import http.client
import json
import math
import os
from pathlib import Path
import re
import secrets
import select
import socket
import ssl
import sys
import threading
import time
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

VERSION = "1.4.0"
STATE = Path(os.environ.get("OMP_FREE_HOME", "~/.config/omp-free-bridge")).expanduser()
CORE = ("bash", "edit", "glob", "grep", "read", "write")
GO_CORE = ("bash", "edit", "glob", "grep", "read")
SESSION_RE = re.compile(r"ses_[0-9a-f]{12}[0-9A-Za-z]{14}\Z")
ID_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
ID_LOCK = threading.Lock()
SESSION_IDS: dict[str, str] = {}
ID_TIMESTAMP_MS = 0
ID_COUNTER = 0
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}\Z")
BASES = {"zen": "/zen/v1", "go": "/zen/go/v1"}
APIS = {"chat/completions": "openai-completions", "responses": "openai-responses",
        "messages": "anthropic-messages"}
NPM = {"@ai-sdk/openai-compatible": "chat/completions", "@ai-sdk/openai": "responses",
       "@ai-sdk/anthropic": "messages"}
TTL = 900  # Refresh before inference after 15 minutes; never serve an expired snapshot.
MAX_BODY = 32 * 1024 * 1024
MAX_FRAME = 2 * 1024 * 1024


class BridgeError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "BridgePolicyError"):
        super().__init__(message)
        self.status, self.code = status, code


def removed_route(path: str) -> bool:
    """1.3.0 dropped the unified opencode-free provider and its /free route. It is
    refused by name: resolving an old ID to Zen or Go would be a silent fallback."""
    return path == "/free" or path.startswith("/free/")


REMOVED_ROUTE = ("Provider opencode-free and its /free route were removed in 1.3.0. "
                 "Select a model from opencode-zen-free or opencode-go-free.")


def encode(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def load_json(data: bytes | str):
    def reject(value):
        raise ValueError(f"Non-finite JSON number: {value}")
    return json.loads(data, parse_constant=reject)


def save_private(path: Path, data: bytes):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def read_private(path: Path) -> str:
    # One handle for open/stat/read: a symlink or mode swap between the checks cannot
    # redirect the read. O_NOFOLLOW refuses the symlink itself.
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise BridgeError(f"Refusing a symlink or unreadable credential file: {path}") from exc
    with os.fdopen(fd, "rb") as stream:
        if os.name == "posix" and os.fstat(fd).st_mode & 0o077:
            raise BridgeError(f"Run chmod 600 '{path}' before using this credential file.")
        return stream.read(MAX_FRAME).decode().strip()


def key_from_user(required: bool = True) -> str:
    key = os.environ.get("OPENCODE_API_KEY", "").strip()
    if not key and (STATE / "zen.key").exists():
        key = read_private(STATE / "zen.key")
    if not key and required:
        raise BridgeError("No API key. Run: python3 bridge.py login", 401)
    if "\n" in key or "\r" in key or len(key) > 8192:
        raise BridgeError("Malformed API key.", 401)
    if key.startswith("st-"):
        raise BridgeError("Console OAuth st- tokens are not Zen API keys. Use a Zen/Go API key.", 401)
    return key


def token_from_user() -> str:
    path = STATE / "local.token"
    if not path.exists():
        save_private(path, secrets.token_urlsafe(32).encode())
    token = read_private(path)
    if len(token) < 32:
        raise BridgeError("Local token is too short; remove local.token and restart.")
    return token


def session_token(identity: str) -> str:
    if SESSION_RE.fullmatch(identity):
        return identity
    with ID_LOCK:
        token = SESSION_IDS.get(identity)
        if token is None:
            token = "ses_" + opencode_id(True)
            if len(SESSION_IDS) >= 10_000:
                SESSION_IDS.pop(next(iter(SESSION_IDS)))
            SESSION_IDS[identity] = token
        return token


def opencode_id(descending: bool) -> str:
    """OpenCode's 6-byte millisecond/counter prefix and 14 base62 bytes.

    Caller holds ID_LOCK so two requests in the same millisecond get distinct
    counters. Random-looking low timestamp bits fail Zen's free-tier check.
    """
    global ID_TIMESTAMP_MS, ID_COUNTER
    now = max(int(time.time() * 1000), ID_TIMESTAMP_MS)
    if now != ID_TIMESTAMP_MS:
        ID_TIMESTAMP_MS, ID_COUNTER = now, 0
    ID_COUNTER += 1
    if ID_COUNTER >= 0x1000:
        ID_TIMESTAMP_MS, ID_COUNTER = now + 1, 1
    value = ID_TIMESTAMP_MS * 0x1000 + ID_COUNTER
    if descending:
        value = ~value
    return f"{value & 0xFFFFFFFFFFFF:012x}" + "".join(ID_ALPHABET[x % 62] for x in secrets.token_bytes(14))


def request_token() -> str:
    with ID_LOCK:
        return "msg_" + opencode_id(False)


def public_get(url: str, timeout: float = 15) -> bytes:
    """Fixed public hosts, TLS verification, no redirects and no credentials."""
    u = urlsplit(url)
    if u.scheme != "https" or u.hostname not in {"opencode.ai", "models.dev"} or u.port:
        raise BridgeError("Unapproved discovery URL.")
    conn = http.client.HTTPSConnection(u.hostname, timeout=timeout, context=ssl.create_default_context())
    try:
        conn.request("GET", u.path + ("?" + u.query if u.query else ""), headers={
            "User-Agent": "omp-free-bridge/" + VERSION, "Accept-Encoding": "identity",
            **({"Authorization": "Bearer public"} if u.hostname == "opencode.ai" else {})})
        response = conn.getresponse()
        if response.status != 200:
            raise BridgeError(f"Discovery HTTP {response.status} from {u.hostname}.", 503, "CatalogUnavailable")
        raw = response.read(MAX_BODY + 1)
        if len(raw) > MAX_BODY:
            raise BridgeError("Discovery response exceeds 32 MiB.", 503)
        return raw
    finally:
        conn.close()


class TableParser(HTMLParser):
    """Only read table text; never execute remote content."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self.table, self.row, self.cell = [], None, None, None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.table = []
        elif tag == "tr" and self.table is not None:
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.table.append(self.row)
            self.row = None
        elif tag == "table" and self.table is not None:
            self.tables.append(self.table)
            self.table = None


def zero_text(value: str, dash_ok: bool = True) -> bool:
    """A dash means "not applicable", which can waive a cache price but never state one."""
    value = value.strip().lower()
    return value in {"free", "$0", "$0.00", "0", "0.00"} or (dash_ok and value in {"-", "—"})


def docs_models(html: str) -> dict:
    parser = TableParser()
    parser.feed(html)
    endpoints, pricing, pricing_key = {}, {}, "model"
    for table in parser.tables:
        if not table:
            continue
        headers = [h.lower() for h in table[0]]
        if {"model", "model id", "endpoint"}.issubset(headers):
            for row in table[1:]:
                if len(row) != len(headers):
                    continue
                d = dict(zip(headers, row))
                endpoints[d["model id"]] = {"name": d["model"], "endpoint_url": d["endpoint"]}
        if {"model", "input", "output"}.issubset(headers):
            # Prefer the unambiguous ID column; a display name can be shared by a
            # free model and its paid sibling.
            pricing_key = "model id" if "model id" in headers else "model"
            for row in table[1:]:
                if len(row) != len(headers):
                    continue
                d = dict(zip(headers, row))
                costs = [v for k, v in d.items() if k in {"input", "output", "cached read", "cached write"}]
                # A dash cannot establish a free input/output price.
                free = all(zero_text(d[k], dash_ok=False) for k in ("input", "output")) and all(map(zero_text, costs))
                key = d[pricing_key]
                # Two pricing rows under one key: neither row can be attributed.
                pricing[key] = False if key in pricing else free
    for model_id, row in endpoints.items():
        row["free"] = pricing.get(model_id if pricing_key == "model id" else row["name"])
    return endpoints


def zero_cost(cost) -> bool:
    if not isinstance(cost, dict) or not {"input", "output"}.issubset(cost):
        return False
    def all_zero(value):
        if isinstance(value, dict):
            return all(all_zero(x) for x in value.values())
        return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value == 0
    return all_zero(cost)


def positive(value, fallback: int) -> int:
    return int(value) if not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value >= 1 else fallback


def compile_catalog(listings: dict, metadata: dict, documentation: dict, now: float | None = None) -> dict:
    """Join live IDs to prices. Missing documentation is not a nonzero price.
    Metadata-only admission requires an explicitly named, non-retired free edition;
    numeric zero alone can be an unpriced placeholder in models.dev."""
    models, excluded = [], []
    for lane, listing in listings.items():
        if lane not in BASES or not isinstance(listing, dict) or not isinstance(listing.get("data"), list):
            raise BridgeError("Malformed model listing.", 503, "CatalogUnavailable")
        provider = metadata.get("opencode" if lane == "zen" else "opencode-go", {})
        meta_models = provider.get("models", {}) if isinstance(provider, dict) else {}
        seen = set()
        for item in listing["data"]:
            model_id = item.get("id") if isinstance(item, dict) else None
            if not isinstance(model_id, str) or not ID_RE.fullmatch(model_id) or model_id in seen:
                continue
            seen.add(model_id)
            meta = meta_models.get(model_id, {})
            doc = documentation.get(lane, {}).get(model_id, {})
            cost = meta.get("cost")
            free_meta = zero_cost(cost)
            free_doc = doc.get("free") is True
            # A conflicting or unknown nonzero cost is never rounded down to free.
            conflict = (cost is not None and not free_meta and free_doc) or (doc.get("free") is False and free_meta)
            named_free = model_id.endswith("-free") and bool(re.search(r"\bfree\b", meta.get("name", ""), re.I))
            verified_meta = free_meta and named_free and meta.get("status") != "deprecated"
            reason = "pricing conflict" if conflict else None
            if not free_doc and not verified_meta:
                reason = "not verified zero-cost or retired free edition (name/suffix alone is not evidence)"
            endpoint = None
            url = doc.get("endpoint_url", "")
            if url:
                # A documented endpoint is the model's own; never override it with the
                # provider-wide npm default, which would relabel e.g. a System One
                # decision API as a chat model.
                for e in APIS:
                    accepted = {"https://opencode.ai" + BASES[lane] + "/" + e}
                    if lane == "zen":
                        accepted.add("https://opencode.ai/inference/v1/" + e)
                        family = "anthropic" if e == "messages" else "openai"
                        accepted.add("https://opencode.ai/inference/" + family + "/v1/" + e)
                    if url in accepted:
                        endpoint = e
            else:
                npm = meta.get("provider", {}).get("npm", provider.get("npm")) if isinstance(meta.get("provider", {}), dict) else None
                endpoint = NPM.get(npm)
            if endpoint is None and not reason:
                reason = "unsupported or unverified wire protocol"
            if reason:
                excluded.append({"lane": lane, "id": model_id, "reason": reason})
                continue
            limit = meta.get("limit", {})
            modalities = meta.get("modalities", {}).get("input", ["text"])
            models.append({
                "lane": lane, "id": model_id, "name": meta.get("name") or doc.get("name") or model_id,
                "endpoint": endpoint, "api": APIS[endpoint],
                "reasoning": meta.get("reasoning") is True,
                "efforts": [v for option in meta.get("reasoning_options", []) if option.get("type") == "effort"
                            for v in option.get("values", []) if v in {"minimal", "low", "medium", "high", "xhigh", "max"}],
                "input": [m for m in ("text", "image") if m in modalities] or ["text"],
                "contextWindow": positive(limit.get("context"), 32768),
                "maxTokens": positive(limit.get("output"), 8192),
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                "evidence": ("official pricing + models.dev" if free_doc and free_meta else "official pricing" if free_doc else "models.dev"),
                "limits_source": "models.dev" if limit else "conservative local defaults, not measured limits",
            })
    stamp = time.time() if now is None else now
    return {"schema": 1, "checked_at": stamp, "expires_at": stamp + TTL,
            "models": models, "excluded": excluded, "notes": []}


class Catalog:
    def __init__(self, state: Path = STATE, fetch=public_get):
        self.state, self.fetch = state, fetch
        self.lock = threading.Lock()
        self.snapshot = None

    def get(self, force: bool = False) -> dict:
        with self.lock:
            snapshot, now = self.snapshot, time.time()
        if not force and snapshot and snapshot["checked_at"] <= now < snapshot["expires_at"] and snapshot["expires_at"] - snapshot["checked_at"] <= TTL:
            return copy.deepcopy(snapshot)
        # Refreshing outside the lock: two concurrent refreshes cost one extra fetch,
        # holding it would stall every waiting request for up to five HTTP timeouts.
        listings, docs, notes = {}, {}, []
        for lane in BASES:
            try:
                listings[lane] = load_json(self.fetch("https://opencode.ai" + BASES[lane] + "/models"))
            except Exception:
                # Do not keep stale models from an unavailable lane.
                notes.append(lane + ": listing unavailable; lane disabled")
                continue
            try:
                docs_url = "https://opencode.ai/v2/docs/console/models" if lane == "zen" else "https://opencode.ai/docs/go/"
                docs[lane] = docs_models(self.fetch(docs_url).decode())
            except Exception:
                docs[lane] = {}
            if not docs[lane]:
                # A 200 whose tables no longer parse looks identical to an outage here.
                notes.append(lane + ": official pricing table unreadable; only named active free editions with explicit metadata costs accepted")
        if not listings:
            raise BridgeError("Both live model listings are unavailable. No stale fallback.", 503, "CatalogUnavailable")
        try:
            metadata = load_json(self.fetch("https://models.dev/api.json"))
            if not isinstance(metadata, dict):
                raise ValueError("metadata is not an object")
        except Exception:
            metadata = {}
            notes.append("models.dev unavailable; only models with explicit official Free pricing accepted")
        try:
            fresh = compile_catalog(listings, metadata, docs)
        except (AttributeError, TypeError, ValueError) as exc:
            raise BridgeError("Malformed remote catalog; inference disabled.", 503, "CatalogUnavailable") from exc
        fresh["notes"] = notes
        save_private(self.state / "catalog.json", encode(fresh))
        with self.lock:
            self.snapshot = fresh
        return copy.deepcopy(fresh)


def prepare(payload: dict, endpoint: str, headers: dict, compat: bool, ua_version: str, lane: str = "zen") -> tuple[dict, dict, set]:
    body = copy.deepcopy(payload)
    identity = body.pop("_omp_free_session", None)
    body.pop("_omp_free_bridge", None)
    identity = identity or headers.get("session_id") or headers.get("x-opencode-session") or headers.get("x-claude-code-session-id") or body.get("prompt_cache_key") or headers.get("x-omp-free-session")
    if not isinstance(identity, str) or not identity or len(identity) > 4096:
        raise BridgeError("Missing stable OMP session ID. Load extension.mjs; do not invent one per request.")
    session = session_token(identity)
    outbound = {"User-Agent": ("opencode/" + ua_version if compat else "omp-free-bridge/" + VERSION),
                "x-opencode-session": session, "Content-Type": "application/json",
                "Accept-Encoding": "identity"}
    if compat and lane == "zen":
        outbound.update({
            "x-opencode-client": "cli",
            "x-opencode-project": hashlib.sha1(b"git-remote:github.com/anomalyco/opencode").hexdigest(),
            "x-opencode-session-id": session,
            "x-session-affinity": session,
        })
    if endpoint == "messages":
        version = headers.get("anthropic-version", "2023-06-01")
        if not re.fullmatch(r"[0-9][0-9-]{0,31}", version):
            raise BridgeError("Malformed anthropic-version header.")
        outbound["anthropic-version"] = version
        # OMP's usual opencode-zen path omits unsupported context-management beta.
        body.pop("context_management", None)
    # Never silently turn a synchronous request into SSE.
    if body.get("stream") is not True:
        raise BridgeError("This bridge requires stream:true for generative requests. Native OMP already streams.", 400, "StreamingRequired")
    if compat and lane == "zen" and body.get("tool_choice") not in (None, "auto"):
        # The Zen free gate accepts auto or an omitted choice only.
        body.pop("tool_choice", None)
        if body.get("reasoning_effort") == "none":
            body.pop("reasoning_effort", None)
        if isinstance(body.get("reasoning"), dict) and body["reasoning"].get("effort") == "none":
            body["reasoning"].pop("effort", None)
    tools = body.get("tools", [])
    if not isinstance(tools, list):
        raise BridgeError("tools must be an array.")
    if any(not isinstance(tool, dict) for tool in tools):
        raise BridgeError("A tools entry is not an object.")
    declared = {(tool.get("function") or {}).get("name") if endpoint == "chat/completions" else tool.get("name")
                for tool in tools}
    originally_toolless = not tools
    core = CORE if lane == "zen" else GO_CORE
    missing = set(core) - declared if compat else set()
    if missing:
        tools = list(tools)
        for name in core:
            if name not in missing:
                continue
            schema = {"type": "object", "properties": {"unused": {"type": "string"}}, "additionalProperties": False}
            stub = {"name": name, "description": "Unavailable in this call. Do not invoke this tool."}
            if endpoint == "messages":
                stub["input_schema"] = schema
            else:
                stub.update(parameters=schema, strict=False)
                stub = {"type": "function", "function": stub} if endpoint == "chat/completions" else {"type": "function", **stub}
            tools.append(stub)
        body["tools"] = tools
        if originally_toolless and lane == "go":
            body["tool_choice"] = {"type": "none"} if endpoint == "messages" else "none"
        # Zen accepts only auto or absent. The response guard below refuses
        # calls to compatibility stubs before any such call reaches OMP.
    return body, outbound, missing


class StreamGuard:
    """Reject fake tool invocations before completion, without changing real tool schemas."""
    def __init__(self, endpoint: str, missing: set[str]):
        self.endpoint, self.missing, self.names = endpoint, missing, {}

    def inspect(self, event: dict):
        if not self.missing or not isinstance(event, dict):
            return
        names = []
        if self.endpoint == "chat/completions":
            for choice in event.get("choices") or []:
                for call in (choice.get("delta") or {}).get("tool_calls") or []:
                    key = (choice.get("index", 0), call.get("index", 0))
                    fragment = (call.get("function") or {}).get("name", "")
                    if isinstance(fragment, str):
                        self.names[key] = self.names.get(key, "") + fragment
                    names.append(self.names.get(key))
                for call in (choice.get("message") or {}).get("tool_calls") or []:
                    names.append((call.get("function") or {}).get("name"))
        elif self.endpoint == "responses":
            names.append((event.get("item") or {}).get("name"))
            for item in (event.get("response") or {}).get("output") or []:
                if item.get("type") in {"function_call", "custom_tool_call"}:
                    names.append(item.get("name"))
        elif self.endpoint == "messages":
            block = event.get("content_block") or {}
            if block.get("type") == "tool_use":
                names.append(block.get("name"))
        if any(name in self.missing for name in names):
            raise BridgeError("Upstream tried to invoke a compatibility stub. No stub was executed. Use a full native OMP tool roster.", 409, "BridgeStubToolError")


def sse_events(response):
    """Yield individual SSE frames; never buffer the whole model answer."""
    frame, size = [], 0
    while True:
        line = response.readline(MAX_FRAME + 1)
        if not line:
            if frame:
                raise BridgeError("Upstream disconnected in the middle of an SSE frame.", 502, "TruncatedStream")
            return
        size += len(line)
        if size > MAX_FRAME:
            raise BridgeError("SSE frame exceeds 2 MiB.", 502)
        frame.append(line)
        if line in (b"\n", b"\r\n"):
            data = b"\n".join(x[5:].strip() for x in frame if x.startswith(b"data:"))
            yield b"".join(frame), data
            frame, size = [], 0


def is_terminal(event, endpoint: str) -> bool:
    """A terminal event for the endpoint's own wire format."""
    if not isinstance(event, dict):
        return False
    if event.get("type") in {"response.completed", "response.failed", "response.incomplete",
                             "message_stop", "error"} or "error" in event:
        return True
    # OpenAI marks the end with finish_reason; data: [DONE] is optional and not sent by
    # every gateway. Requiring it turns a complete answer into a false TruncatedStream.
    return endpoint == "chat/completions" and any(
        isinstance(c, dict) and c.get("finish_reason") for c in event.get("choices", []) or [])


def error_object(exc: BridgeError, endpoint: str = "chat/completions") -> dict:
    detail = {"message": str(exc), "type": "invalid_request_error", "code": exc.code}
    if endpoint == "responses":
        return {"type": "error", "code": exc.code, "message": str(exc), "param": None}
    if endpoint == "messages":
        return {"type": "error", "error": detail}
    return {"error": detail}


def error_info(status: int, raw: bytes, key: str) -> tuple[BridgeError, float]:
    text = raw.decode("utf-8", "replace")
    code = "UpstreamError"
    try:
        value = load_json(text)
        e = value.get("error", value)
        if isinstance(e, dict):
            code = str(e.get("type") or e.get("code") or code)
            text = str(e.get("message") or text)
    except (ValueError, AttributeError):
        pass
    if key:
        text = text.replace(key, "[REDACTED]")
    text = text[:2000]
    wait = 0
    if status == 429:
        wait = 60
        text += " [Quota/rate limit; NOT a successful model answer. No automatic upstream retry.]"
    if status in {401, 402, 403}:
        text += " [No retry, key rotation, balance fallback or paid-model substitution. Restart only after resolving the cause.]"
    return BridgeError(text, status, code), wait


def quota_reset(until: float, now: float | None = None) -> str:
    left = max(0, int(until - (time.time() if now is None else now)))
    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(round(until)))
    return f"Resets at {stamp} (in {left // 3600}h {left % 3600 // 60}m)"


LANE_NAMES = {"zen": "OpenCode Zen", "go": "OpenCode Go"}


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, catalog, key, token, compat=False, ua_version="1.19.0", connector=None):
        super().__init__(address, Handler)
        self.catalog, self.key, self.token = catalog, key, token
        self.compat, self.ua_version = compat, ua_version
        self.connector = connector or (lambda: http.client.HTTPSConnection("opencode.ai", timeout=120, context=ssl.create_default_context()))
        self.cooldowns, self.lane_cooldowns, self.blocked = {}, {}, {}
        self.lock = threading.Lock()

    def check_model(self, lane: str, model_id: str, endpoint: str):
        with self.lock:
            if lane in self.blocked:
                raise BridgeError("Lane stopped after upstream auth/policy/payment denial. Resolve the cause, then restart the bridge.", 409, "UpstreamLaneStopped")
            until = self.lane_cooldowns.get(lane, 0)
            if time.time() < until:
                raise BridgeError(f"{LANE_NAMES[lane]} free quota is exhausted for every model. {quota_reset(until)}. "
                                  "No upstream call was made.", 409, "FreeQuotaCooldown")
            if time.time() < self.cooldowns.get((lane, model_id), 0):
                raise BridgeError("Model is in local cooldown after HTTP 429. No upstream call was made.", 409, "LocalCooldown")
        if not any(row["lane"] == lane and row["id"] == model_id and row["endpoint"] == endpoint
                   for row in self.catalog.get()["models"]):
            raise BridgeError("Model/endpoint is not on the current verified free-only allowlist. No upstream request was sent.", 403, "PaidOrUnknownModelBlocked")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "OMPFreeBridge/" + VERSION

    def log_message(self, fmt, *args):
        pass  # No request bodies, credentials or URLs in default access logs.

    def setup(self):
        super().setup()
        self.connection.settimeout(120)

    def check_client(self):
        port = self.server.server_port
        if self.headers.get("Host") not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
            raise BridgeError("Invalid local Host header.", 403)
        if self.headers.get("Origin"):
            raise BridgeError("Browser-origin requests are not accepted.", 403)
        bearer = self.headers.get("Authorization", "")
        key = bearer[7:] if bearer.startswith("Bearer ") else self.headers.get("x-api-key", "")
        if not hmac.compare_digest(key.encode(), self.server.token.encode()):
            raise BridgeError("Invalid local proxy token (not the Zen key).", 401)

    def send_json(self, status: int, value):
        raw = encode(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)
        self.close_connection = True

    def fail(self, exc: BridgeError, endpoint: str, started: bool):
        try:
            if not started:
                return self.send_json(exc.status, error_object(exc, endpoint))
            prefix = b"event: error\n" if endpoint in {"responses", "messages"} else b""
            self.wfile.write(prefix + b"data: " + encode(error_object(exc, endpoint)) + b"\n\n")
            self.wfile.flush()
        except OSError:
            pass

    def do_GET(self):
        try:
            self.check_client()
            if self.path == "/health":
                with self.server.lock:
                    blocked = list(self.server.blocked)
                return self.send_json(200, {"ok": True, "version": VERSION,
                    "compat_opencode": self.server.compat, "blocked_lanes": blocked})
            if self.path == "/catalog":
                return self.send_json(200, self.server.catalog.get())
            for lane in BASES:
                if self.path == f"/{lane}/v1/models":
                    ids = dict.fromkeys(r["id"] for r in self.server.catalog.get()["models"] if r["lane"] == lane)
                    data = [{"id": mid, "object": "model", "owned_by": "opencode"} for mid in ids]
                    return self.send_json(200, {"object": "list", "data": data})
            if removed_route(self.path):
                raise BridgeError(REMOVED_ROUTE, 404, "RouteRemoved")
            raise BridgeError("Unknown local endpoint.", 404)
        except BridgeError as exc:
            self.send_json(exc.status, error_object(exc))
        except (OSError, ValueError):
            self.close_connection = True

    def do_POST(self):
        started, conn, finished = False, None, threading.Event()
        response = None
        endpoint = "chat/completions"
        try:
            self.check_client()
            parts = self.path.split("/", 3)
            if removed_route(self.path):
                # Answer in the old provider's own protocol envelope so OMP shows the text.
                endpoint = parts[3] if len(parts) == 4 and parts[3] in APIS else endpoint
                raise BridgeError(REMOVED_ROUTE, 404, "RouteRemoved")
            if len(parts) != 4 or parts[1] not in BASES or parts[2] != "v1" or parts[3] not in APIS:
                raise BridgeError("Unknown local inference endpoint.", 404)
            lane, endpoint = parts[1], parts[3]
            if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Encoding"):
                raise BridgeError("Chunked/compressed request bodies are not supported.")
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or not lengths[0].isdigit():
                raise BridgeError("One Content-Length is required.", 411)
            length = int(lengths[0])
            if not 0 < length <= MAX_BODY:
                raise BridgeError("Request body must be 1 byte to 32 MiB.", 413)
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise BridgeError("Incomplete request body.")
            try:
                payload = load_json(raw)
            except (ValueError, UnicodeError) as exc:
                raise BridgeError("Malformed JSON request.") from exc
            if not isinstance(payload, dict) or not isinstance(payload.get("model"), str):
                raise BridgeError("JSON object with a model ID is required.")
            model_id = payload["model"]
            self.server.check_model(lane, model_id, endpoint)
            if lane == "go" and not self.server.key:
                raise BridgeError("OpenCode Go requires an API key. Run bridge.py login.", 401, "GoCredentialsMissing")
            body, headers, missing = prepare(payload, endpoint, {k.lower(): v for k, v in self.headers.items()}, self.server.compat, self.server.ua_version, lane)
            if lane == "go":
                headers["X-Api-Key" if endpoint == "messages" else "Authorization"] = self.server.key if endpoint == "messages" else "Bearer " + self.server.key
            else:
                headers["Authorization"] = "Bearer public"
            headers["Accept"] = "text/event-stream"
            conn = self.server.connector()
            upstream_path = BASES[lane] + "/" + endpoint if lane == "go" else ("/inference/anthropic/v1/messages" if endpoint == "messages" else "/inference/openai/v1/" + endpoint)
            conn.request("POST", upstream_path, body=encode(body), headers=headers)
            # Abort upstream when the local client disconnects, including a long thinking pause.
            def watch_disconnect():
                while not finished.wait(0.25):
                    try:
                        readable, _, _ = select.select([self.connection], [], [], 0)
                        if readable and self.connection.recv(1, socket.MSG_PEEK) == b"":
                            sock = getattr(conn, "sock", None)
                            # HTTPConnection can release its socket reference for Connection: close
                            # while HTTPResponse still owns the live buffered socket.
                            sock = sock or getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
                            if sock is not None:
                                sock.shutdown(socket.SHUT_RDWR)
                            conn.close()
                            return
                    except (OSError, ValueError):
                        # fileno() is -1 once the handler's finally closes the socket.
                        return
            threading.Thread(target=watch_disconnect, daemon=True).start()
            response = conn.getresponse()
            if response.status != 200:
                exc, wait = error_info(response.status, response.read(16384), self.server.key)
                retry = response.getheader("Retry-After", "") if response.status == 429 else ""
                # The free quota is shared by every model of the lane and upstream names its
                # reset in Retry-After (midnight UTC), so honor it in full for the whole lane.
                lane_quota = response.status == 429 and exc.code == "FreeUsageLimitError"
                if lane_quota:
                    wait = max(wait, int(retry)) if retry.isdigit() else wait
                    exc = BridgeError(f"{exc} {quota_reset(time.time() + wait)}.", exc.status, exc.code)
                elif retry.isdigit():
                    # A day-long per-model cooldown can only be cleared by a restart,
                    # which is the one thing this bridge tells users not to do.
                    wait = max(wait, min(int(retry), 3600))
                with self.server.lock:
                    if response.status in {401, 402, 403}:
                        self.server.blocked[lane] = response.status
                    if lane_quota:
                        self.server.lane_cooldowns[lane] = time.time() + wait
                    elif wait:
                        self.server.cooldowns[(lane, model_id)] = time.time() + wait
                raise exc
            if "text/event-stream" not in response.getheader("Content-Type", "").lower():
                raise BridgeError("Expected SSE but received a non-stream response.", 502, "UnexpectedResponse")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            started = True
            guard = StreamGuard(endpoint, missing)
            terminal = False
            for frame, data in sse_events(response):
                if data == b"[DONE]":
                    terminal = True
                elif data:
                    event = load_json(data)
                    guard.inspect(event)
                    terminal = terminal or is_terminal(event, endpoint)
                self.wfile.write(frame)
                self.wfile.flush()
            if not terminal:
                raise BridgeError("Upstream stream ended without a terminal event. Do not treat this as a completed answer.", 502, "TruncatedStream")
        except BridgeError as exc:
            self.fail(exc, endpoint, started)
        except (OSError, ValueError, TypeError, AttributeError, http.client.HTTPException) as exc:
            self.fail(BridgeError("Transport/stream failure (" + type(exc).__name__ + "). No automatic upstream retry.",
                                  502, "BridgeTransportError"), endpoint, started)
        finally:
            finished.set()
            if conn:
                conn.close()
            self.close_connection = True


@contextlib.contextmanager
def local_request(path: str, payload: dict | None = None):
    port = load_json((STATE / "runtime.json").read_bytes())["port"]
    token = read_private(STATE / "local.token")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise BridgeError("Bad runtime port.")
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    try:
        conn.request("POST" if payload is not None else "GET", path,
                     body=encode(payload) if payload is not None else None,
                     headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        yield conn.getresponse()
    finally:
        conn.close()


def probe(rows: list[dict]):
    results = []
    for row in rows:
        payload = {"model": row["id"], "stream": True, "_omp_free_session": "diagnostic-" + secrets.token_hex(16)}
        if row["endpoint"] == "responses":
            payload.update(input="For a coding-agent connectivity check, reply with exactly BRIDGE_OK. Do not invoke tools.", max_output_tokens=1024)
        else:
            payload.update(messages=[{"role": "user", "content": "For a coding-agent connectivity check, reply with exactly BRIDGE_OK. Do not invoke tools."}], max_tokens=1024)
        try:
            with local_request(f'/{row["lane"]}/v1/{row["endpoint"]}', payload) as response:
                status, text, terminal, error, called_tool = response.status, [], False, None, False
                if status == 200:
                    for _, data in sse_events(response):
                        if data == b"[DONE]":
                            terminal = True
                            continue
                        if not data:
                            continue
                        e = load_json(data)
                        if e.get("error") or e.get("type") in {"error", "response.failed", "response.incomplete"}:
                            error = e
                        if e.get("type") == "response.output_text.delta":
                            text.append(e.get("delta", ""))
                        elif e.get("type") == "content_block_delta":
                            text.append(e.get("delta", {}).get("text", ""))
                        for c in e.get("choices", []):
                            text.append(c.get("delta", {}).get("content") or "")
                            called_tool |= bool(c.get("delta", {}).get("tool_calls"))
                        called_tool |= (e.get("item") or {}).get("type") == "function_call" or (e.get("content_block") or {}).get("type") == "tool_use"
                        terminal |= is_terminal(e, row["endpoint"]) and not error
                    passed = terminal and not error and not called_tool and "".join(text).strip() == "BRIDGE_OK"
                    result = "MODEL_ANSWER_CONFIRMED" if passed else "NO_VERIFIED_ANSWER"
                else:
                    error = load_json(response.read(16384))
                    result = "QUOTA_LIMIT_NOT_SUCCESS" if status == 429 else "FAILED"
                item = {"lane": row["lane"], "model": row["id"], "http": status, "result": result}
                if error:
                    item["error"] = error
                results.append(item)
                print(json.dumps(item, ensure_ascii=False))
                if status in {401, 402, 403, 409, 429}:
                    break
        except (OSError, ValueError, BridgeError, http.client.HTTPException) as exc:
            results.append({"lane": row["lane"], "model": row["id"], "result": "TRANSPORT_FAILURE", "error": type(exc).__name__})
            print(json.dumps(results[-1]))
            break
    save_private(STATE / "probe-results.json", encode({"checked_at": time.time(), "results": results}))
    return 0 if results and all(r["result"] == "MODEL_ANSWER_CONFIRMED" for r in results) and len(results) == len(rows) else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("login", help="Read a Zen/Go API key with hidden input; save mode 0600")
    sub.add_parser("catalog", help="Fetch and display the zero-cost allowlist and excluded-model reasons; performs no inference")
    serve = sub.add_parser("serve", help="Run a foreground, loopback-only proxy")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--compat-opencode", action="store_true", help="Explicitly enable unofficial PR-style OpenCode identity and tool padding; account-policy risk")
    serve.add_argument("--opencode-version", default="1.19.0", help="OpenCode client UA version")
    sub.add_parser("doctor", help="Check local proxy health; does not infer")
    smoke = sub.add_parser("probe", help="Opt-in live check: accepts only a real completed model answer as success")
    pick = smoke.add_mutually_exclusive_group(required=True)
    pick.add_argument("--model", help="Exact ID in the current free allowlist")
    pick.add_argument("--all", action="store_true", help="One sequential call per approved model; stop on policy/quota denial")
    smoke.add_argument("--lane", choices=tuple(BASES), default="zen")
    args = parser.parse_args(argv)
    if args.command == "login":
        key = getpass.getpass("Zen/Go API key (not a Console st- token): ").strip()
        if not key or "\n" in key or "\r" in key or key.startswith("st-"):
            raise BridgeError("A nonempty Zen/Go API key is required.")
        save_private(STATE / "zen.key", key.encode())
        print("Saved locally with mode 0600. No network request was made.")
        return 0
    if args.command == "catalog":
        print(json.dumps(Catalog().get(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "doctor":
        with local_request("/health") as response:
            print(response.read(16384).decode())
            return 0 if response.status == 200 else 1
    if args.command == "probe":
        with local_request("/catalog") as response:
            if response.status != 200:
                raise BridgeError("Proxy catalog unavailable.", 503)
            rows = load_json(response.read(MAX_BODY))["models"]
        if not args.all:
            rows = [r for r in rows if r["lane"] == args.lane and r["id"] == args.model]
        if not rows:
            raise BridgeError("No matching approved free models. Run catalog for exclusion reasons.")
        return probe(rows)
    if not 1 <= args.port <= 65535 or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", args.opencode_version):
        raise BridgeError("Invalid port or OpenCode version.")
    key, token = key_from_user(required=False), token_from_user()
    catalog = Catalog()
    snapshot = catalog.get()
    if not snapshot["models"]:
        raise BridgeError("No verified zero-cost models. Inspect catalog; no inference allowed.", 503)
    server = BridgeServer(("127.0.0.1", args.port), catalog, key, token, args.compat_opencode, args.opencode_version)
    save_private(STATE / "runtime.json", encode({"port": args.port, "version": VERSION, "pid": os.getpid()}))
    print(f"OMP free-only bridge: 127.0.0.1:{args.port}; {len(snapshot['models'])} verified zero-cost routes.", flush=True)
    print("No OpenCode CLI. No inference retries. No paid fallback. Ctrl+C stops the proxy.", flush=True)
    if args.compat_opencode:
        print("UNOFFICIAL COMPATIBILITY ENABLED: OpenCode-shaped identity and guarded tool padding. Account-policy risk; read docs/ДОСЛІДЖЕННЯ.md.", flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BridgeError, OSError, ValueError, http.client.HTTPException) as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
