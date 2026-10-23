#!/usr/bin/env python3
"""OMP OpenCode free-only bridge. Python 3.10+, standard library only.
No OpenCode CLI, key rotation or paid fallback. Inference never runs subprocesses;
the local doctor command can query Docker for container memory and uptime.
Zen starts direct, then tries fixed WARP exits in order after a quota refusal.
See docs/ДОСЛІДЖЕННЯ.md before enabling --compat-opencode.
"""
from __future__ import annotations

import argparse
import contextlib
import concurrent.futures
import copy
from datetime import datetime
import getpass
import hashlib
import hmac
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import select
import socket
import ssl
import subprocess
import sys
import threading
import time
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

VERSION = "1.9.0"
MAX_ZEN_PROXIES = 10
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


def parse_proxy(value: str) -> tuple[str, int]:
    """One explicit HTTP CONNECT proxy; no credentials or URL routing."""
    try:
        if any(ord(c) <= 32 or ord(c) == 127 for c in value):
            raise ValueError
        u = urlsplit(value)
        if (u.scheme != "http" or not u.hostname or u.port is None
                or not 1 <= u.port <= 65535 or u.username is not None
                or u.password is not None or u.path not in {"", "/"}
                or "?" in value or "#" in value):
            raise ValueError
        return u.hostname, u.port
    except ValueError as exc:
        raise BridgeError("Proxy must be http://HOST:PORT, without credentials, path, query or fragment.") from exc


def configured_proxies(values=None):
    if values is None:
        try:
            values = load_json((STATE / "zen-proxies.json").read_bytes())
        except FileNotFoundError:
            values = []
        except (OSError, ValueError) as exc:
            raise BridgeError("Cannot read zen-proxies.json. Fix the file before starting; no direct fallback.") from exc
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise BridgeError("zen-proxies.json must contain a JSON array of HTTP proxy URLs.")
    proxies = tuple(parse_proxy(value) for value in values)
    if len(proxies) > MAX_ZEN_PROXIES or len(set(proxies)) != len(proxies):
        raise BridgeError(f"Configure at most {MAX_ZEN_PROXIES} distinct Zen proxy endpoints.")
    return proxies


def upstream_connection(proxy: tuple[str, int] | None = None):
    conn = http.client.HTTPSConnection(*(proxy or ("opencode.ai", 443)),
                                       timeout=120, context=ssl.create_default_context())
    if proxy:
        conn.set_tunnel("opencode.ai", 443)
    return conn


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
        text += " [Quota/rate limit; NOT a successful model answer.]"
    if status in {401, 402, 403}:
        text += " [No retry, key rotation, balance fallback or paid-model substitution. Restart only after resolving the cause.]"
    return BridgeError(text, status, code), wait


def quota_reset(until: float, now: float | None = None) -> str:
    left = max(0, int(until - (time.time() if now is None else now)))
    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(round(until)))
    return f"Resets at {stamp} (in {left // 3600}h {left % 3600 // 60}m)"


LANE_NAMES = {"zen": "OpenCode Zen", "go": "OpenCode Go"}


class RouteStats:
    """Cumulative Zen counters, persisted atomically without prompts or credentials."""
    FIELDS = {"inference": ("attempts", "successes", "http_429", "transport_errors"),
              "ip_checks": ("attempts", "successes", "errors")}

    @classmethod
    def empty_route(cls):
        return {group: dict.fromkeys(fields, 0) for group, fields in cls.FIELDS.items()}

    def __init__(self, path=None, routes=()):
        self.path = Path(path) if path is not None else None
        self.lock = threading.Lock()
        self.persistence_error = None
        now = time.time()
        self.data = {"schema": 1, "started_at": now, "updated_at": now, "routes": {}}
        if self.path is not None and self.path.exists():
            try:
                data = load_json(read_private(self.path))
                if (data.get("schema") != 1 or not isinstance(data.get("routes"), dict)
                        or any(type(data.get(key)) not in (int, float) or not math.isfinite(data[key])
                               or data[key] < 0 for key in ("started_at", "updated_at"))):
                    raise ValueError
                for name, row in data["routes"].items():
                    if not isinstance(name, str) or not isinstance(row, dict):
                        raise ValueError
                    for group, fields in self.FIELDS.items():
                        if (not isinstance(row.get(group), dict)
                                or any(type(row[group].get(field)) is not int or row[group][field] < 0
                                       for field in fields)):
                            raise ValueError
                self.data = data
            except (BridgeError, OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
                raise BridgeError("Invalid zen-stats.json; refusing to discard saved counters.") from exc
        for name in routes:
            self.data["routes"].setdefault(name, self.empty_route())
        with self.lock:
            self._save()

    def _save(self):
        # Caller holds lock. A telemetry write failure must not replay model work.
        if self.path is not None:
            try:
                save_private(self.path, encode(self.data))
                self.persistence_error = None
            except OSError as exc:
                self.persistence_error = type(exc).__name__

    def record(self, route, group, field):
        if group not in self.FIELDS or field not in self.FIELDS[group]:
            raise ValueError("Unknown route counter")
        with self.lock:
            row = self.data["routes"].setdefault(route, self.empty_route())
            row[group][field] += 1
            self.data["updated_at"] = time.time()
            self._save()

    def snapshot(self, routes=()):
        with self.lock:
            result = copy.deepcopy(self.data)
            result["persistence_error"] = self.persistence_error
        for name in routes:
            result["routes"].setdefault(name, self.empty_route())
        return result


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, catalog, key, token, compat=False, ua_version="1.19.0", connector=None, zen_proxies=(), state=None):
        super().__init__(address, Handler)
        self.catalog, self.key, self.token = catalog, key, token
        self.compat, self.ua_version = compat, ua_version
        self.connector = connector or upstream_connection
        self.zen_proxies = tuple(zen_proxies)
        self.proxy_until = [0.0] * len(self.zen_proxies)
        self.zen_route_quotas = {}
        self.zen_route_ips = {}
        self.zen_route_revisions = {}
        self.zen_last_429 = {}
        self.cooldowns, self.lane_cooldowns, self.blocked = {}, {}, {}
        self.lock = threading.Lock()
        self.direct_transport_until = 0.0
        self.ip_observations = {}
        self.ip_monitor_error = None
        self.ip_monitor_stop = threading.Event()
        self.ip_path = Path(state) / "zen-ips.json" if state is not None else None
        self.cooldown_path = Path(state) / "zen-cooldowns.json" if state is not None else None
        try:
            self.load_zen_cooldowns()
            self.stats = RouteStats(Path(state) / "zen-stats.json" if state is not None else None,
                                    ["direct", *(f"{host}:{port}" for host, port in self.zen_proxies)])
            if self.ip_path is not None and self.ip_path.exists():
                try:
                    data = load_json(read_private(self.ip_path))
                    if data.get("schema") != 1 or not isinstance(data.get("routes"), dict):
                        raise ValueError
                    for record in data["routes"].values():
                        if not isinstance(record, dict) or type(record.get("changes", 0)) is not int:
                            raise ValueError
                        if record.get("ip"):
                            ipaddress.ip_address(record["ip"])
                    self.ip_observations = data["routes"]
                except (BridgeError, OSError, ValueError, TypeError, AttributeError):
                    self.ip_monitor_error = "InvalidSavedIPSnapshot"
        except Exception:
            self.server_close()
            raise

    def refresh_zen_ips(self):
        """Confirm exit IPs; clear only the changed WARP's previous cooldowns."""
        routes = [("direct", None), *((f"{host}:{port}", (host, port)) for host, port in self.zen_proxies)]
        def measure(item):
            name, proxy = item
            conn = None
            self.stats.record(name, "ip_checks", "attempts")
            try:
                conn = upstream_connection(proxy)
                conn.timeout = 15
                conn.request("GET", "/cdn-cgi/trace", headers={"Accept-Encoding": "identity"})
                response = conn.getresponse()
                raw = response.read(16385)
                if response.status != 200 or len(raw) > 16384:
                    raise ValueError("trace response unavailable")
                values = dict(line.split("=", 1) for line in raw.decode().splitlines() if "=" in line)
                address = str(ipaddress.ip_address(values["ip"]))
                if proxy is not None and values.get("warp") not in {"on", "plus"}:
                    raise ValueError("trace does not confirm WARP")
                self.stats.record(name, "ip_checks", "successes")
                return name, {"ip": address, "warp": values.get("warp"), "observed_at": time.time(), "error": None}
            except (OSError, ValueError, KeyError, http.client.HTTPException) as exc:
                self.stats.record(name, "ip_checks", "errors")
                return name, {"error": type(exc).__name__, "attempted_at": time.time()}
            finally:
                if conn:
                    conn.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(routes)) as pool:
            observations = list(pool.map(measure, routes))
        with self.lock:
            proxies = {self.route_key(i): i for i in range(len(self.zen_proxies))}
            before = (self.zen_route_quotas.copy(), self.proxy_until[:], self.zen_route_ips.copy(),
                      self.zen_route_revisions.copy())
            updated_observations = {}
            for name, observed in observations:
                previous = self.ip_observations.get(name, {})
                updated = {**previous, **observed}
                changed = bool(observed.get("ip") and previous.get("ip") and observed["ip"] != previous["ip"])
                if changed:
                    updated.update(previous_ip=previous["ip"], changes=previous.get("changes", 0) + 1,
                                   changed_at=observed["observed_at"])
                else:
                    updated.setdefault("changes", 0)
                if name in proxies and observed.get("ip"):
                    # The binding lives with the deadlines, so a stale/missing IP
                    # history file cannot clear a newer refusal after restart.
                    known_ip = self.zen_route_ips.get(name, previous.get("ip"))
                    if known_ip and known_ip != observed["ip"]:
                        self.zen_route_quotas.pop(name, None)
                        self.proxy_until[proxies[name]] = 0.0
                        self.zen_route_revisions[name] = self.zen_route_revisions.get(name, 0) + 1
                        updated["cooldown_reset_at"] = observed["observed_at"]
                    self.zen_route_ips[name] = observed["ip"]
                updated_observations[name] = updated
            if self.zen_route_ips != before[2]:
                try:
                    self.save_zen_cooldowns()
                except OSError:
                    (self.zen_route_quotas, self.proxy_until, self.zen_route_ips,
                     self.zen_route_revisions) = before
                    raise
            self.ip_observations = updated_observations
            self.ip_monitor_error = None
            if self.ip_path is not None:
                save_private(self.ip_path, encode({"schema": 1, "routes": self.ip_observations}))

    def start_ip_monitor(self):
        def monitor():
            while not self.ip_monitor_stop.is_set():
                try:
                    self.refresh_zen_ips()
                except Exception as exc:
                    with self.lock:
                        self.ip_monitor_error = type(exc).__name__
                self.ip_monitor_stop.wait(60)
        threading.Thread(target=monitor, name="zen-ip-monitor", daemon=True).start()

    def cool_direct_transport(self, seconds=30):
        with self.lock:
            self.direct_transport_until = max(self.direct_transport_until, time.time() + seconds)
            self.save_zen_cooldowns()

    def load_zen_cooldowns(self):
        if self.cooldown_path is None or not self.cooldown_path.exists():
            return
        try:
            data = load_json(read_private(self.cooldown_path))
            if data.get("schema") == 1:
                # Legacy direct_until is retained only for the direct route.
                # Old per-proxy pauses may include quotas; never discard them on upgrade.
                direct, proxies = data["direct_until"], data["proxies"]
                quotas = {"direct": direct, **proxies}
                transport = {}
                direct_transport = 0.0
                ips = {}
                last_429 = {}
            elif data.get("schema") == 2:
                quotas, transport = data["quotas"], data["transport"]
                direct_transport = data.get("direct_transport_until", 0.0)
                ips = data.get("ips", {})
                last_429 = data.get("last_429", {})
            else:
                raise ValueError
            if (not isinstance(quotas, dict) or not isinstance(transport, dict)
                    or any(type(value) not in (int, float) or not math.isfinite(value) or value < 0
                           for value in [direct_transport, *quotas.values(), *transport.values()])):
                raise ValueError
            if not isinstance(ips, dict) or any(not isinstance(name, str) or not isinstance(ip, str)
                                              for name, ip in ips.items()):
                raise ValueError
            for ip in ips.values():
                ipaddress.ip_address(ip)
            if not isinstance(last_429, dict):
                raise ValueError
            for route, record in last_429.items():
                if (not isinstance(route, str) or not isinstance(record, dict)
                        or type(record.get("received_at")) not in (int, float)
                        or not math.isfinite(record["received_at"]) or record["received_at"] < 0
                        or (record.get("retry_after") is not None and not isinstance(record["retry_after"], str))
                        or (record.get("code") is not None and not isinstance(record["code"], str))
                        or type(record.get("cooldown_applied")) is not bool):
                    raise ValueError
                until = record.get("cooldown_until")
                if until is not None and (type(until) not in (int, float) or not math.isfinite(until) or until < 0):
                    raise ValueError
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise BridgeError("Invalid zen-cooldowns.json; refusing to discard known quota cooldowns.") from exc
        self.zen_route_quotas = quotas
        self.zen_route_ips = ips
        self.zen_last_429 = last_429
        self.direct_transport_until = direct_transport
        self.proxy_until = [transport.get(f"{host}:{port}", 0.0) for host, port in self.zen_proxies]

    def save_zen_cooldowns(self):
        # Caller holds lock. Persist by endpoint, so adding/reordering slots keeps known limits.
        if self.cooldown_path is not None:
            save_private(self.cooldown_path, encode({"schema": 2,
                "quotas": self.zen_route_quotas,
                "ips": self.zen_route_ips,
                "last_429": self.zen_last_429,
                "direct_transport_until": self.direct_transport_until,
                "transport": {f"{host}:{port}": until
                            for (host, port), until in zip(self.zen_proxies, self.proxy_until)}}))

    def record_zen_429(self, route, received_at, retry_after):
        # Record the actual response even if reading its body fails or its old IP
        # revision can no longer put the route on pause. Caller supplies no body.
        with self.lock:
            previous = self.zen_last_429.get(route)
            if previous and previous["received_at"] > received_at:
                return
            self.zen_last_429[route] = {"received_at": received_at, "retry_after": retry_after,
                                       "code": None, "cooldown_applied": False, "cooldown_until": None}
            self.save_zen_cooldowns()

    def route_cooldown_info(self, route, transport, now):
        # Caller holds lock; history and deadlines must come from one snapshot.
        quota = self.zen_route_quotas.get(route, 0)
        until = max(quota, transport)
        return {"cooldown_seconds": max(0, math.ceil(until - now)),
                "quota_cooldown_seconds": max(0, math.ceil(quota - now)),
                "transport_cooldown_seconds": max(0, math.ceil(transport - now)),
                "cooldown_until": until if until > now else None,
                "quota_cooldown_until": quota or None,
                "transport_cooldown_until": transport or None,
                "last_429": copy.deepcopy(self.zen_last_429.get(route))}

    def route_key(self, proxy):
        return "direct" if proxy is None else "{}:{}".format(*self.zen_proxies[proxy])

    def select_proxy(self, lane: str, session: str, attempted=()):
        if lane != "zen":
            return None
        with self.lock:
            if lane in self.blocked:
                raise BridgeError("Lane stopped after upstream auth/policy/payment denial. Resolve the cause, then restart the bridge.", 409, "UpstreamLaneStopped")
            now = time.time()
            deadlines = [(None, max(self.direct_transport_until, self.zen_route_quotas.get("direct", 0.0)))]
            deadlines.extend((index, max(until, self.zen_route_quotas.get(self.route_key(index), 0.0)))
                             for index, until in enumerate(self.proxy_until))
            for index, until in deadlines:
                if index not in attempted and until <= now:
                    return index
            if any(until <= now for _, until in deadlines):
                raise BridgeError("All available Zen routes were already tried for this request.",
                                  503, "ZenRoutesExhausted")
            until = min(until for _, until in deadlines)
            raise BridgeError(f"All configured Zen routes are in cooldown. Retry in {math.ceil(until - now)}s. "
                              f"{quota_reset(until)}. No additional upstream call was made.",
                              503, "ZenProxyCooldown")

    def cool_proxy(self, index, seconds: float, revision=None):
        if index is not None:
            with self.lock:
                if revision is not None and revision != self.zen_route_revisions.get(self.route_key(index), 0):
                    return
                self.proxy_until[index] = max(self.proxy_until[index], time.time() + seconds)
                self.save_zen_cooldowns()

    def connect_upstream(self, lane: str, proxy=None):
        if lane == "zen" and proxy is not None:
            return upstream_connection(self.zen_proxies[proxy])
        return self.connector()

    def check_model(self, lane: str, model_id: str, endpoint: str):
        with self.lock:
            if lane in self.blocked:
                raise BridgeError("Lane stopped after upstream auth/policy/payment denial. Resolve the cause, then restart the bridge.", 409, "UpstreamLaneStopped")
            until = self.lane_cooldowns.get(lane, 0)
            if lane != "zen" and time.time() < until:
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

    def end_headers(self):
        proxy = getattr(self, "zen_proxy_index", None)
        if hasattr(self, "zen_proxy_index"):
            self.send_header("X-OMP-Zen-Route", "direct" if proxy is None else "warp")
        if proxy is not None:
            self.send_header("X-OMP-Zen-Proxy", str(proxy + 1))
        super().end_headers()

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
                    now = time.time()
                    proxies = []
                    for i, until in enumerate(self.server.proxy_until):
                        route = self.server.route_key(i)
                        proxies.append({"id": i + 1, "endpoint": route,
                                        **self.server.route_cooldown_info(route, until, now)})
                    direct = self.server.route_cooldown_info("direct", self.server.direct_transport_until, now)
                    addresses = copy.deepcopy(self.server.ip_observations)
                    groups = {}
                    for name, observed in addresses.items():
                        if observed.get("ip") and not observed.get("error"):
                            groups.setdefault(observed["ip"], []).append(name)
                    duplicates = {ip: names for ip, names in groups.items() if len(names) > 1}
                    monitor_error = self.server.ip_monitor_error
                return self.send_json(200, {"ok": True, "version": VERSION,
                    "compat_opencode": self.server.compat, "zen_proxy": bool(proxies),
                    "zen_routing": "direct-first", "zen_quota_failover": bool(proxies),
                    "zen_route_order": ["direct", *(f"warp-{i + 1}" for i in range(len(proxies)))],
                    "zen_direct": direct,
                    "zen_proxies": proxies, "blocked_lanes": blocked,
                    "ip_observations": addresses, "duplicate_ips": duplicates, "ip_monitor_error": monitor_error,
                    "zen_stats": self.server.stats.snapshot(["direct", *(self.server.route_key(i) for i in range(len(proxies)))])})
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
        proxy = None
        lane = None
        upstream_io = False
        attempt_counted = False
        revision = None
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
            upstream_path = BASES[lane] + "/" + endpoint if lane == "go" else ("/inference/anthropic/v1/messages" if endpoint == "messages" else "/inference/openai/v1/" + endpoint)
            wire_body = encode(body)
            attempted = set()
            # Abort upstream when the local client disconnects, including a long thinking pause.
            def watch_disconnect():
                while not finished.wait(0.25):
                    try:
                        readable, _, _ = select.select([self.connection], [], [], 0)
                        if readable and self.connection.recv(1, socket.MSG_PEEK) == b"":
                            finished.set()
                            sock = getattr(conn, "sock", None)
                            # HTTPConnection can release its socket reference for Connection: close
                            # while HTTPResponse still owns the live buffered socket.
                            sock = sock or getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
                            if sock is not None:
                                sock.shutdown(socket.SHUT_RDWR)
                            if conn:
                                conn.close()
                            return
                    except (OSError, ValueError):
                        # fileno() is -1 once the handler's finally closes the socket.
                        return
            threading.Thread(target=watch_disconnect, daemon=True).start()
            while not finished.is_set():
                self.server.check_model(lane, model_id, endpoint)
                proxy = self.server.select_proxy(lane, headers["x-opencode-session"], attempted)
                if lane == "zen":
                    self.zen_proxy_index = proxy
                    self.server.stats.record(self.server.route_key(proxy), "inference", "attempts")
                    attempt_counted = True
                with self.server.lock:
                    revision = self.server.zen_route_revisions.get(self.server.route_key(proxy), 0)
                attempted.add(proxy)
                upstream_io = True
                conn = self.server.connect_upstream(lane, proxy)
                conn.request("POST", upstream_path, body=wire_body, headers=headers)
                response = conn.getresponse()
                received_at = time.time()
                if lane == "zen" and response.status == 429:
                    self.server.stats.record(self.server.route_key(proxy), "inference", "http_429")
                    self.server.record_zen_429(self.server.route_key(proxy), received_at,
                                               response.getheader("Retry-After"))
                if response.status == 200:
                    break
                exc, wait = error_info(response.status, response.read(16384), self.server.key)
                retry = response.getheader("Retry-After", "") if response.status == 429 else ""
                # Honor the full quota reset, scoped to this exit when a pool is configured.
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
                    if lane == "zen" and response.status == 429:
                        route = self.server.route_key(proxy)
                        applied = revision == self.server.zen_route_revisions.get(route, 0)
                        if applied:
                            self.server.zen_route_quotas[route] = max(self.server.zen_route_quotas.get(route, 0), time.time() + wait)
                        record = self.server.zen_last_429.get(route)
                        if record and record["received_at"] == received_at:
                            record.update(code=exc.code, cooldown_applied=applied,
                                          cooldown_until=self.server.zen_route_quotas[route] if applied else None)
                        self.server.save_zen_cooldowns()
                    elif lane_quota:
                        self.server.lane_cooldowns[lane] = max(self.server.lane_cooldowns.get(lane, 0), time.time() + wait)
                    elif proxy is None and wait:
                        self.server.cooldowns[(lane, model_id)] = time.time() + wait
                if 500 <= response.status <= 599:
                    self.server.cool_proxy(proxy, 30, revision)
                    if lane == "zen" and proxy is None:
                        self.server.cool_direct_transport()
                if lane == "zen" and lane_quota and self.server.zen_proxies:
                    # Only an explicit quota refusal before SSE permits another POST.
                    # Keep the exact body/session, close this connection, and recheck
                    # model admission and lane state before selecting the next exit.
                    conn.close()
                    conn = response = None
                    upstream_io = False
                    continue
                raise exc
            if finished.is_set():
                return
            if "text/event-stream" not in response.getheader("Content-Type", "").lower():
                raise BridgeError("Expected SSE but received a non-stream response.", 502, "UnexpectedResponse")
            upstream_io = False
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            started = True
            guard = StreamGuard(endpoint, missing)
            terminal = False
            stream_failed = False
            upstream_io = True
            for frame, data in sse_events(response):
                if data == b"[DONE]":
                    terminal = True
                elif data:
                    event = load_json(data)
                    guard.inspect(event)
                    terminal = terminal or is_terminal(event, endpoint)
                    stream_failed |= isinstance(event, dict) and ("error" in event or event.get("type") in {
                        "error", "response.failed", "response.incomplete"})
                    if endpoint == "chat/completions" and isinstance(event, dict):
                        stream_failed |= any(isinstance(choice, dict) and choice.get("finish_reason") in {
                            "error", "network_error", "content_filter", "insufficient_system_resource"}
                            for choice in event.get("choices") or [])
                upstream_io = False
                self.wfile.write(frame)
                self.wfile.flush()
                upstream_io = True
            if not terminal:
                raise BridgeError("Upstream stream ended without a terminal event. Do not treat this as a completed answer.", 502, "TruncatedStream")
            if lane == "zen" and not stream_failed and not finished.is_set():
                self.server.stats.record(self.server.route_key(proxy), "inference", "successes")
        except BridgeError as exc:
            if not finished.is_set() and exc.code in {"TruncatedStream", "UnexpectedResponse"}:
                if lane == "zen" and attempt_counted:
                    self.server.stats.record(self.server.route_key(proxy), "inference", "transport_errors")
                self.server.cool_proxy(proxy, 30, revision)
                if lane == "zen" and proxy is None:
                    self.server.cool_direct_transport()
            self.fail(exc, endpoint, started)
        except (OSError, ValueError, TypeError, AttributeError, http.client.HTTPException) as exc:
            if upstream_io and not finished.is_set():
                if lane == "zen" and attempt_counted:
                    self.server.stats.record(self.server.route_key(proxy), "inference", "transport_errors")
                self.server.cool_proxy(proxy, 30, revision)
                if lane == "zen" and proxy is None:
                    self.server.cool_direct_transport()
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


def doctor_table(headers, rows, numeric=()):
    rows = [[str(value) for value in row] for row in rows]
    widths = [max(len(header), *(len(row[i]) for row in rows)) for i, header in enumerate(headers)]
    def line(row, headings=False):
        return "  ".join(value.rjust(width) if i in numeric and not headings else value.ljust(width)
                         for i, (value, width) in enumerate(zip(row, widths))).rstrip()
    return "\n".join([line(headers, True), line(["-" * width for width in widths]),
                      *(line(row) for row in rows)])


def doctor_memory(health):
    """Read-only, on-demand Docker metrics. Never called by the HTTP server."""
    result = {"routes": {}, "uptime_seconds": {}, "error": None}
    local = {}
    for proxy in health.get("zen_proxies") or []:
        endpoint = proxy.get("endpoint") or ""
        host, _, port = endpoint.rpartition(":")
        if host in {"127.0.0.1", "localhost", "::1"} and port.isdigit():
            local[endpoint] = (host, port)
    if not local:
        return result
    command = ["docker"]
    if sys.platform == "darwin" and not (os.environ.get("DOCKER_CONTEXT") or os.environ.get("DOCKER_HOST")):
        command += ["--context", "colima"]
    def run(args, timeout):
        return subprocess.run(command + args, capture_output=True, text=True, check=True, timeout=timeout).stdout
    try:
        ids = run(["ps", "--filter", "label=com.docker.compose.project=omp-warp", "--format", "{{.ID}}"], 3).split()
        if not ids:
            result["error"] = "Контейнери WARP не запущені в Docker."
            return result
        if any(not re.fullmatch(r"[0-9a-f]{12,64}", value) for value in ids):
            raise ValueError("Invalid container IDs")
        containers = load_json(run(["inspect", *ids], 3))
        by_name = {}
        sampled_at = time.time()
        for container in containers:
            name = container["Name"].lstrip("/")
            labels = container.get("Config", {}).get("Labels") or {}
            if not re.fullmatch(r"warp-[a-j]", labels.get("com.docker.compose.service", "")):
                continue
            bindings = (container.get("NetworkSettings", {}).get("Ports") or {}).get("8888/tcp") or []
            endpoints = []
            for endpoint, (host, port) in local.items():
                for binding in bindings:
                    bound = binding.get("HostIp")
                    matches = (bound == host or host == "localhost" and bound in {"127.0.0.1", "::1", "0.0.0.0", "::"}
                               or host == "127.0.0.1" and bound == "0.0.0.0" or host == "::1" and bound == "::")
                    if binding.get("HostPort") == port and matches:
                        endpoints.append(endpoint)
                        break
            by_name[name] = endpoints
            state = container.get("State") or {}
            if state.get("Running") is True:
                try:
                    started = datetime.fromisoformat(state.get("StartedAt", "").replace("Z", "+00:00"))
                    if started.tzinfo is not None and started.year > 1 and 0 < started.timestamp() <= sampled_at:
                        for endpoint in endpoints:
                            result["uptime_seconds"][endpoint] = int(sampled_at - started.timestamp())
                except (ValueError, TypeError, AttributeError, OverflowError, OSError):
                    pass
        raw = run(["stats", "--no-stream", "--format", "{{json .}}", *ids], 8)
        units = {"B": 1, **{unit + "B": 1000 ** power for power, unit in enumerate("KMGT", 1)},
                 **{unit + "IB": 1024 ** power for power, unit in enumerate("KMGT", 1)}}
        for line in raw.splitlines():
            row = load_json(line)
            if row.get("PIDs") in ("0", 0) or row.get("MemUsage", "").replace(" ", "") == "0B/0B":
                for endpoint in by_name.get(row.get("Name"), []):
                    result["uptime_seconds"].pop(endpoint, None)
                continue
            used = row.get("MemUsage", "").split("/", 1)[0].strip()
            match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)", used)
            if not match or match[2].upper() not in units:
                continue
            value = float(match[1]) * units[match[2].upper()]
            if not math.isfinite(value):
                continue
            for endpoint in by_name.get(row.get("Name"), []):
                result["routes"][endpoint] = int(round(value))
    except FileNotFoundError:
        result["error"] = "Docker не знайдено."
    except subprocess.TimeoutExpired:
        result["error"] = "Docker не відповів вчасно."
    except (OSError, subprocess.CalledProcessError):
        result["error"] = "Не вдалося отримати статистику Docker."
    except (ValueError, TypeError, KeyError, AttributeError):
        result["error"] = "Некоректна відповідь Docker."
    return result


def doctor_report(health, memory=None):
    stats = health.get("zen_stats") or {}
    routes = stats.get("routes") or {}
    entries = [("direct", "direct", health.get("zen_direct") or {})]
    entries.extend((f"WARP {proxy['id']}", proxy.get("endpoint"), proxy)
                   for proxy in health.get("zen_proxies") or [])
    lines = [f"OMP Bridge {health.get('version', '?')} — " + ("працює" if health.get("ok") else "не готовий")]
    if stats.get("started_at") is not None:
        lines.append("Облік від: " + time.strftime("%d.%m.%Y %H:%M:%S %Z", time.localtime(stats["started_at"])))
    else:
        lines.append("Статистика недоступна: оновіть запущений брідж.")
    if stats.get("persistence_error"):
        lines.append(f"Помилка збереження: {stats['persistence_error']}. Нові числа поки лише в пам'яті.")
    elif stats:
        lines.append("Збереження статистики: гаразд")

    def duration(seconds):
        minutes = max(1, math.ceil(seconds / 60))
        return f"{minutes // 60}г {minutes % 60:02}хв" if minutes >= 60 else f"{minutes}хв"

    def uptime(seconds):
        days, remainder = divmod(seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes = remainder // 60
        if days:
            return f"{days}д {hours:02}г {minutes:02}хв"
        if hours:
            return f"{hours}г {minutes:02}хв"
        return f"{minutes}хв" if minutes else f"{seconds}с"

    def timestamp(value):
        if value is None:
            return "н/д"
        try:
            return datetime.fromtimestamp(value).astimezone().isoformat(sep=" ", timespec="seconds")
        except (ValueError, TypeError, OverflowError, OSError):
            return "н/д"

    def printable(value):
        # Retry-After/error codes come from upstream; never emit terminal controls.
        return "".join(char if char.isprintable() else "?" for char in str(value))[:120]

    model_rows, ip_rows, pause_rows = [], [], []
    model_totals, ip_totals = [0] * 4, [0] * 3
    missing = False
    observations = health.get("ip_observations") or {}
    memory = memory or {"routes": {}, "error": None}
    measured = []
    for label, endpoint, state in entries:
        row = routes.get(endpoint)
        missing |= row is None
        inference = [row["inference"][field] if row else "—" for field in RouteStats.FIELDS["inference"]]
        checks = [row["ip_checks"][field] if row else "—" for field in RouteStats.FIELDS["ip_checks"]]
        if row:
            model_totals = [total + count for total, count in zip(model_totals, inference)]
            ip_totals = [total + count for total, count in zip(ip_totals, checks)]
        quota, transport = state.get("quota_cooldown_seconds", 0), state.get("transport_cooldown_seconds", 0)
        if "zen" in (health.get("blocked_lanes") or []):
            status = "Канал зупинено"
        elif max(quota, transport) > 0:
            status = ("Квота: " if quota >= transport else "Мережа: ") + duration(max(quota, transport))
        else:
            status = "Без паузи"
        ram = "—" if endpoint == "direct" else "н/д"
        if endpoint != "direct" and endpoint in memory["routes"]:
            value = memory["routes"][endpoint]
            ram = f"{value / 1024 ** 2:.1f}"
            measured.append(value)
        running = "—" if endpoint == "direct" else "н/д"
        if endpoint != "direct" and endpoint in memory.get("uptime_seconds", {}):
            running = uptime(memory["uptime_seconds"][endpoint])
        model_rows.append([label, *inference, ram, running, status])
        observed = observations.get(endpoint) or {}
        ip_rows.append([label, *checks, observed.get("ip") or "—"])
        last = state.get("last_429")
        last_at = timestamp(last.get("received_at")) if last else "н/д"
        retry = "н/д" if last is None else ("немає" if last.get("retry_after") is None
                                             else printable(last["retry_after"]) or '""')
        paused = max(quota, transport) > 0
        end = timestamp(state.get("cooldown_until")) if paused else "—"
        if quota > 0 and quota >= transport:
            if (last and last.get("cooldown_applied")
                    and last.get("cooldown_until") == state.get("quota_cooldown_until")):
                reason = "429 " + printable(last.get("code") or "тип н/д")
            else:
                reason = "Квота; історія 429 н/д"
        elif transport > 0:
            reason = "Транспортна помилка"
        else:
            reason = "Без паузи"
        if "zen" in (health.get("blocked_lanes") or []):
            reason = "Канал зупинено"
        pause_rows.append([label, last_at, retry, end, reason])
    ram_total = f"{sum(measured) / 1024 ** 2:.1f}" if measured else "н/д"
    if measured and len(measured) < len(entries) - 1:
        ram_total += "+"
    model_rows.append(["Разом", *(model_totals if not missing else ["—"] * 4), ram_total, "—", ""])
    ip_rows.append(["Разом", *(ip_totals if not missing else ["—"] * 3), ""])
    lines.extend(["", "Модельні запити",
                  doctor_table(["Маршрут", "Спроби", "Успіхи", "429", "Транспортні помилки", "RAM, МіБ", "Час роботи", "Стан"], model_rows, (1, 2, 3, 4, 5)),
                  "", "Перевірки IP",
                  doctor_table(["Маршрут", "Спроби", "Успіхи", "Помилки", "Останній IP"], ip_rows, (1, 2, 3)),
                  "", "Паузи маршрутів",
                  doctor_table(["Маршрут", "Остання 429", "Retry-After", "Кінець паузи", "Причина / стан"], pause_rows)])
    for label, endpoint, _ in entries:
        if (observations.get(endpoint) or {}).get("error"):
            lines.append(f"Помилка перевірки IP {label}: {observations[endpoint]['error']}")
    if health.get("ip_monitor_error"):
        lines.append(f"Помилка монітора IP: {health['ip_monitor_error']}")
    for ip, endpoints in (health.get("duplicate_ips") or {}).items():
        labels = [label for label, endpoint, _ in entries if endpoint in endpoints]
        lines.append(f"Однаковий IP {ip}: {', '.join(labels)}")
    if missing:
        lines.append("— Немає даних для маршруту; для повної таблиці оновіть запущений брідж.")
    if set(routes) - {endpoint for _, endpoint, _ in entries}:
        lines.append("Історія інших маршрутів збережена; доступна через doctor --json.")
    if memory.get("error"):
        lines.append("Пам'ять WARP / час роботи: " + memory["error"])
    lines.extend(["", "Лічильники накопичувальні; локальні відмови через cooldown не є спробами.",
                  "Остання 429 — історія відповідей через цей брідж; н/д — дані не збережені, зокрема для старих пауз.",
                  "Retry-After — отриманий заголовок (число означає секунди); немає — заголовок відсутній.",
                  "Час 429 і кінець чинної паузи містять дату, секунди й UTC-зсув; — означає відсутність чинної паузи.",
                  "Без паузи — немає відомої відмови; доступність квоти тут не перевіряється.",
                  "RAM — поточне споживання контейнера WARP із проксі, без спільних витрат Colima.",
                  "Час роботи — від останнього запуску контейнера; після його перезапуску відлік починається заново.",
                  "н/д — немає виміру; + у підсумку — частина вузлів без виміру; direct не має WARP-контейнера."])
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("login", help="Read a Zen/Go API key with hidden input; save mode 0600")
    sub.add_parser("catalog", help="Fetch and display the zero-cost allowlist and excluded-model reasons; performs no inference")
    serve = sub.add_parser("serve", help="Run a foreground, loopback-only proxy")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--compat-opencode", action="store_true", help="Explicitly enable unofficial PR-style OpenCode identity and tool padding; account-policy risk")
    serve.add_argument("--opencode-version", default="1.19.0", help="OpenCode client UA version")
    serve.add_argument("--zen-proxy", action="append", metavar="http://HOST:PORT",
                       help=f"Override saved zen-proxies.json; up to {MAX_ZEN_PROXIES} fixed WARP exits, tried in order after direct quota exhaustion")
    doctor = sub.add_parser("doctor", help="Show human-readable route statistics and health; does not infer")
    doctor.add_argument("--json", action="store_true", help="Print the complete machine-readable /health JSON")
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
            raw = response.read(MAX_FRAME + 1)
            if len(raw) > MAX_FRAME:
                raise BridgeError("Health response exceeds 2 MiB.")
            health = load_json(raw)
            if args.json:
                print(json.dumps(health, ensure_ascii=False))
            elif response.status == 200:
                print(doctor_report(health, doctor_memory(health)))
            else:
                detail = health.get("error") or health
                print("Перевірка бріджа не вдалася: " + str(detail.get("message", detail)))
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
    zen_proxies = configured_proxies(args.zen_proxy)
    key, token = key_from_user(required=False), token_from_user()
    catalog = Catalog()
    snapshot = catalog.get()
    if not snapshot["models"]:
        raise BridgeError("No verified zero-cost models. Inspect catalog; no inference allowed.", 503)
    server = BridgeServer(("127.0.0.1", args.port), catalog, key, token, args.compat_opencode, args.opencode_version, zen_proxies=zen_proxies, state=STATE)
    with server.lock:
        server.save_zen_cooldowns()
    save_private(STATE / "runtime.json", encode({"port": args.port, "version": VERSION, "pid": os.getpid()}))
    print(f"OMP free-only bridge: 127.0.0.1:{args.port}; {len(snapshot['models'])} verified zero-cost routes.", flush=True)
    print(f"Zen routing: direct, then {len(zen_proxies)} fixed WARP proxies in configured order; quota cooldowns are per route." if zen_proxies else "Zen routing: direct.", flush=True)
    print("No OpenCode CLI. No stream replay. No paid fallback. Ctrl+C stops the proxy.", flush=True)
    if args.compat_opencode:
        print("UNOFFICIAL COMPATIBILITY ENABLED: OpenCode-shaped identity and guarded tool padding. Account-policy risk; read docs/ДОСЛІДЖЕННЯ.md.", flush=True)
    server.start_ip_monitor()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.ip_monitor_stop.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BridgeError, OSError, ValueError, http.client.HTTPException) as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
