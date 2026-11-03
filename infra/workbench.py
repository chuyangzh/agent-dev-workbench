#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Verification and self-test for the agent-dev-workbench monitoring stack.

Standard library only (Python 3.9+). Every read goes through Grafana's
authenticated API on loopback, because Prometheus, Tempo and Loki are not
published on the host (see compose.yaml).

  verify    Read-only. What the store holds per harness and signal, which labels
            reach metrics, whether any forbidden label or canary string is
            stored, and the result of every dashboard query.
  selftest  Start a separate, throwaway stack (compose project adw-selftest on
            ports 13000/14317/14318), send the SYNTHETIC fixtures from
            probe/fixtures/synthetic with their timestamps moved to now, then
            check storage, filtering and every dashboard query against known
            answers. The stack and its volumes are deleted afterwards.

Approach adapted from microsoft/hve-core verify.py, inspect_metrics.py and
validate_dashboard.py (MIT): prove data from the store rather than from an
HTTP 200, enumerate names before trusting them, run each panel's query. The
code here is new; see THIRD_PARTY_NOTICES.md.
"""
from __future__ import annotations

import argparse
import base64
import copy
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
FIXTURES = REPO / "probe" / "fixtures" / "synthetic"
DASHBOARD_DIR = HERE / "grafana" / "dashboards"
OUT_DIR = HERE / "out"
ENV_FILE = HERE / ".env"
MAIN_GRAFANA = "http://127.0.0.1:3000"

SELFTEST_PROJECT = "adw-selftest"
SELFTEST_PORTS = {"ADW_GRAFANA_PORT": 13000, "ADW_OTLP_HTTP_PORT": 14318, "ADW_OTLP_GRPC_PORT": 14317}

# Labels that must never reach Prometheus: per-session or per-user identifiers
# and content-bearing keys. workbench_task_id is allowed by design (bounded,
# user-chosen; docs/design.md).
FORBIDDEN_METRIC_LABELS = {
    "session_id", "gen_ai_conversation_id", "prompt_id", "user_email", "user_id",
    "user_account_uuid", "user_account_id", "organization_id", "service_instance_id",
    "host_name", "terminal_type", "gen_ai_tool_call_id", "gen_ai_response_id",
    "workbench_fixture", "error", "tool_parameters", "prompt",
}
CANARY_RE = re.compile(r"CANARY-|canary@example\.invalid", re.I)
METRIC_PREFIXES = ("gen_ai_", "copilot_chat_", "claude_code_", "codex_")


# --------------------------------------------------------------------------
# Grafana client (loopback only, no redirects, credential sent only to Grafana)
# --------------------------------------------------------------------------
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        raise urllib.error.HTTPError(req.full_url, code, f"redirect refused -> {newurl}", headers, fp)


OPENER = urllib.request.build_opener(NoRedirect())


def is_loopback_url(url: str) -> bool:
    p = urllib.parse.urlsplit(url)
    return p.scheme == "http" and p.hostname in ("127.0.0.1", "localhost", "::1") and not p.username


class Grafana:
    def __init__(self, base: str, user: str, password: str):
        if not is_loopback_url(base):
            raise SystemExit(f"refusing non-loopback Grafana URL: {base}")
        self.base = base.rstrip("/")
        self._auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

    def call(self, method: str, path: str, params: dict | None = None, body=None, timeout: int = 30):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", self._auth)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with OPENER.open(req, timeout=timeout) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            raw = exc.read() if exc.fp else b""
            try:
                return exc.code, json.loads(raw)
            except ValueError:
                return exc.code, {"message": raw.decode("utf-8", "replace")[:300]}

    # Datasource proxies (uids pre-provisioned by grafana/otel-lgtm).
    def prom(self, path: str, params: dict) -> dict:
        status, body = self.call("GET", "/api/datasources/proxy/uid/prometheus" + path, params)
        if status != 200:
            raise RuntimeError(f"prometheus {path}: HTTP {status} {body}")
        return body

    def prom_query(self, expr: str) -> list:
        return self.prom("/api/v1/query", {"query": expr})["data"]["result"]

    def loki(self, path: str, params: dict) -> dict:
        status, body = self.call("GET", "/api/datasources/proxy/uid/loki" + path, params)
        if status != 200:
            raise RuntimeError(f"loki {path}: HTTP {status} {body}")
        return body

    def tempo(self, path: str, params: dict | None = None) -> dict:
        status, body = self.call("GET", "/api/datasources/proxy/uid/tempo" + path, params)
        if status != 200:
            raise RuntimeError(f"tempo {path}: HTTP {status} {body}")
        return body

    def ds_query(self, queries: list, frm: str, to: str) -> dict:
        status, body = self.call("POST", "/api/ds/query", body={"queries": queries, "from": frm, "to": to}, timeout=60)
        if status not in (200, 207, 400, 500):
            raise RuntimeError(f"/api/ds/query: HTTP {status} {body}")
        return body or {}


def read_env(path: Path) -> dict:
    env = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def main_grafana() -> Grafana:
    env = read_env(ENV_FILE)
    user = os.environ.get("ADW_GRAFANA_USER") or env.get("ADW_GRAFANA_USER")
    password = os.environ.get("ADW_GRAFANA_PASSWORD") or env.get("ADW_GRAFANA_PASSWORD")
    if not user or not password:
        raise SystemExit("No Grafana login: run .\\infra\\stack.ps1 init (writes infra\\.env), then up.")
    return Grafana(os.environ.get("ADW_GRAFANA_URL", MAIN_GRAFANA), user, password)


# --------------------------------------------------------------------------
# Dashboard queries
# --------------------------------------------------------------------------
def load_dashboards() -> list:
    out = []
    for p in sorted(DASHBOARD_DIR.glob("*.json")):
        out.append((p, json.loads(p.read_text(encoding="utf-8"))))
    return out


def variable_defaults(dash: dict) -> dict:
    values = {}
    for v in dash.get("templating", {}).get("list", []):
        cur = v.get("current", {}).get("value")
        if v.get("includeAll") and (cur in ("$__all", ["$__all"]) or cur is None):
            values[v["name"]] = v.get("allValue") or ".*"
        elif isinstance(cur, list):
            values[v["name"]] = "|".join(cur)
        elif cur is not None:
            values[v["name"]] = str(cur)
        elif v.get("query") is not None and v.get("type") in ("textbox", "constant"):
            values[v["name"]] = str(v["query"])
    return values


def substitute(expr: str, values: dict) -> str:
    # Longest names first so $task does not clobber $task_x.
    for name in sorted(values, key=len, reverse=True):
        expr = expr.replace("${" + name + "}", values[name]).replace("$" + name, values[name])
    return expr


def iter_targets(dash: dict):
    def walk(panels):
        for p in panels:
            if p.get("type") == "row":
                yield from walk(p.get("panels", []))
                continue
            if p.get("type") == "text":
                continue
            for t in p.get("targets", []):
                yield p, t
    yield from walk(dash.get("panels", []))


def build_query(panel: dict, target: dict, values: dict) -> dict:
    ds = target.get("datasource") or panel.get("datasource") or {}
    q = {k: v for k, v in target.items() if k not in ("datasource",)}
    q["datasource"] = {"uid": ds.get("uid"), "type": ds.get("type")}
    for key in ("expr", "query"):
        if isinstance(q.get(key), str):
            q[key] = substitute(q[key], values)
    q.setdefault("intervalMs", 60000)
    q.setdefault("maxDataPoints", 300)
    return q


def frame_rows(result: dict) -> list:
    """Flatten Grafana data frames into (labels, last numeric value or row count)."""
    rows = []
    for frame in result.get("frames") or []:
        fields = frame.get("schema", {}).get("fields", [])
        values = frame.get("data", {}).get("values", [])
        if not fields or not values:
            continue
        num_idx = [i for i, f in enumerate(fields) if f.get("type") == "number"]
        if num_idx and len(fields) <= 3:
            for i in num_idx:
                col = [x for x in values[i] if x is not None]
                if col:
                    labels = dict(fields[i].get("labels") or {})
                    rows.append((labels, col[-1]))
        else:
            n = len(values[0]) if values else 0
            for r in range(n):
                labels = {f.get("name"): values[i][r] for i, f in enumerate(fields) if i < len(values)}
                rows.append((labels, None))
    return rows


def run_dashboard(gf: Grafana, overrides: dict, frm: str = "now-6h", to: str = "now") -> list:
    report = []
    for path, dash in load_dashboards():
        values = variable_defaults(dash)
        values.update(overrides)
        for panel, target in iter_targets(dash):
            q = build_query(panel, target, values)
            q["refId"] = target.get("refId", "A")
            body = gf.ds_query([q], frm, to)
            res = (body.get("results") or {}).get(q["refId"], {})
            err = res.get("error")
            rows = [] if err else frame_rows(res)
            report.append({
                "dashboard": dash.get("uid"), "panel_id": panel.get("id"), "panel": panel.get("title"),
                "refId": q["refId"], "datasource": q["datasource"]["uid"],
                "query": q.get("expr") or q.get("query"), "error": err, "rows": rows,
            })
    return report


def print_dashboard_report(report: list) -> tuple:
    ok = empty = errors = 0
    for r in report:
        if r["error"]:
            errors += 1
            status = "ERROR"
        elif r["rows"]:
            ok += 1
            status = "DATA "
        else:
            empty += 1
            status = "EMPTY"
        summary = ""
        if r["rows"]:
            parts = []
            for labels, value in r["rows"][:4]:
                lab = ",".join(f"{k}={v}" for k, v in sorted(labels.items()) if k not in ("Time",))[:70]
                parts.append(f"{lab}: {fmt_num(value)}" if value is not None else lab)
            summary = "; ".join(parts) + (f" (+{len(r['rows']) - 4})" if len(r["rows"]) > 4 else "")
        elif r["error"]:
            summary = str(r["error"])[:120]
        print(f"  [{status}] #{r['panel_id']:<3} {r['panel'][:46]:<46} {r['refId']:<2} {summary}")
    return ok, empty, errors


def fmt_num(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float) and not v.is_integer():
        return f"{v:.4g}"
    return str(int(v)) if isinstance(v, (int, float)) else str(v)


# --------------------------------------------------------------------------
# Store inventory
# --------------------------------------------------------------------------
def inventory(gf: Grafana, provenance: str | None, lookback_s: int = 6 * 3600) -> dict:
    sel = f'{{workbench_provenance="{provenance}"}}' if provenance else "{workbench_provenance=~\".+\"}"
    inv: dict = {"metrics": {}, "metric_labels": [], "target_info_labels": [], "spans": {}, "logs": {}}
    names = gf.prom("/api/v1/label/__name__/values", {})["data"]
    agent_names = sorted(n for n in names if n.startswith(METRIC_PREFIXES))
    for n in agent_names:
        res = gf.prom_query(f"count by (workbench_harness, workbench_provenance) (last_over_time({n}{sel}[{lookback_s}s]))")
        for r in res:
            key = (r["metric"].get("workbench_harness", "?"), r["metric"].get("workbench_provenance", "?"))
            inv["metrics"].setdefault("/".join(key), []).append(n)
    labels = set()
    for n in agent_names:
        series = gf.prom("/api/v1/series", {"match[]": n, "start": str(int(time.time()) - lookback_s)})["data"]
        for s in series:
            labels.update(s.keys())
    inv["metric_labels"] = sorted(labels - {"__name__"})
    ti = set()
    # Only resources that came through the workbench collector (otel-lgtm's own
    # internal collector reports its service.instance.id on target_info).
    for s in gf.prom("/api/v1/series", {"match[]": 'target_info{workbench_provenance=~".+"}',
                                        "start": str(int(time.time()) - lookback_s)})["data"]:
        ti.update(s.keys())
    inv["target_info_labels"] = sorted(ti - {"__name__"})
    now = int(time.time())
    for harness in ("vscode-copilot-chat", "github-copilot-runtime", "claude-code", "codex", "other"):
        q = f'{{resource.workbench.harness="{harness}"' + (f' && resource.workbench.provenance="{provenance}"' if provenance else "") + "}"
        try:
            res = gf.tempo("/api/search", {"q": q, "start": now - lookback_s, "end": now, "limit": 200, "spss": 50})
            traces = res.get("traces") or []
            if traces:
                inv["spans"][harness] = {"traces": len(traces), "root_names": sorted({t.get("rootTraceName", "?") for t in traces})[:8]}
        except RuntimeError as exc:
            inv["spans"][harness] = {"error": str(exc)[:160]}
    lq = '{service_name=~".+"}' + (f' | workbench_provenance="{provenance}"' if provenance else "")
    try:
        res = gf.loki("/loki/api/v1/query", {
            "query": f"sum by (workbench_harness, event_name) (count_over_time({lq} [{lookback_s}s]))",
            "time": str(now)})
        for r in res["data"]["result"]:
            h = r["metric"].get("workbench_harness", "?")
            inv["logs"].setdefault(h, {})[r["metric"].get("event_name", "(none)")] = int(float(r["value"][1]))
    except RuntimeError as exc:
        inv["logs"]["error"] = str(exc)[:160]
    return inv


def scan_for_canaries(gf: Grafana, lookback_s: int = 6 * 3600) -> list:
    """Look for canary strings anywhere the store can return them."""
    hits = []
    now = int(time.time())
    for label in gf.prom("/api/v1/labels", {"start": str(now - lookback_s)})["data"]:
        vals = gf.prom(f"/api/v1/label/{urllib.parse.quote(label)}/values", {"start": str(now - lookback_s)})["data"]
        hits += [f"prometheus label {label}" for v in vals if CANARY_RE.search(v)]
    res = gf.loki("/loki/api/v1/query_range", {
        "query": '{service_name=~".+"}', "start": str((now - lookback_s) * 10**9), "end": str(now * 10**9), "limit": 5000})
    for stream in res["data"]["result"]:
        if CANARY_RE.search(json.dumps(stream)):
            hits.append(f"loki stream {stream.get('stream', {}).get('service_name')}")
    traces = gf.tempo("/api/search", {"q": "{}", "start": now - lookback_s, "end": now, "limit": 200}).get("traces") or []
    for t in traces:
        full = gf.tempo(f"/api/traces/{t['traceID']}")
        if CANARY_RE.search(json.dumps(full)):
            hits.append(f"tempo trace {t['traceID'][:12]}")
    return hits


def cmd_verify(args) -> int:
    gf = main_grafana()
    status, health = gf.call("GET", "/api/health")
    print(f"Grafana {gf.base}: HTTP {status} {health.get('version') if isinstance(health, dict) else ''}")
    if status != 200:
        return 1
    status, _ = gf.call("GET", "/api/search", {"query": ""})
    if status == 401:
        print("Grafana rejected the login in infra\\.env (it adopts it only on first start; see docs/monitoring.md).")
        return 1
    inv = inventory(gf, args.provenance)
    print("\nStore inventory (last 6h):")
    for key, names in sorted(inv["metrics"].items()):
        print(f"  metrics  {key:<34} {len(names)} names: {', '.join(names[:6])}{' ...' if len(names) > 6 else ''}")
    if not inv["metrics"]:
        print("  metrics  none")
    for h, info in sorted(inv["spans"].items()):
        print(f"  traces   {h:<34} {info}")
    if not inv["spans"]:
        print("  traces   none")
    for h, info in sorted(inv["logs"].items()):
        print(f"  logs     {h:<34} {info}")
    if not inv["logs"]:
        print("  logs     none")
    bad = sorted(set(inv["metric_labels"]) & FORBIDDEN_METRIC_LABELS)
    bad_ti = sorted(set(inv["target_info_labels"]) & FORBIDDEN_METRIC_LABELS)
    print(f"\nMetric labels in use: {', '.join(inv['metric_labels']) or 'none'}")
    print(f"Forbidden metric labels present: {bad or 'none'}; on target_info: {bad_ti or 'none'}")
    canaries = scan_for_canaries(gf)
    print(f"Canary strings in the store: {canaries or 'none'}")
    if args.brief:
        return 1 if (bad or bad_ti or canaries) else 0
    print("\nDashboard queries (provenance=%s):" % (args.provenance or "captured"))
    overrides = {"provenance": args.provenance or "captured"}
    if args.task:
        overrides["task"] = args.task
    report = run_dashboard(gf, overrides, frm=f"now-{args.range}")
    ok, empty, errors = print_dashboard_report(report)
    print(f"\n{ok} queries with data, {empty} empty (no data in range: shown as 'No data' in Grafana), {errors} errors")
    OUT_DIR.mkdir(exist_ok=True)
    out = OUT_DIR / f"verify-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps({"inventory": inv, "dashboard": report, "canaries": canaries,
                               "forbidden_labels": bad + bad_ti}, indent=2, default=str), encoding="utf-8")
    print(f"Report: {out}")
    return 1 if (errors or bad or bad_ti or canaries) else 0


# --------------------------------------------------------------------------
# Self-test on synthetic fixtures in an isolated stack
# --------------------------------------------------------------------------
def shift_times(node, delta_ns: int):
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k.endswith("UnixNano") and isinstance(v, (str, int)) and str(v) not in ("0", ""):
                out[k] = str(int(v) + delta_ns)
            else:
                out[k] = shift_times(v, delta_ns)
        return out
    if isinstance(node, list):
        return [shift_times(x, delta_ns) for x in node]
    return node


def max_time(node) -> int:
    best = 0
    if isinstance(node, dict):
        for k, v in node.items():
            if k.endswith("UnixNano") and isinstance(v, (str, int)):
                best = max(best, int(v))
            else:
                best = max(best, max_time(v))
    elif isinstance(node, list):
        for x in node:
            best = max(best, max_time(x))
    return best


def load_fixtures() -> list:
    out = []
    for p in sorted(FIXTURES.glob("*.json")):
        payload = json.loads(p.read_text(encoding="utf-8"))
        signal = "traces" if "resourceSpans" in payload else "metrics" if "resourceMetrics" in payload else "logs"
        out.append((p.name, signal, payload))
    return out


def derived_fixtures() -> list:
    """SYNTHETIC variants built in memory from the fixtures (never written to disk).

    A copy of the VS Code metrics fixture under service.name "github-copilot" and
    task fixture-task-2: it checks that the collector gives that service the
    neutral github-copilot-runtime label, and that no panel mixes its series into
    the VS Code extension's numbers. It keeps workbench.fixture=synthetic.
    """
    src = json.loads((FIXTURES / "vscode-copilot.metrics.json").read_text(encoding="utf-8"))
    variant = copy.deepcopy(src)
    for rm in variant["resourceMetrics"]:
        for attr in rm["resource"]["attributes"]:
            if attr["key"] == "service.name":
                attr["value"] = {"stringValue": "github-copilot"}
            elif attr["key"] == "workbench.task.id":
                attr["value"] = {"stringValue": "fixture-task-2"}
            elif attr["key"] == "workbench.harness":
                # Sender-supplied harness label; the collector must ignore it.
                attr["value"] = {"stringValue": "vscode-copilot-chat"}
    return [("vscode-copilot.metrics.json as service github-copilot (derived)", "metrics", variant)]


def post_otlp(port: int, signal: str, payload: dict) -> int:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/{signal}", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    with OPENER.open(req, timeout=15) as resp:
        return resp.status


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) != 0


def compose(env: dict, *args) -> int:
    cmd = ["docker", "compose", "--project-directory", str(HERE), "-f", str(HERE / "compose.yaml"),
           "-p", SELFTEST_PROJECT, *args]
    return subprocess.call(cmd, env=env)


def wait_for(fn, timeout: float, interval: float = 3.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = fn()
            if last:
                return last
        except (RuntimeError, OSError, urllib.error.URLError) as exc:
            last = None
            _ = exc
        time.sleep(interval)
    return last


# Known answers, computed by hand from probe/fixtures/synthetic. Each entry:
# panel id -> list of (label subset, expected value). Panel ids are stable in
# the dashboard JSON; titles are shown in the report.
def selftest_expectations() -> dict:
    return json.loads((HERE / "selftest-expected.json").read_text(encoding="utf-8"))


def rows_match(rows: list, want_labels: dict, want_value) -> tuple:
    for labels, value in rows:
        if all(str(labels.get(k)) == str(v) for k, v in want_labels.items()):
            if want_value is None:
                return True, value
            if value is not None and abs(float(value) - float(want_value)) <= max(1e-6, abs(float(want_value)) * 1e-6):
                return True, value
            return False, value
    return False, None


def cmd_selftest(args) -> int:
    for name, port in SELFTEST_PORTS.items():
        if not port_free(port):
            print(f"Port {port} ({name}) is in use. Is an old selftest stack running? docker compose -p {SELFTEST_PROJECT} down -v")
            return 2
    user, password = "selftest", secrets.token_urlsafe(18)
    env = dict(os.environ)
    env.update({k: str(v) for k, v in SELFTEST_PORTS.items()})
    env.update({"ADW_GRAFANA_USER": user, "ADW_GRAFANA_PASSWORD": password})
    checks = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(': ' + detail) if detail else ''}")

    print(f"Starting throwaway stack '{SELFTEST_PROJECT}' (Grafana :13000, OTLP :14318). SYNTHETIC data only.")
    rc = compose(env, "up", "-d", "--wait", "--wait-timeout", "300")
    if rc != 0:
        print("compose up failed")
        compose(env, "down", "-v")
        return 2
    try:
        gf = Grafana("http://127.0.0.1:13000", user, password)
        fixtures = load_fixtures() + derived_fixtures()
        newest = max(max_time(p) for _, _, p in fixtures)
        delta = time.time_ns() - 30 * 10**9 - newest
        for name, signal, payload in fixtures:
            status = post_otlp(14318, signal, shift_times(copy.deepcopy(payload), delta))
            print(f"  sent {name} ({signal}) -> HTTP {status}")

        print("\nWaiting for the store (Prometheus at once; Tempo and Loki can take ~30 s)...")
        got_metrics = wait_for(lambda: gf.prom_query('count({workbench_provenance="synthetic"})'), 90)
        now = int(time.time())
        got_traces = wait_for(lambda: gf.tempo("/api/search", {"q": '{resource.workbench.provenance="synthetic"}',
                                                               "start": now - 3600, "end": now + 600}).get("traces"), 120)
        got_logs = wait_for(lambda: gf.loki("/loki/api/v1/query", {
            "query": 'sum(count_over_time({service_name=~".+"} | workbench_provenance="synthetic" [1h]))',
            "time": str(int(time.time()))})["data"]["result"], 120)
        print("\nStorage:")
        check("metrics stored", bool(got_metrics), f"{got_metrics[0]['value'][1] if got_metrics else 0} series")
        check("traces stored", bool(got_traces), f"{len(got_traces or [])} traces")
        check("logs stored", bool(got_logs), f"{got_logs[0]['value'][1] if got_logs else 0} records")
        time.sleep(5)

        inv = inventory(gf, "synthetic", lookback_s=3600)
        check("both harnesses labelled from service.name",
              {"vscode-copilot-chat/synthetic", "github-copilot-runtime/synthetic", "claude-code/synthetic"}
              <= set(inv["metrics"]),
              ", ".join(sorted(inv["metrics"])))
        bad = sorted(set(inv["metric_labels"]) & FORBIDDEN_METRIC_LABELS)
        check("no session/user/content labels on metrics", not bad, f"present: {bad}" if bad else
              "labels: " + ", ".join(inv["metric_labels"]))
        bad_ti = sorted(set(inv["target_info_labels"]) & FORBIDDEN_METRIC_LABELS)
        check("no identity labels on target_info", not bad_ti, f"present: {bad_ti}" if bad_ti else
              "labels: " + ", ".join(inv["target_info_labels"]))
        canaries = scan_for_canaries(gf, lookback_s=3600)
        check("no canary (prompt/identity/path stand-in) in Prometheus, Loki or Tempo", not canaries, ", ".join(canaries))
        captured = gf.prom_query('count({workbench_provenance="captured"})')
        check("nothing labelled 'captured' in the synthetic stack", not captured)

        print("\nDashboard queries (provenance=synthetic, task=All):")
        report = run_dashboard(gf, {"provenance": "synthetic"}, frm="now-1h")
        ok, empty, errors = print_dashboard_report(report)
        check("every dashboard query runs without error", errors == 0, f"{errors} errors")
        expected = selftest_expectations()
        by_panel = {}
        for r in report:
            by_panel.setdefault(str(r["panel_id"]) + r["refId"], r)
        print("\nKnown answers from the fixtures:")
        for key, spec in expected["panels"].items():
            r = by_panel.get(key)
            if r is None:
                check(f"panel {key} exists", False)
                continue
            for want in spec["rows"]:
                ok_row, got = rows_match(r["rows"], want.get("labels", {}), want.get("value"))
                if want.get("value") is None:
                    detail = "row present" if ok_row else "no row"
                else:
                    detail = f"expected {want.get('value')}, got {fmt_num(got) if got is not None else 'no row'}"
                check(f"{r['panel']} [{key}] {want.get('labels', {})}", ok_row, detail)
            if spec.get("absent"):
                for labels in spec["absent"]:
                    ok_row, got = rows_match(r["rows"], labels, None)
                    check(f"{r['panel']} [{key}] has no row {labels}", not ok_row)

        print("\nProvenance filter (provenance=captured must show nothing here):")
        cap = run_dashboard(gf, {"provenance": "captured"}, frm="now-1h")
        coverage_ids = set(expected.get("coverage_panels", []))
        constant_ids = set(expected.get("constant_panels", []))
        leaked = [r for r in cap if r["rows"] and r["panel_id"] not in coverage_ids | constant_ids]
        check("captured view of a synthetic-only store shows no data", not leaked,
              ", ".join(f"#{r['panel_id']} {r['panel']}" for r in leaked[:5]))
        cov = [r for r in cap if r["panel_id"] in coverage_ids]
        all_missing = all(v == -1 for r in cov for _, v in r["rows"]) and all(r["rows"] for r in cov)
        check("coverage table marks every expected signal 'not seen' (-1), never 0", all_missing)
    finally:
        if args.keep:
            print(f"\n--keep: stack left running. Grafana http://127.0.0.1:13000 user '{user}'. Remove with: "
                  f"docker compose -p {SELFTEST_PROJECT} down -v")
        else:
            print("\nRemoving the throwaway stack and its volumes...")
            compose(env, "down", "-v")
    passed = sum(1 for _, ok, _ in checks if ok)
    print(f"\n{passed}/{len(checks)} checks passed (SYNTHETIC fixtures; says nothing about real harness output)")
    return 0 if passed == len(checks) else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify")
    v.add_argument("--provenance", choices=["captured", "synthetic"], default=None)
    v.add_argument("--task", default=None)
    v.add_argument("--range", default="6h")
    v.add_argument("--brief", action="store_true")
    s = sub.add_parser("selftest")
    s.add_argument("--keep", action="store_true")
    args = ap.parse_args(argv)
    return {"verify": cmd_verify, "selftest": cmd_selftest}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
