#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Minimal local OTLP/HTTP JSON capture and schema probe for agent-dev-workbench.

Standard library only (Python 3.9+). Nothing here sends data anywhere: the
receiver binds 127.0.0.1 and writes only under the output directory it is given.

  receive   Listen for OTLP/HTTP JSON on /v1/traces, /v1/metrics and /v1/logs and
            append each request to <out>/otlp.jsonl. String attribute values are
            redacted at ingest (names, enums, numbers and booleans are kept) unless
            --store-raw-values is given.
  schema    Summarize capture files (receiver output, or OTLP-JSON lines written by
            another exporter) into schema.json and schema.md: signal names,
            attribute keys and value types, span hierarchy, token-field totals,
            parent/child token roll-up, privacy flags, and documented-vs-observed
            coverage. It never prints a redacted value.
  selftest  Run receive + schema against fixtures/synthetic/ in a temporary
            directory. Everything it produces is labeled SYNTHETIC.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import secrets
import socket
import sys
import tempfile
import threading
import zlib
from collections import Counter, defaultdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROBE_FORMAT = 1
HERE = Path(__file__).resolve().parent
DEFAULT_EXPECTATIONS = HERE / "expectations.json"
FIXTURE_DIR = HERE / "fixtures" / "synthetic"
SIGNAL_PATHS = {"/v1/traces": "traces", "/v1/metrics": "metrics", "/v1/logs": "logs"}
SIGNAL_TOP_KEYS = {"resourceSpans": "traces", "resourceMetrics": "metrics", "resourceLogs": "logs"}
MAX_BODY = 64 * 1024 * 1024
FIXTURE_MARKER_KEY = "workbench.fixture"

# --------------------------------------------------------------------------
# Redaction policy
# --------------------------------------------------------------------------
# String values survive only for keys that carry names or enums, never free text.
KEEP_STRING_KEYS = frozenset({
    # resource identity
    "service.name", "service.version", "service.namespace", "telemetry.sdk.name",
    "telemetry.sdk.language", "telemetry.sdk.version", "os.type", "os.version",
    "host.arch", "process.runtime.name", "process.runtime.version", "wsl.version",
    # this project's own attribution keys
    "workbench.fixture", "workbench.task.id", "workbench.harness",
    # OTel GenAI semantic conventions (names and enums only)
    "gen_ai.operation.name", "gen_ai.provider.name", "gen_ai.system", "gen_ai.agent.name",
    "gen_ai.request.model", "gen_ai.response.model", "gen_ai.response.finish_reasons",
    "gen_ai.tool.name", "gen_ai.tool.type", "gen_ai.token.type", "gen_ai.output.type",
    "error.type", "exception.type", "server.address", "event.name", "event.domain",
    # VS Code Copilot Chat (documented enum-like keys)
    "github.copilot.agent.type", "github.copilot.hook.decision",
    "github.copilot.tool.parameters.edit_type", "github.copilot.tool.parameters.mcp_tool_name",
    "copilot_chat.mode_name", "copilot_chat.hook_type", "copilot_chat.hook_result_kind",
    "copilot_chat.edit.source", "copilot_chat.edit_source",
    # Claude Code (documented enum-like keys)
    "type", "model", "final_model", "tool_name", "decision", "source", "decision_type",
    "decision_source", "success", "error_type", "terminal.type", "app.version",
    "app.entrypoint", "query_source", "speed", "effort", "start_type", "language",
    "agent_type", "agent.name", "agent.source", "permission_mode", "tool_source",
    "category", "trigger", "status", "command_source", "stop_reason", "hook_event",
    "transport_type", "mcp_server_scope",
    # Codex (documented context fields; low confidence, see expectations.json)
    "auth_mode", "originator", "session_source", "environment", "env",
})
# Identifiers: replaced with a salted hash so joins work inside one run only.
HASH_KEYS = frozenset({
    "session.id", "gen_ai.conversation.id", "conversation.id", "prompt.id",
    "gen_ai.tool.call.id", "tool_use_id", "call_id", "request_id", "client_request_id",
    "gen_ai.response.id", "message.uuid", "message.id", "agent_id", "parent_agent_id",
    "service.instance.id", "ccr.session.id",
})
# Reported (presence only) so a reviewer sees what a harness attaches.
IDENTITY_KEYS = frozenset({
    "user.email", "user.account_uuid", "user.account_id", "organization.id", "user.id",
    "user.name", "process.user.name", "host.name", "enduser.id", "github.copilot.github.org",
})
WORKSPACE_KEYS = frozenset({
    "github.copilot.git.repository", "github.copilot.git.branch",
    "github.copilot.git.commit_sha", "copilot_chat.repo.remote_url",
    "copilot_chat.repo.head_branch_name", "copilot_chat.repo.head_commit_hash",
})
WORKSPACE_PREFIXES = ("vcs.",)
CONTENT_KEYS = frozenset({
    "gen_ai.input.messages", "gen_ai.output.messages", "gen_ai.system_instructions",
    "gen_ai.tool.definitions", "gen_ai.tool.call.arguments", "gen_ai.tool.call.result",
    "copilot_chat.user_request", "copilot_chat.reasoning_content", "copilot_chat.hook_input",
    "copilot_chat.hook_output", "github.copilot.tool.parameters.command",
    "github.copilot.tool.parameters.file_path", "github.copilot.tool.parameters.mcp_server_name",
    "prompt", "prompt_text", "user_prompt", "tool_parameters", "tool_input", "full_command",
    "file_path", "error", "body", "system_prompt", "exception.message", "exception.stacktrace",
})
HARNESS_REDACTION_TOKENS = frozenset({
    "", "<REDACTED>", "[REDACTED]", "[redacted]", "REDACTED", "redacted", "<redacted>", "[encrypted]",
})
PROBE_MARKER_RE = re.compile(r"^<probe-(?:redacted (?:string|bytes) len=(\d+)|hash [0-9a-f]{12})>$")
EVENT_NAME_BODY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*(?:[._][A-Za-z0-9]+)+$")
TOKEN_FIELD_RE = re.compile(r"(?:^|[._])(?:input|output|cache|cached|reasoning)[a-z0-9_.]*tokens?(?:_count)?$")

PARENT_OPS = ("invoke_agent", "claude_code.interaction")
MODEL_OPS = ("chat", "claude_code.llm_request")
KIND_NAMES = {0: "UNSPECIFIED", 1: "INTERNAL", 2: "SERVER", 3: "CLIENT", 4: "PRODUCER", 5: "CONSUMER"}
STATUS_NAMES = {0: "UNSET", 1: "OK", 2: "ERROR"}
TEMPORALITY_NAMES = {0: "UNSPECIFIED", 1: "DELTA", 2: "CUMULATIVE"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def is_sensitive_key(key: str) -> str | None:
    if key in CONTENT_KEYS:
        return "content"
    if key in IDENTITY_KEYS:
        return "identity"
    if key in WORKSPACE_KEYS or key.startswith(WORKSPACE_PREFIXES):
        return "workspace"
    return None


def hash_marker(value: str, salt: bytes) -> str:
    return "<probe-hash %s>" % hashlib.sha256(salt + value.encode("utf-8", "replace")).hexdigest()[:12]


def redact_any(key: str, av, salt: bytes):
    """Redact one OTLP AnyValue according to the key policy."""
    if not isinstance(av, dict):
        return av
    if "stringValue" in av:
        s = av["stringValue"]
        if not isinstance(s, str):
            return av
        if key in KEEP_STRING_KEYS or s in HARNESS_REDACTION_TOKENS or PROBE_MARKER_RE.match(s):
            return {"stringValue": s[:200]}
        if key in HASH_KEYS:
            return {"stringValue": hash_marker(s, salt)}
        return {"stringValue": "<probe-redacted string len=%d>" % len(s)}
    if "bytesValue" in av:
        return {"bytesValue": ""}  # keep the type, drop the bytes
    if "arrayValue" in av:
        values = (av.get("arrayValue") or {}).get("values") or []
        return {"arrayValue": {"values": [redact_any(key, x, salt) for x in values]}}
    if "kvlistValue" in av:
        values = (av.get("kvlistValue") or {}).get("values") or []
        return {"kvlistValue": {"values": redact_kvs(values, salt)}}
    return av  # bool / int / double carry no free text


def redact_kvs(kvs, salt: bytes):
    out = []
    for kv in kvs or []:
        if isinstance(kv, dict):
            key = str(kv.get("key", ""))
            out.append({"key": key, "value": redact_any(key, kv.get("value") or {}, salt)})
    return out


def body_is_event_name(body) -> bool:
    return isinstance(body, dict) and isinstance(body.get("stringValue"), str) \
        and bool(EVENT_NAME_BODY_RE.match(body["stringValue"])) and len(body["stringValue"]) <= 120


def redact_node(node, salt: bytes, parent_key: str | None = None):
    """Walk an OTLP JSON payload and redact every carrier that can hold free text."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k == "attributes" and isinstance(v, list):
                out[k] = redact_kvs(v, salt)
            elif k == "body" and isinstance(v, dict):
                out[k] = v if body_is_event_name(v) else redact_any("body", v, salt)
            elif k == "exemplars":
                continue  # exemplars can carry filtered attributes; not needed for schema
            elif k == "traceState":
                out[k] = ""
            elif k == "message" and parent_key == "status" and isinstance(v, str):
                out[k] = v if not v else "<probe-redacted string len=%d>" % len(v)
            elif k == "name" and isinstance(v, str):
                out[k] = v[:200]
            else:
                out[k] = redact_node(v, salt, k)
        return out
    if isinstance(node, list):
        return [redact_node(x, salt, parent_key) for x in node]
    return node


def detect_signal(payload) -> str | None:
    if isinstance(payload, dict):
        for key, signal in SIGNAL_TOP_KEYS.items():
            if key in payload:
                return signal
    return None


def resource_attr(resource_block, key: str):
    for kv in ((resource_block or {}).get("resource") or {}).get("attributes") or []:
        if isinstance(kv, dict) and kv.get("key") == key:
            return unwrap(kv.get("value"))
    return None


def payload_is_synthetic(payload) -> bool:
    signal = detect_signal(payload)
    if not signal:
        return False
    top = {v: k for k, v in SIGNAL_TOP_KEYS.items()}[signal]
    blocks = payload.get(top) or []
    return bool(blocks) and all(resource_attr(b, FIXTURE_MARKER_KEY) == "synthetic" for b in blocks)


def unwrap(av):
    """Plain Python value from an OTLP AnyValue."""
    if not isinstance(av, dict):
        return av
    if "stringValue" in av:
        return av["stringValue"]
    if "boolValue" in av:
        return bool(av["boolValue"])
    if "intValue" in av:
        try:
            return int(av["intValue"])
        except (TypeError, ValueError):
            return av["intValue"]
    if "doubleValue" in av:
        try:
            return float(av["doubleValue"])
        except (TypeError, ValueError):
            return av["doubleValue"]
    if "arrayValue" in av:
        return [unwrap(x) for x in (av.get("arrayValue") or {}).get("values") or []]
    if "kvlistValue" in av:
        return {kv.get("key"): unwrap(kv.get("value")) for kv in (av.get("kvlistValue") or {}).get("values") or []}
    if "bytesValue" in av:
        return "<bytes>"
    return None


def value_type(av) -> str:
    if not isinstance(av, dict):
        return type(av).__name__
    for field, name in (("stringValue", "string"), ("boolValue", "bool"), ("intValue", "int"),
                        ("doubleValue", "double"), ("kvlistValue", "kvlist"), ("bytesValue", "bytes")):
        if field in av:
            return name
    if "arrayValue" in av:
        inner = sorted({value_type(x) for x in (av.get("arrayValue") or {}).get("values") or []})
        return "array<%s>" % ("|".join(inner) if inner else "empty")
    return "empty"


def as_number(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        try:
            return int(v)
        except ValueError:
            try:
                return float(v)
            except ValueError:
                return None
    return None


def enum_name(v, table, prefix: str, default: str) -> str:
    if v is None:
        return default
    if isinstance(v, str):
        s = v.upper()
        return s[len(prefix):] if s.startswith(prefix) else s
    if isinstance(v, int):
        return table.get(v, str(v))
    return str(v)


# --------------------------------------------------------------------------
# Receiver
# --------------------------------------------------------------------------
class Receiver:
    def __init__(self, out_dir: Path, store_raw: bool = False, quiet: bool = False):
        self.out_dir = out_dir
        self.store_raw = store_raw
        self.quiet = quiet
        self.salt = secrets.token_bytes(16)
        self.lock = threading.Lock()
        self.counts = Counter()
        self.capture_path = out_dir / "otlp.jsonl"
        self.started_at = now_iso()

    def log(self, msg: str) -> None:
        if not self.quiet:
            print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg), flush=True)

    def write_run_file(self, port: int, stopped: bool = False) -> None:
        run = {
            "probe_format": PROBE_FORMAT,
            "kind": "capture",
            "started_at": self.started_at,
            "stopped_at": now_iso() if stopped else None,
            "endpoint": "http://127.0.0.1:%d" % port,
            "redaction": "none (raw values stored)" if self.store_raw else "ingest-default",
            "requests": dict(self.counts),
            "note": "Received on an unauthenticated loopback port: any local process can post here. "
                    "Payloads carrying workbench.fixture=synthetic are labeled synthetic-fixture.",
        }
        (self.out_dir / "run.json").write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")

    def handle(self, path: str, headers, body: bytes):
        signal = SIGNAL_PATHS.get(path.split("?", 1)[0].rstrip("/"))
        if not signal:
            return 404, b'{"error":"use /v1/traces, /v1/metrics or /v1/logs"}'
        encoding = (headers.get("Content-Encoding") or "identity").strip().lower()
        if encoding == "gzip":
            d = zlib.decompressobj(16 + zlib.MAX_WBITS)
            try:
                body = d.decompress(body, MAX_BODY)
            except zlib.error:
                return 400, b'{"error":"invalid gzip body"}'
            if d.unconsumed_tail:
                return 413, b'{"error":"decompressed body too large"}'
        elif encoding not in ("identity", ""):
            return 415, b'{"error":"unsupported Content-Encoding"}'
        ctype = (headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        envelope = {"probe_format": PROBE_FORMAT, "received_at": now_iso(), "signal": signal,
                    "content_type": ctype, "content_encoding": encoding, "bytes": len(body)}
        if ctype != "application/json":
            with self.lock:
                self.counts["rejected:" + (ctype or "no-content-type")] += 1
                envelope.update(payload=None, rejected="content type %r is not decoded by this probe; "
                                "set the exporter protocol to http/json" % ctype)
                self._append(envelope)
            self.log("REJECTED %s body with Content-Type %r (configure http/json)" % (signal, ctype))
            return 415, b'{"error":"this probe accepts OTLP/HTTP JSON only; set protocol http/json"}'
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return 400, b'{"error":"body is not valid JSON"}'
        if detect_signal(payload) != signal:
            return 400, b'{"error":"payload does not match the signal path"}'
        synthetic = payload_is_synthetic(payload)
        envelope.update(
            redaction="none" if self.store_raw else "ingest-default",
            provenance="synthetic-fixture" if synthetic else "captured",
            payload=payload if self.store_raw else redact_node(payload, self.salt),
        )
        with self.lock:
            self.counts[signal] += 1
            self._append(envelope)
        self.log("%-7s %s  services=%s  provenance=%s" % (
            signal, describe_counts(signal, payload), ",".join(sorted(services_in(signal, payload))) or "?",
            envelope["provenance"]))
        return 200, b"{}"

    def _append(self, envelope) -> None:
        with self.capture_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(envelope, separators=(",", ":")) + "\n")


def services_in(signal: str, payload) -> set:
    top = {v: k for k, v in SIGNAL_TOP_KEYS.items()}[signal]
    return {str(resource_attr(b, "service.name")) for b in payload.get(top) or []}


def describe_counts(signal: str, payload) -> str:
    n = 0
    for _rb, _res, _scope, _item in iter_items(signal, payload):
        n += 1
    unit = {"traces": "spans", "metrics": "metrics", "logs": "log records"}[signal]
    return "+%d %s" % (n, unit)


def read_chunked(rfile, limit: int) -> bytes | None:
    data = bytearray()
    while True:
        line = rfile.readline(1024)
        if not line:
            return None
        try:
            size = int(line.split(b";", 1)[0].strip(), 16)
        except ValueError:
            return None
        if size == 0:
            while rfile.readline(1024) not in (b"\r\n", b"\n", b""):
                pass
            return bytes(data)
        if len(data) + size > limit:
            return None
        data += rfile.read(size)
        rfile.readline(1024)


class LoopbackServer(ThreadingHTTPServer):
    """Loopback-only server that refuses to share its port.

    HTTPServer enables SO_REUSEADDR. On Windows that lets a second socket bind a
    port that is already listening, so two receivers (or a receiver and a
    collector) could silently split one exporter's traffic. Bind exclusively.
    """
    daemon_threads = True
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def make_server(rx: Receiver, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "agent-dev-workbench-probe/1"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # keep request lines out of the console
            pass

        def do_POST(self):
            if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
                body = read_chunked(self.rfile, MAX_BODY)
                if body is None:
                    return self._reply(413, b'{"error":"bad or oversized chunked body"}')
            else:
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = -1
                if length < 0 or length > MAX_BODY:
                    return self._reply(413, b'{"error":"body too large"}')
                body = self.rfile.read(length)
            status, resp = rx.handle(self.path, self.headers, body)
            self._reply(status, resp)

        def do_GET(self):
            self._reply(405, b'{"error":"POST OTLP/HTTP JSON to /v1/traces, /v1/metrics or /v1/logs"}')

        def _reply(self, status: int, body: bytes):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return LoopbackServer(("127.0.0.1", port), Handler)


def cmd_receive(args) -> int:
    out = Path(args.out) if args.out else HERE / "captures" / ("%s-%s" % (
        datetime.now().strftime("%Y%m%d-%H%M%S"), re.sub(r"[^A-Za-z0-9_.-]", "-", args.label)))
    out.mkdir(parents=True, exist_ok=True)
    rx = Receiver(out, store_raw=args.store_raw_values)
    try:
        server = make_server(rx, args.port)
    except OSError as exc:
        print("Cannot listen on 127.0.0.1:%d (%s). Is a collector or another probe already running? "
              "Pick another port with --port and point the exporter at it." % (args.port, exc), file=sys.stderr)
        return 2
    port = server.server_address[1]
    rx.write_run_file(port)
    print("OTLP/HTTP JSON probe listening on http://127.0.0.1:%d  (Ctrl+C to stop)" % port)
    print("Writing to %s" % rx.capture_path)
    if args.store_raw_values:
        print("WARNING: --store-raw-values keeps prompt, identity and path values verbatim. "
              "Delete this directory after inspection; never commit it.")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        rx.write_run_file(port, stopped=True)
        print("\nStopped. Requests: %s" % (dict(rx.counts) or "none"))
        print("Next: python \"%s\" schema \"%s\"" % (Path(__file__).resolve(), out))
    return 0


# --------------------------------------------------------------------------
# Schema summarizer
# --------------------------------------------------------------------------
SCOPE_KEYS = {
    "traces": ("resourceSpans", ("scopeSpans", "instrumentationLibrarySpans"), "spans"),
    "metrics": ("resourceMetrics", ("scopeMetrics", "instrumentationLibraryMetrics"), "metrics"),
    "logs": ("resourceLogs", ("scopeLogs", "instrumentationLibraryLogs"), "logRecords"),
}


def iter_items(signal: str, payload):
    top, scope_keys, item_key = SCOPE_KEYS[signal]
    for rb in payload.get(top) or []:
        resource = rb.get("resource") or {}
        for sk in scope_keys:
            for sb in rb.get(sk) or []:
                scope = sb.get("scope") or sb.get("instrumentationLibrary") or {}
                for item in sb.get(item_key) or []:
                    yield rb, resource, scope, item


def iter_keyvalues(node):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "attributes" and isinstance(v, list):
                for kv in v:
                    if isinstance(kv, dict) and "key" in kv:
                        yield kv
            else:
                yield from iter_keyvalues(v)
    elif isinstance(node, list):
        for x in node:
            yield from iter_keyvalues(x)


def attrs_dict(kvs) -> dict:
    return {str(kv.get("key")): kv.get("value") or {} for kv in kvs or [] if isinstance(kv, dict)}


def token_fields(attrs: dict) -> dict:
    out = {}
    for key, av in attrs.items():
        if TOKEN_FIELD_RE.search(key):
            n = as_number(unwrap(av))
            if n is not None:
                out[key] = n
    return out


class AttrStats:
    def __init__(self):
        self.keys = {}

    def add(self, kvs) -> None:
        for kv in kvs or []:
            if not isinstance(kv, dict):
                continue
            key = str(kv.get("key", ""))
            av = kv.get("value") or {}
            st = self.keys.setdefault(key, {"count": 0, "types": Counter(), "values": Counter(),
                                            "min": None, "max": None})
            st["count"] += 1
            st["types"][value_type(av)] += 1
            pv = unwrap(av)
            if isinstance(pv, bool):
                st["values"][str(pv).lower()] += 1
            elif isinstance(pv, (int, float)):
                st["min"] = pv if st["min"] is None else min(st["min"], pv)
                st["max"] = pv if st["max"] is None else max(st["max"], pv)
            elif key in KEEP_STRING_KEYS:
                for item in pv if isinstance(pv, list) else [pv]:
                    if isinstance(item, str) and (item in st["values"] or len(st["values"]) < 20):
                        st["values"][item[:120]] += 1

    def to_json(self) -> dict:
        out = {}
        for key in sorted(self.keys):
            st = self.keys[key]
            entry = {"count": st["count"], "types": sorted(st["types"])}
            if st["values"]:
                entry["values"] = dict(st["values"].most_common(20))
            if st["min"] is not None:
                entry["min"], entry["max"] = st["min"], st["max"]
            sensitivity = is_sensitive_key(key)
            if sensitivity:
                entry["sensitive"] = sensitivity
            out[key] = entry
        return out


class Schema:
    def __init__(self, expectations: dict | None):
        self.expectations = expectations or {}
        self.salt = secrets.token_bytes(16)
        self.inputs = []
        self.requests = Counter()
        self.provenance = Counter()
        self.unrecognized_shapes = Counter()
        self.resources = {}
        self.spans = {}
        self.metrics = {}
        self.events = {}
        self.privacy = defaultdict(lambda: {"category": None, "occurrences": 0, "with_value": 0,
                                            "harness_redacted": 0, "signals": set()})
        self.span_records = []
        self.metric_series = {}
        self.event_tokens = defaultdict(float)
        self.observed = defaultdict(lambda: {"spans": set(), "metrics": set(), "events": set()})

    # -- input -------------------------------------------------------------
    def add_file(self, path: Path) -> None:
        lines = 0
        with path.open(encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                lines += 1
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    self.unrecognized_shapes["<not JSON>"] += 1
                    continue
                if isinstance(rec, dict) and "probe_format" in rec:
                    if rec.get("payload") is None:
                        self.requests["rejected"] += 1
                        continue
                    self.add_payload(rec["payload"], rec.get("provenance", "captured"))
                elif detect_signal(rec):
                    # OTLP JSON written by another exporter: provenance from the fixture marker only.
                    self.add_payload(rec, "synthetic-fixture" if payload_is_synthetic(rec) else "captured")
                else:
                    keys = ",".join(sorted(rec)[:12]) if isinstance(rec, dict) else type(rec).__name__
                    self.unrecognized_shapes[keys] += 1
        self.inputs.append({"path": str(path), "lines": lines})

    def add_payload(self, payload, provenance: str) -> None:
        signal = detect_signal(payload)
        if not signal:
            return
        self.requests[signal] += 1
        self.provenance[provenance] += 1
        self.scan_privacy(signal, payload)
        payload = redact_node(payload, self.salt)  # idempotent on receiver output
        getattr(self, "_add_" + signal)(payload)

    def scan_privacy(self, signal: str, payload) -> None:
        def note(key: str, category: str, raw) -> None:
            entry = self.privacy[key]
            entry["category"] = category
            entry["occurrences"] += 1
            entry["signals"].add(signal)
            if isinstance(raw, str):
                m = PROBE_MARKER_RE.match(raw)
                if raw in HARNESS_REDACTION_TOKENS:
                    entry["harness_redacted"] += 1
                elif m and m.group(1) is not None:
                    entry["with_value" if int(m.group(1)) > 0 else "harness_redacted"] += 1
                else:
                    entry["with_value"] += 1
            elif raw not in (None, [], {}):
                entry["with_value"] += 1

        for kv in iter_keyvalues(payload):
            key = str(kv.get("key", ""))
            category = is_sensitive_key(key)
            if category:
                note(key, category, unwrap(kv.get("value")))
        if signal == "logs":
            for _rb, _res, _scope, rec in iter_items("logs", payload):
                body = rec.get("body")
                if isinstance(body, dict) and body and not body_is_event_name(body):
                    note("log.body", "content", unwrap(body) if "stringValue" in body else "<structured>")

    # -- per-signal analysis ---------------------------------------------
    def _service(self, rb, resource, seen: dict) -> str:
        """Register each resource block once per payload and return its service.name."""
        if id(rb) not in seen:
            seen[id(rb)] = self._resource(resource)
        return seen[id(rb)]

    def _resource(self, resource) -> str:
        attrs = attrs_dict(resource.get("attributes"))
        service = str(unwrap(attrs.get("service.name")) or "(no service.name)")
        entry = self.resources.setdefault(service, {"count": 0, "attributes": AttrStats()})
        entry["count"] += 1
        entry["attributes"].add(resource.get("attributes"))
        return service

    def _add_traces(self, payload) -> None:
        seen = {}
        for rb, resource, scope, span in iter_items("traces", payload):
            service = self._service(rb, resource, seen)
            attrs = attrs_dict(span.get("attributes"))
            name = str(span.get("name") or "(unnamed)")
            group = str(unwrap(attrs.get("gen_ai.operation.name")) or name)
            st = self.spans.setdefault(group, {"count": 0, "names": Counter(), "services": Counter(),
                                               "scopes": Counter(), "kinds": Counter(), "status": Counter(),
                                               "events": Counter(), "parents": Counter(),
                                               "attributes": AttrStats(), "duration_ms": [None, None]})
            st["count"] += 1
            st["names"][name] += 1
            st["services"][service] += 1
            st["scopes"][str(scope.get("name") or "")] += 1
            st["kinds"][enum_name(span.get("kind"), KIND_NAMES, "SPAN_KIND_", "UNSPECIFIED")] += 1
            st["status"][enum_name((span.get("status") or {}).get("code"), STATUS_NAMES, "STATUS_CODE_", "UNSET")] += 1
            for ev in span.get("events") or []:
                st["events"][str(ev.get("name") or "")] += 1
            st["attributes"].add(span.get("attributes"))
            start, end = as_number(span.get("startTimeUnixNano")), as_number(span.get("endTimeUnixNano"))
            if start is not None and end is not None and end >= start:
                ms = (end - start) / 1e6
                lo, hi = st["duration_ms"]
                st["duration_ms"] = [ms if lo is None else min(lo, ms), ms if hi is None else max(hi, ms)]
            self.observed[service]["spans"].update({group, name})
            self.span_records.append({
                "trace": str(span.get("traceId") or ""), "id": str(span.get("spanId") or ""),
                "parent": str(span.get("parentSpanId") or ""), "group": group, "service": service,
                "tokens": token_fields(attrs),
            })

    def _add_metrics(self, payload) -> None:
        seen = {}
        for rb, resource, scope, metric in iter_items("metrics", payload):
            service = self._service(rb, resource, seen)
            name = str(metric.get("name") or "(unnamed)")
            mtype = next((t for t in ("sum", "gauge", "histogram", "exponentialHistogram", "summary") if t in metric), "unknown")
            body = metric.get(mtype) or {}
            st = self.metrics.setdefault(name, {"type": Counter(), "unit": Counter(), "description": "",
                                                "temporality": Counter(), "monotonic": Counter(),
                                                "services": Counter(), "points": 0, "attributes": AttrStats()})
            st["type"][mtype] += 1
            st["unit"][str(metric.get("unit") or "")] += 1
            st["description"] = st["description"] or str(metric.get("description") or "")[:200]
            temporality = enum_name(body.get("aggregationTemporality"), TEMPORALITY_NAMES,
                                    "AGGREGATION_TEMPORALITY_", "n/a" if mtype in ("gauge", "summary") else "UNSPECIFIED")
            st["temporality"][temporality] += 1
            if "isMonotonic" in body:
                st["monotonic"][str(bool(body["isMonotonic"])).lower()] += 1
            st["services"][service] += 1
            self.observed[service]["metrics"].add(name)
            for point in body.get("dataPoints") or []:
                st["points"] += 1
                st["attributes"].add(point.get("attributes"))
                if "token" in name and "time_to_first" not in name:
                    self._token_point(service, name, mtype, temporality, point)

    def _token_point(self, service, name, mtype, temporality, point) -> None:
        attrs = attrs_dict(point.get("attributes"))
        token_type = unwrap(attrs.get("gen_ai.token.type")) or unwrap(attrs.get("type")) or "(untyped)"
        if mtype == "sum":
            value = as_number(point.get("asInt", point.get("asDouble")))
        elif mtype in ("histogram", "exponentialHistogram"):
            value = as_number(point.get("sum"))
        else:
            return
        if value is None:
            return
        series = (service, name, str(token_type), json.dumps(point.get("attributes"), sort_keys=True),
                  str(point.get("startTimeUnixNano")))
        if temporality == "CUMULATIVE":
            self.metric_series[series] = max(self.metric_series.get(series, value), value)
        else:
            self.metric_series[series] = self.metric_series.get(series, 0) + value

    def _add_logs(self, payload) -> None:
        seen = {}
        for rb, resource, scope, rec in iter_items("logs", payload):
            service = self._service(rb, resource, seen)
            attrs = attrs_dict(rec.get("attributes"))
            body = rec.get("body")
            event_attr = unwrap(attrs.get("event.name"))
            if rec.get("eventName"):
                name = str(rec["eventName"])
            elif body_is_event_name(body):
                name = body["stringValue"]
            elif event_attr:
                name = str(event_attr)
            else:
                name = "(unnamed log record)"
            st = self.events.setdefault(name, {"count": 0, "services": Counter(), "scopes": Counter(),
                                               "severity": Counter(), "body_types": Counter(),
                                               "attributes": AttrStats()})
            st["count"] += 1
            st["services"][service] += 1
            st["scopes"][str(scope.get("name") or "")] += 1
            st["severity"][str(rec.get("severityText") or rec.get("severityNumber") or "")] += 1
            st["body_types"]["event-name string" if body_is_event_name(body) else value_type(body) if body else "none"] += 1
            st["attributes"].add(rec.get("attributes"))
            self.observed[service]["events"].update({name} | ({str(event_attr)} if event_attr else set()))
            for key, n in token_fields(attrs).items():
                self.event_tokens[(service, name, key)] += n

    # -- derived views -----------------------------------------------------
    def token_rollup(self) -> list:
        by_trace = defaultdict(dict)
        for rec in self.span_records:
            by_trace[rec["trace"]][rec["id"]] = rec
        children = defaultdict(list)
        for trace, spans in by_trace.items():
            for rec in spans.values():
                if rec["parent"]:
                    children[(trace, rec["parent"])].append(rec)

        def is_op(group: str, ops) -> bool:
            return any(group == o or group.startswith(o + " ") for o in ops)

        def walk(trace, span_id, stop_at_agents):
            for child in children.get((trace, span_id), []):
                if is_op(child["group"], MODEL_OPS):
                    yield child
                if stop_at_agents and is_op(child["group"], PARENT_OPS):
                    continue
                yield from walk(trace, child["id"], stop_at_agents)

        results = Counter()
        for trace, spans in by_trace.items():
            for rec in spans.values():
                if not is_op(rec["group"], PARENT_OPS) or not rec["tokens"]:
                    continue
                direct = list(walk(trace, rec["id"], True))
                every = list(walk(trace, rec["id"], False))
                nested_agent = any(is_op(c["group"], PARENT_OPS) for c in self._descendants(trace, rec["id"], children))
                for field, value in rec["tokens"].items():
                    d = sum(c["tokens"].get(field, 0) for c in direct)
                    a = sum(c["tokens"].get(field, 0) for c in every)
                    if not every:
                        rel = "no child model-call spans captured"
                    elif value == a and (value != d or not nested_agent):
                        rel = "equals sum of descendant model calls" + (" (incl. nested agents)" if nested_agent else "")
                    elif value == d:
                        rel = "equals sum of direct model calls (excl. nested agents)"
                    elif value > a:
                        rel = "exceeds descendant model-call sum"
                    else:
                        rel = "less than descendant model-call sum"
                    results[(rec["group"], rec["service"], field, rel)] += 1
        return [{"parent": g, "service": s, "field": f, "relation": r, "count": n}
                for (g, s, f, r), n in sorted(results.items())]

    def _descendants(self, trace, span_id, children):
        for child in children.get((trace, span_id), []):
            yield child
            yield from self._descendants(trace, child["id"], children)

    def token_views(self) -> list:
        rows = defaultdict(float)
        for rec in self.span_records:
            for field, n in rec["tokens"].items():
                rows[("span", rec["service"], rec["group"], field)] += n
        for (service, name, field), n in self.event_tokens.items():
            rows[("event", service, name, field)] += n
        for (service, name, token_type, _attrs, _start), n in self.metric_series.items():
            rows[("metric", service, name, "type=" + token_type)] += n
        return [{"source": src, "service": svc, "signal_name": name, "field": field,
                 "total": int(total) if float(total).is_integer() else round(total, 3)}
                for (src, svc, name, field), total in sorted(rows.items())]

    def coverage(self) -> list:
        out = []
        harnesses = (self.expectations.get("harnesses") or {})
        for hid, spec in harnesses.items():
            names = set(spec.get("service_names") or [])
            prefixes = tuple(spec.get("service_name_prefixes") or [])
            services = [s for s in self.observed if s in names or (prefixes and s.startswith(prefixes))]
            present = bool(services)
            for kind in ("spans", "metrics", "events"):
                for item in spec.get(kind) or []:
                    exp = item["name"] if isinstance(item, dict) else item
                    optional = isinstance(item, dict) and item.get("optional")
                    seen = any(self._matches(exp, self.observed[s][kind]) for s in services)
                    status = "observed" if seen else ("harness not in capture" if not present
                                                      else "not observed in this capture")
                    out.append({"harness": hid, "kind": kind[:-1], "name": exp, "status": status,
                                "optional": bool(optional),
                                "note": item.get("note", "") if isinstance(item, dict) else ""})
        return out

    @staticmethod
    def _matches(expected: str, observed: set) -> bool:
        return any(o == expected or o.startswith(expected + " ") or expected.endswith("." + o) for o in observed)

    def overall_provenance(self) -> str:
        kinds = {k for k, v in self.provenance.items() if v}
        if kinds == {"synthetic-fixture"}:
            return "synthetic"
        if kinds == {"captured"}:
            return "captured"
        return "mixed" if kinds else "empty"

    def to_json(self) -> dict:
        def counter(c):
            return dict(c.most_common())

        return {
            "probe_format": PROBE_FORMAT,
            "generated_at": now_iso(),
            "provenance": self.overall_provenance(),
            "provenance_counts": counter(self.provenance),
            "inputs": self.inputs,
            "requests": counter(self.requests),
            "unrecognized_line_shapes": counter(self.unrecognized_shapes),
            "coverage": self.coverage(),
            "privacy": {k: {**v, "signals": sorted(v["signals"])} for k, v in sorted(self.privacy.items())},
            "token_views": self.token_views(),
            "token_rollup": self.token_rollup(),
            "resources": {s: {"count": r["count"], "attributes": r["attributes"].to_json()}
                          for s, r in sorted(self.resources.items())},
            "spans": {g: {"count": s["count"], "names": counter(s["names"]), "services": counter(s["services"]),
                          "scopes": counter(s["scopes"]), "kinds": counter(s["kinds"]),
                          "status": counter(s["status"]), "span_events": counter(s["events"]),
                          "parents": counter(s["parents"]), "duration_ms": s["duration_ms"],
                          "attributes": s["attributes"].to_json()} for g, s in sorted(self._with_parents().items())},
            "metrics": {n: {"type": counter(m["type"]), "unit": counter(m["unit"]), "description": m["description"],
                            "temporality": counter(m["temporality"]), "monotonic": counter(m["monotonic"]),
                            "services": counter(m["services"]), "points": m["points"],
                            "attributes": m["attributes"].to_json()} for n, m in sorted(self.metrics.items())},
            "events": {n: {"count": e["count"], "services": counter(e["services"]), "scopes": counter(e["scopes"]),
                           "severity": counter(e["severity"]), "body": counter(e["body_types"]),
                           "attributes": e["attributes"].to_json()} for n, e in sorted(self.events.items())},
        }

    def _with_parents(self) -> dict:
        index = {(r["trace"], r["id"]): r["group"] for r in self.span_records}
        for st in self.spans.values():
            st["parents"] = Counter()
        for r in self.span_records:
            if not r["parent"]:
                parent = "(root)"
            else:
                parent = index.get((r["trace"], r["parent"]), "(parent not captured)")
            self.spans[r["group"]]["parents"][parent] += 1
        return self.spans


def md_escape(s) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def attr_table(attrs: dict) -> list:
    if not attrs:
        return ["_(no attributes)_", ""]
    lines = ["| Attribute | Types | Count | Values / range |", "|---|---|---:|---|"]
    for key, a in attrs.items():
        if "values" in a:
            detail = ", ".join("`%s`×%d" % (md_escape(v), n) for v, n in a["values"].items())
        elif "min" in a:
            detail = "%s … %s" % (a["min"], a["max"])
        else:
            detail = "_value withheld_"
        flag = " ⚠ %s" % a["sensitive"] if a.get("sensitive") else ""
        lines.append("| `%s`%s | %s | %d | %s |" % (md_escape(key), flag, ", ".join(a["types"]), a["count"], detail))
    return lines + [""]


def render_markdown(s: dict) -> str:
    banner = {
        "synthetic": "> **PROVENANCE: SYNTHETIC FIXTURE.** Built from hand-written fixtures in "
                     "`probe/fixtures/synthetic/`. This is not telemetry from a real agent session.",
        "captured": "> **PROVENANCE: CAPTURED.** Received on an unauthenticated loopback port, so any local "
                    "process could have posted it. String values were redacted; keys, types, names and numbers kept.",
        "mixed": "> **PROVENANCE: MIXED.** Synthetic fixtures and captured data are combined in this report. "
                 "Do not treat it as a real-session schema.",
        "empty": "> **NO DATA.** No OTLP payloads were found in the inputs.",
    }[s["provenance"]]
    out = ["# Telemetry schema probe report", "", banner, "",
           "Generated %s from %s. Requests: %s. Unrecognized lines: %s." % (
               s["generated_at"], ", ".join("`%s` (%d lines)" % (i["path"], i["lines"]) for i in s["inputs"]) or "no inputs",
               ", ".join("%s %d" % kv for kv in s["requests"].items()) or "none",
               sum(s["unrecognized_line_shapes"].values()) or 0), ""]

    out += ["## Coverage: documented vs observed", "",
            "`not observed` means absent from this capture, not unsupported. Idle features emit nothing.", "",
            "| Harness | Kind | Name | Status |", "|---|---|---|---|"]
    for c in s["coverage"]:
        out.append("| %s | %s | `%s`%s | %s |" % (c["harness"], c["kind"], md_escape(c["name"]),
                                                 " (optional)" if c["optional"] else "", c["status"]))
    out += [""]

    out += ["## Privacy flags", "",
            "Keys that can carry content, identity or workspace identifiers. Values are never shown.", ""]
    if s["privacy"]:
        out += ["| Key | Category | Occurrences | With a value | Harness-redacted/empty | Signals |",
                "|---|---|---:|---:|---:|---|"]
        for key, p in s["privacy"].items():
            out.append("| `%s` | %s | %d | %d | %d | %s |" % (md_escape(key), p["category"], p["occurrences"],
                                                         p["with_value"], p["harness_redacted"], ", ".join(p["signals"])))
    else:
        out.append("_None seen._")
    out += [""]

    out += ["## Token-bearing fields", "",
            "**Each row is an alternative view of the same model calls. Never add rows together.** "
            "Pick one canonical source per harness (see docs/design.md).", "",
            "| Source | Service | Signal | Field | Total |", "|---|---|---|---|---:|"]
    for t in s["token_views"]:
        out.append("| %s | %s | `%s` | `%s` | %s |" % (t["source"], md_escape(t["service"]), md_escape(t["signal_name"]),
                                                   md_escape(t["field"]), t["total"]))
    out += ["", "### Parent/child token roll-up", "",
            "Compares token attributes on agent-level spans with the sum over their model-call descendants.", ""]
    if s["token_rollup"]:
        out += ["| Parent | Service | Field | Relation | Spans |", "|---|---|---|---|---:|"]
        for r in s["token_rollup"]:
            out.append("| `%s` | %s | `%s` | %s | %d |" % (md_escape(r["parent"]), md_escape(r["service"]),
                                                       md_escape(r["field"]), r["relation"], r["count"]))
    else:
        out.append("_No agent-level spans with token attributes._")
    out += [""]

    out += ["## Resources", ""]
    for svc, r in s["resources"].items():
        out += ["### `%s` (%d resource blocks)" % (md_escape(svc), r["count"]), ""] + attr_table(r["attributes"])

    out += ["## Spans", ""]
    for g, sp in s["spans"].items():
        out += ["### `%s` ×%d" % (md_escape(g), sp["count"]), "",
                "- names: %s" % ", ".join("`%s`" % md_escape(n) for n in list(sp["names"])[:10]),
                "- services: %s; kinds: %s; status: %s" % (", ".join(sp["services"]), ", ".join(sp["kinds"]),
                                                           ", ".join("%s×%d" % kv for kv in sp["status"].items())),
                "- parents: %s" % ", ".join("`%s`×%d" % (md_escape(p), n) for p, n in sp["parents"].items()),
                "- duration ms: %s" % ("%.1f … %.1f" % tuple(sp["duration_ms"]) if sp["duration_ms"][0] is not None else "n/a")]
        if sp["span_events"]:
            out.append("- span events: %s" % ", ".join("`%s`" % md_escape(e) for e in sp["span_events"]))
        out += [""] + attr_table(sp["attributes"])

    out += ["## Metrics", ""]
    for n, m in s["metrics"].items():
        out += ["### `%s`" % md_escape(n), "",
                "- type: %s; unit: %s; temporality: %s; monotonic: %s; points: %d; services: %s" % (
                    ", ".join(m["type"]), ", ".join("`%s`" % u for u in m["unit"]), ", ".join(m["temporality"]),
                    ", ".join(m["monotonic"]) or "n/a", m["points"], ", ".join(m["services"])), ""] + attr_table(m["attributes"])

    out += ["## Events (log records)", ""]
    for n, e in s["events"].items():
        out += ["### `%s` ×%d" % (md_escape(n), e["count"]), "",
                "- services: %s; scopes: %s; body: %s" % (", ".join(e["services"]), ", ".join(e["scopes"]),
                                                          ", ".join(e["body"])), ""] + attr_table(e["attributes"])
    if s["unrecognized_line_shapes"]:
        out += ["## Unrecognized lines", "", "Top-level keys only:", ""]
        out += ["- `%s` ×%d" % (md_escape(k), v) for k, v in s["unrecognized_line_shapes"].items()]
    return "\n".join(out) + "\n"


def collect_inputs(paths) -> list:
    files = []
    for p in map(Path, paths):
        if p.is_dir():
            files += sorted(p.glob("*.jsonl"))
        elif p.is_file():
            files.append(p)
        else:
            raise FileNotFoundError(str(p))
    return files


def build_schema(files, expectations_path: Path | None) -> dict:
    expectations = {}
    if expectations_path and expectations_path.is_file():
        expectations = json.loads(expectations_path.read_text(encoding="utf-8"))
    schema = Schema(expectations)
    for f in files:
        schema.add_file(f)
    return schema.to_json()


def write_schema(result: dict, out_dir: Path) -> tuple:
    out_dir.mkdir(parents=True, exist_ok=True)
    jpath, mpath = out_dir / "schema.json", out_dir / "schema.md"
    jpath.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    mpath.write_text(render_markdown(result), encoding="utf-8")
    return jpath, mpath


def cmd_schema(args) -> int:
    try:
        files = collect_inputs(args.paths)
    except FileNotFoundError as exc:
        print("Input not found: %s" % exc, file=sys.stderr)
        return 2
    if not files:
        print("No .jsonl files found in %s" % ", ".join(args.paths), file=sys.stderr)
        return 2
    result = build_schema(files, Path(args.expectations) if args.expectations else DEFAULT_EXPECTATIONS)
    out_dir = Path(args.out) if args.out else files[0].parent
    jpath, mpath = write_schema(result, out_dir)
    print("Provenance: %s | requests: %s" % (result["provenance"].upper(), result["requests"] or "none"))
    print("Wrote %s\n      %s" % (jpath, mpath))
    missing = [c for c in result["coverage"] if c["status"] == "not observed in this capture" and not c["optional"]]
    if missing:
        print("Documented but not observed (%d): %s" % (len(missing), ", ".join(c["name"] for c in missing[:12])))
    return 0


# --------------------------------------------------------------------------
# Self-test (synthetic fixtures only)
# --------------------------------------------------------------------------
CANARIES = ("CANARY-PROMPT", "canary@example.invalid", "CANARY-PATH", "CANARY-COMMAND", "CANARY-ERROR")


def post(port: int, path: str, body: bytes, ctype: str = "application/json", gzip_body: bool = False,
         chunked: bool = False) -> int:
    headers = {"Content-Type": ctype}
    if gzip_body:
        c = zlib.compressobj(wbits=16 + zlib.MAX_WBITS)
        body = c.compress(body) + c.flush()
        headers["Content-Encoding"] = "gzip"
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        if chunked:
            conn.request("POST", path, body=iter([body[: len(body) // 2], body[len(body) // 2:]]),
                         headers=headers, encode_chunked=True)
        else:
            conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        resp.read()
        return resp.status
    finally:
        conn.close()


def cmd_selftest(args) -> int:
    fixtures = sorted(FIXTURE_DIR.glob("*.json"))
    if not fixtures:
        print("No fixtures in %s" % FIXTURE_DIR, file=sys.stderr)
        return 2
    checks = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    with tempfile.TemporaryDirectory(prefix="adw-probe-selftest-") as td:
        cap = Path(td) / "capture"
        cap.mkdir()
        rx = Receiver(cap, quiet=True)
        server = make_server(rx, 0)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        thread.start()
        try:
            statuses = []
            for i, f in enumerate(fixtures):
                signal = f.name.rsplit(".", 2)[-2]
                statuses.append(post(port, "/v1/" + signal, f.read_bytes(), gzip_body=(i % 2 == 0), chunked=(i % 3 == 1)))
            check("receiver accepts every fixture (gzip, chunked and plain bodies)",
                  all(s == 200 for s in statuses), "statuses=%s" % statuses)
            check("receiver rejects protobuf with 415", post(port, "/v1/traces", b"\x0a\x00", "application/x-protobuf") == 415)
            check("receiver rejects unknown path with 404", post(port, "/v1/other", b"{}") == 404)
            try:
                make_server(Receiver(cap, quiet=True), port).server_close()
                shared = True
            except OSError:
                shared = False
            check("a second receiver cannot bind the same port", not shared)
        finally:
            server.shutdown()
            server.server_close()

        envelopes = [json.loads(line) for line in (cap / "otlp.jsonl").read_text(encoding="utf-8").splitlines()]
        accepted = [e for e in envelopes if e.get("payload") is not None]
        check("every accepted envelope is labeled synthetic-fixture",
              accepted and all(e["provenance"] == "synthetic-fixture" for e in accepted))

        result = build_schema([cap / "otlp.jsonl"], DEFAULT_EXPECTATIONS)
        out_dir = Path(args.out) if args.out else cap
        jpath, mpath = write_schema(result, out_dir)
        check("report provenance is SYNTHETIC", result["provenance"] == "synthetic")
        check("span operations invoke_agent, chat, execute_tool found",
              {"invoke_agent", "chat", "execute_tool"} <= set(result["spans"]))
        check("nested agent is parented by execute_tool",
              "execute_tool" in result["spans"].get("invoke_agent", {}).get("parents", {}))
        rollup = {(r["parent"], r["field"]): r["relation"] for r in result["token_rollup"]}
        check("root agent input tokens recognized as roll-up incl. nested agents",
              any(rel == "equals sum of descendant model calls (incl. nested agents)"
                  for (p, f), rel in rollup.items() if p == "invoke_agent" and f == "gen_ai.usage.input_tokens"),
              str(rollup))
        views = {(t["source"], t["signal_name"], t["field"]): t["total"] for t in result["token_views"]}
        check("Claude Code token metric and api_request events agree on input tokens",
              views.get(("metric", "claude_code.token.usage", "type=input")) ==
              views.get(("event", "claude_code.api_request", "input_tokens")) == 1600, str(views))
        check("delta temporality detected on claude_code.token.usage",
              "DELTA" in result["metrics"].get("claude_code.token.usage", {}).get("temporality", {}))
        priv = result["privacy"]
        check("content key with a value is flagged", priv.get("gen_ai.input.messages", {}).get("with_value") == 1)
        check("harness-redacted prompt is reported as redacted", priv.get("prompt", {}).get("harness_redacted") == 1)
        check("identity key is flagged", priv.get("user.email", {}).get("occurrences", 0) >= 1)
        cov = {(c["harness"], c["name"]): c["status"] for c in result["coverage"]}
        check("coverage marks execute_hook as not observed",
              cov.get(("vscode-copilot-chat", "execute_hook")) == "not observed in this capture")
        check("coverage marks codex as absent", cov.get(("codex", "codex.api_request")) == "harness not in capture")
        blob = "\n".join(p.read_text(encoding="utf-8") for p in (cap / "otlp.jsonl", jpath, mpath))
        leaked = [c for c in CANARIES if c in blob]
        check("no canary value survives in capture or reports", not leaked, "leaked=%s" % leaked)

    width = max(len(n) for n, _, _ in checks)
    for name, ok, detail in checks:
        print("%s  %-*s %s" % ("PASS" if ok else "FAIL", width, name, "" if ok else detail))
    failed = sum(1 for _, ok, _ in checks if not ok)
    print("\nSYNTHETIC self-test: %d/%d checks passed." % (len(checks) - failed, len(checks)))
    if args.out:
        print("Synthetic report written to %s" % Path(args.out))
    return 1 if failed else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("receive", help="listen for OTLP/HTTP JSON on 127.0.0.1")
    p.add_argument("--port", type=int, default=4318)
    p.add_argument("--out", help="capture directory (default: probe/captures/<timestamp>-<label>)")
    p.add_argument("--label", default="session")
    p.add_argument("--store-raw-values", action="store_true",
                   help="store payloads verbatim (may include prompts, identity, paths)")
    p.set_defaults(func=cmd_receive)
    p = sub.add_parser("schema", help="summarize capture files into schema.json and schema.md")
    p.add_argument("paths", nargs="+", help="capture directories or .jsonl files")
    p.add_argument("--out", help="output directory (default: directory of the first input)")
    p.add_argument("--expectations", help="expectations JSON (default: probe/expectations.json)")
    p.set_defaults(func=cmd_schema)
    p = sub.add_parser("selftest", help="run against synthetic fixtures in a temp directory")
    p.add_argument("--out", help="also write the synthetic report here")
    p.set_defaults(func=cmd_selftest)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
