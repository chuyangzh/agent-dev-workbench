#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Generate infra/grafana/dashboards/workbench.json (the provisioned dashboard).

    python -I infra/grafana/build_dashboard.py          # write the JSON
    python -I infra/grafana/build_dashboard.py --check  # exit 1 if the file is stale

Rules the generated panels follow (docs/monitoring.md, docs/design.md):

* One canonical token source per harness, never summed across signals:
  VS Code Copilot -> gen_ai.client.token.usage (metric, per model call);
  Claude Code -> claude_code.token.usage (metric). invoke_agent span usage and
  Claude api_request event tokens appear only in the roll-up check panels.
* Exact totals: counters use `increase(x[range] anchored)` (Prometheus
  promql-extended-range-selectors) on series that start with a zero sample
  (created-timestamp-zero-ingestion). Plain increase() extrapolates and was
  measured overcounting a single export by 1.5x.
* Missing is not zero: no `or vector(0)` anywhere; every panel has a noValue
  text; the coverage table marks expected-but-absent signals "not seen".
* Session and task identifiers: workbench_task_id is the only ID on metrics;
  sessions are counted from logs/traces, where IDs belong.
* Synthetic data is excluded unless the provenance variable says otherwise.

Prometheus names below were confirmed against the store (synthetic fixtures
through this collector, then a real capture where noted in docs/monitoring.md);
regex name matchers are used where the unit suffix is not yet confirmed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent / "dashboards" / "workbench.json"

PROM = {"type": "prometheus", "uid": "prometheus"}
LOKI = {"type": "loki", "uid": "loki"}
TEMPO = {"type": "tempo", "uid": "tempo"}
MIXED = {"type": "datasource", "uid": "-- Mixed --"}
TEXT_DS = {"type": "datasource", "uid": "grafana"}

SEL = 'workbench_provenance="$provenance",workbench_task_id=~"$task"'
LSEL = '| workbench_provenance="$provenance" | workbench_task_id=~"$task"'
TSEL = 'resource.workbench.provenance="$provenance" && resource.workbench.task.id=~"$task"'
NO_DATA = "No data in range (not zero) - see Coverage"

# Canonical token series (Prometheus names confirmed in the store).
VS_TOKENS = "gen_ai_client_token_usage_sum"
CC_TOKENS = "claude_code_token_usage_tokens_total"
VS_TOOL_COUNT = "copilot_chat_tool_call_count_total"
VS_TOOL_DUR = "copilot_chat_tool_call_duration_milliseconds"
VS_OP_DUR = "gen_ai_client_operation_duration_seconds"
VS_TURNS = "copilot_chat_agent_turn_count"
CLAUDE_EVENTS = '{service_name="claude-code"} ' + LSEL

# Expected signals per harness for the coverage table: (harness, label, name regex).
EXPECTED_METRICS = [
    ("vscode-copilot-chat", "gen_ai.client.token.usage", "gen_ai_client_token_usage_sum"),
    ("vscode-copilot-chat", "gen_ai.client.operation.duration", "gen_ai_client_operation_duration.*_count"),
    ("vscode-copilot-chat", "copilot_chat.tool.call.count", "copilot_chat_tool_call_count.*_total"),
    ("vscode-copilot-chat", "copilot_chat.tool.call.duration", "copilot_chat_tool_call_duration.*_count"),
    ("vscode-copilot-chat", "copilot_chat.agent.turn.count", "copilot_chat_agent_turn_count.*_count"),
    ("vscode-copilot-chat", "copilot_chat.agent.invocation.duration", "copilot_chat_agent_invocation_duration.*_count"),
    ("vscode-copilot-chat", "copilot_chat.session.count", "copilot_chat_session_count.*_total"),
    ("vscode-copilot-chat", "copilot_chat.time_to_first_token", "copilot_chat_time_to_first_token.*_count"),
    ("github-copilot-runtime", "gen_ai.client.token.usage", "gen_ai_client_token_usage_sum"),
    ("github-copilot-runtime", "any gen_ai.* / copilot_chat.* metric", "gen_ai_.*|copilot_chat_.*"),
    ("claude-code", "claude_code.token.usage", "claude_code_token_usage.*_total"),
    ("claude-code", "claude_code.session.count", "claude_code_session_count.*_total"),
    ("claude-code", "claude_code.active_time.total", "claude_code_active_time.*_total"),
    ("codex", "codex.* metrics (any)", "codex_.*"),
]


def prom(expr: str, legend: str = "", ref: str = "A", instant: bool = True, fmt: str | None = None) -> dict:
    t = {"refId": ref, "datasource": PROM, "expr": expr, "legendFormat": legend,
         "instant": instant, "range": not instant, "editorMode": "code"}
    if fmt:
        t["format"] = fmt
    return t


def loki(expr: str, legend: str = "", ref: str = "A", instant: bool = True) -> dict:
    return {"refId": ref, "datasource": LOKI, "expr": expr, "legendFormat": legend,
            "queryType": "instant" if instant else "range", "editorMode": "code"}


def traceql_table(q: str, ref: str = "A", limit: int = 50) -> dict:
    return {"refId": ref, "datasource": TEMPO, "queryType": "traceql", "query": q,
            "tableType": "spans", "limit": limit, "spss": 50}


def traceql_metric(q: str, ref: str = "A") -> dict:
    return {"refId": ref, "datasource": TEMPO, "queryType": "traceql", "query": q,
            "metricsQueryType": "instant", "tableType": "traces", "limit": 20}


class Layout:
    def __init__(self):
        self.y = 0
        self.panels = []

    def row(self, pid: int, title: str):
        self.panels.append({"type": "row", "id": pid, "title": title, "collapsed": False,
                            "gridPos": {"h": 1, "w": 24, "x": 0, "y": self.y}, "panels": []})
        self.y += 1

    def add(self, panels: list, h: int):
        x = 0
        for p, w in panels:
            p["gridPos"] = {"h": h, "w": w, "x": x, "y": self.y}
            x += w
            self.panels.append(p)
        self.y += h


def panel(pid: int, title: str, ptype: str, targets: list, desc: str, unit: str = "short",
          no_value: str = NO_DATA, ds: dict | None = None, options: dict | None = None,
          overrides: list | None = None, mappings: list | None = None, decimals: int | None = None,
          thresholds: list | None = None, transformations: list | None = None, custom: dict | None = None) -> dict:
    defaults = {"unit": unit, "noValue": no_value}
    if decimals is not None:
        defaults["decimals"] = decimals
    if mappings:
        defaults["mappings"] = mappings
    if thresholds:
        defaults["thresholds"] = {"mode": "absolute", "steps": thresholds}
        defaults["color"] = {"mode": "thresholds"}
    if custom:
        defaults["custom"] = custom
    p = {"id": pid, "type": ptype, "title": title, "description": desc,
         "datasource": ds or targets[0]["datasource"], "targets": targets,
         "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
         "options": options or {}}
    if transformations:
        p["transformations"] = transformations
    return p


def stat_opts(text_mode: str = "value_and_name") -> dict:
    return {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "textMode": text_mode, "colorMode": "none", "graphMode": "none", "justifyMode": "auto",
            "orientation": "horizontal"}


def table_opts() -> dict:
    return {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}}


ORGANIZE_HIDE_TIME = {"id": "organize", "options": {"excludeByName": {"Time": True}}}


def coverage_expr() -> str:
    parts = []
    for harness, label, name_re in EXPECTED_METRICS:
        inner = (f'count(last_over_time({{__name__=~"{name_re}",workbench_harness="{harness}",{SEL}}}[$__range])) '
                 "or vector(-1)")
        parts.append(f'label_replace(label_replace(({inner}), "signal", "{label}", "", ""), '
                     f'"harness", "{harness}", "", "")')
    return "\n or ".join(parts)


def build() -> dict:
    L = Layout()

    L.add([(panel(1, "About this dashboard", "text", [{"refId": "A", "datasource": TEXT_DS}],
                  "How to read the workbench dashboard.", ds=TEXT_DS, options={"mode": "markdown", "content": (
        "**Showing `$provenance` data, task `$task`.** `captured` = arrived on the local OTLP port "
        "(not proof of authenticity: any local process can post there). `synthetic` = the repo's "
        "hand-written fixtures, used only by `stack.ps1 selftest`.\n\n"
        "* **Missing is not zero.** Empty panels say *No data in range*. The Coverage table marks expected "
        "signals that never arrived as **not seen**. Nothing here substitutes 0 for missing data.\n"
        "* **Tokens: one canonical source per harness, never summed across signals or harnesses.** "
        "Copilot harnesses: `gen_ai.client.token.usage` (per model call). Claude Code: `claude_code.token.usage`. "
        "Agent-level `invoke_agent` span tokens are roll-ups that repeat their children; they appear only in "
        "the roll-up check.\n"
        "* **Harness labels come from `service.name`.** `copilot-chat` = the VS Code Copilot Chat extension. "
        "`github-copilot` = the GitHub Copilot runtime, which is both the standalone Copilot CLI and VS Code's "
        "embedded agent host; nothing verified tells them apart yet, so it is labelled `github-copilot-runtime`. "
        "If one VS Code session shows up under both labels, they describe the same work: do not add them.\n"
        "* **Budget** is a number you type in the `token_budget` variable. It is not your provider plan or "
        "subscription quota, and no cost is computed anywhere.\n"
        "* Sessions are counted from logs and traces; metrics carry no session IDs by design.")})
              , 24)], h=6)

    # ---------------------------------------------------------------- coverage
    L.row(100, "Coverage and activity")
    L.add([
        (panel(101, "Expected metric signals in range", "table",
               [prom(coverage_expr(), fmt="table")],
               "One row per documented signal and harness. A number is the count of series seen in the time "
               "range; 'not seen' means nothing arrived (the harness may be idle, not configured, or not emit it). "
               "Generated from probe/expectations.json names.",
               no_value="Nothing received in range",
               mappings=[{"type": "value", "options": {"-1": {"text": "not seen", "color": "red", "index": 0}}},
                         {"type": "range", "options": {"from": 0, "to": 1e12, "result": {"color": "green", "index": 1}}}],
               options=table_opts(),
               custom={"cellOptions": {"type": "color-text"}, "align": "auto"},
               transformations=[{"id": "organize", "options": {
                   "excludeByName": {"Time": True},
                   "indexByName": {"harness": 0, "signal": 1, "Value": 2},
                   "renameByName": {"Value": "series in range"}}},
                   {"id": "sortBy", "options": {"sort": [{"field": "harness"}]}}]), 10),
        (panel(102, "Spans in range by harness", "stat",
               [traceql_metric("{" + TSEL + "} | count_over_time() by (resource.workbench.harness)")],
               "Tempo spans per harness. VS Code emits spans; Claude Code only with its beta tracing flag; "
               "Codex is unverified.", no_value="No spans in range", options=stat_opts()), 5),
        (panel(103, "Log events in range by harness", "stat",
               [loki('sum by (workbench_harness) (count_over_time({service_name=~".+"} ' + LSEL + " [$__range]))",
                     legend="{{workbench_harness}}")],
               "Loki log records (events) per harness.", no_value="No log events in range", options=stat_opts()), 5),
        (panel(104, "Sessions started (metric)", "stat",
               [prom('sum by (workbench_harness) (increase({__name__=~"copilot_chat_session_count.*_total|'
                     'claude_code_session_count.*_total",' + SEL + "}[$__range] anchored))",
                     legend="{{workbench_harness}}")],
               "Session-start counters. Counted without session IDs on the metric.",
               no_value="No session counter in range", options=stat_opts()), 4),
    ], h=9)
    L.add([
        (panel(105, "Distinct sessions seen in log events", "stat",
               [loki('count by (workbench_harness) (sum by (workbench_harness, session_id) (count_over_time('
                     '{service_name=~".+"} ' + LSEL + ' | session_id!="" [$__range])))',
                     legend="{{workbench_harness}}")],
               "Distinct session.id values in log events (session IDs live in logs, not metric labels).",
               no_value="No session IDs in log events", options=stat_opts()), 6),
        (panel(106, "Model calls in range", "stat",
               [prom(f"sum by (workbench_harness, gen_ai_request_model) (increase({VS_OP_DUR}_count{{{SEL}}}[$__range] anchored))",
                     legend="{{workbench_harness}} {{gen_ai_request_model}}", ref="A"),
                loki("sum by (model) (count_over_time(" + CLAUDE_EVENTS + ' | event_name="api_request" [$__range]))',
                     legend="claude-code {{model}}", ref="B")],
               "Model round trips per harness and model. Copilot harnesses: gen_ai.client.operation.duration count. "
               "Claude Code: api_request events.", ds=MIXED, options=stat_opts()), 10),
        (panel(107, "Models seen (canonical token series)", "table",
               [prom(f'sum by (workbench_harness, model) (label_replace(increase({VS_TOKENS}{{{SEL}}}[$__range] anchored), '
                     f'"model", "$1", "gen_ai_request_model", "(.*)")) or sum by (workbench_harness, model) '
                     f"(increase({CC_TOKENS}{{{SEL}}}[$__range] anchored))", fmt="table")],
               "Models with token use in range, per harness (all token types). Per-harness rows; no cross-harness total.",
               options=table_opts(), transformations=[{"id": "organize", "options": {
                   "excludeByName": {"Time": True}, "renameByName": {"Value": "tokens (all types)"}}}]), 8),
    ], h=6)

    # ------------------------------------------------------------------ tokens
    L.row(200, "Canonical token usage")
    task_tokens = (
        f'sum by (workbench_task_id, workbench_harness, token_type) (label_replace(increase({VS_TOKENS}{{{SEL}}}[$__range] anchored), '
        f'"token_type", "$1", "gen_ai_token_type", "(.*)")) or sum by (workbench_task_id, workbench_harness, token_type) '
        f'(label_replace(increase({CC_TOKENS}{{{SEL}}}[$__range] anchored), "token_type", "$1", "type", "(.*)"))')
    L.add([
        (panel(201, "Tokens by task, harness and token type", "table", [prom(task_tokens, fmt="table")],
               "Canonical sources only: VS Code gen_ai.client.token.usage, Claude Code claude_code.token.usage. "
               "Exact increase over the range. Token types are not comparable across harnesses (Claude 'input' "
               "excludes cache; Copilot's is unverified), so there is no cross-harness total.",
               options=table_opts(), decimals=0, transformations=[{"id": "organize", "options": {
                   "excludeByName": {"Time": True},
                   "indexByName": {"workbench_task_id": 0, "workbench_harness": 1, "token_type": 2, "Value": 3},
                   "renameByName": {"workbench_task_id": "task", "workbench_harness": "harness",
                                    "token_type": "token type", "Value": "tokens"}}}]), 10),
        (panel(202, "Copilot tokens over time (gen_ai.client.token.usage)", "timeseries",
               [prom(f"sum by (workbench_harness, gen_ai_request_model, gen_ai_token_type) (increase({VS_TOKENS}{{{SEL}}}[$__interval] anchored))",
                     legend="{{workbench_harness}} {{gen_ai_request_model}} {{gen_ai_token_type}}", instant=False)],
               "Tokens per interval from gen_ai.client.token.usage, per harness (VS Code extension, GitHub Copilot "
               "runtime). Series of different harnesses are never stacked into one total.",
               custom={"drawStyle": "bars", "fillOpacity": 60, "stacking": {"mode": "normal"}}), 7),
        (panel(203, "Claude Code tokens over time", "timeseries",
               [prom(f"sum by (model, type, query_source) (increase({CC_TOKENS}{{{SEL}}}[$__interval] anchored))",
                     legend="{{model}} {{type}} ({{query_source}})", instant=False)],
               "Tokens per interval from claude_code.token.usage; query_source separates main thread from subagents.",
               custom={"drawStyle": "bars", "fillOpacity": 60, "stacking": {"mode": "normal"}}), 7),
    ], h=9)

    # ------------------------------------------------------------------- tools
    L.row(300, "Tools, model latency and round trips")
    vs_tools = (f"sum by (workbench_harness, gen_ai_tool_name, success) (increase({VS_TOOL_COUNT}{{{SEL}}}[$__range] anchored))")
    L.add([
        (panel(301, "Copilot tool calls by outcome", "table", [prom(vs_tools, fmt="table")],
               "copilot_chat.tool.call.count by harness, tool and success.", options=table_opts(), decimals=0,
               transformations=[{"id": "organize", "options": {"excludeByName": {"Time": True},
                                 "renameByName": {"workbench_harness": "harness", "gen_ai_tool_name": "tool",
                                                  "Value": "calls"}}}]), 6),
        (panel(302, "Claude Code tool calls by outcome", "table",
               [loki("sum by (tool_name, success) (count_over_time(" + CLAUDE_EVENTS + ' | event_name="tool_result" [$__range]))')],
               "tool_result events by tool and success.", options=table_opts(), decimals=0,
               transformations=[{"id": "organize", "options": {"excludeByName": {"Time": True},
                                 "renameByName": {"tool_name": "tool", "Value #A": "calls", "Value": "calls"}}}]), 6),
        (panel(303, "Tool failure rate", "bargauge",
               [prom(f'(sum by (workbench_harness, gen_ai_tool_name) (increase({VS_TOOL_COUNT}{{{SEL},success="false"}}[$__range] anchored)) '
                     f"or 0 * sum by (workbench_harness, gen_ai_tool_name) (increase({VS_TOOL_COUNT}{{{SEL}}}[$__range] anchored))) "
                     f"/ sum by (workbench_harness, gen_ai_tool_name) (increase({VS_TOOL_COUNT}{{{SEL}}}[$__range] anchored))",
                     legend="{{workbench_harness}} {{gen_ai_tool_name}}", ref="A"),
                loki("(sum by (tool_name) (count_over_time(" + CLAUDE_EVENTS + ' | event_name="tool_result" | success="false" [$__range])) '
                     "or 0 * sum by (tool_name) (count_over_time(" + CLAUDE_EVENTS + ' | event_name="tool_result" [$__range]))) '
                     "/ sum by (tool_name) (count_over_time(" + CLAUDE_EVENTS + ' | event_name="tool_result" [$__range]))',
                     legend="claude-code {{tool_name}}", ref="B")],
               "Failed / all calls per tool. A tool that ran without failing shows 0% (a measured zero: its calls "
               "are in the denominator); a tool that never ran has no bar.",
               unit="percentunit", ds=MIXED, options={"displayMode": "basic", "orientation": "horizontal",
                                                      "reduceOptions": {"calcs": ["lastNotNull"], "values": False}},
               thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 0.2}, {"color": "red", "value": 0.5}]), 6),
        (panel(304, "Tool latency (mean and p95)", "table",
               [prom(f"sum by (workbench_harness, gen_ai_tool_name) (increase({VS_TOOL_DUR}_sum{{{SEL}}}[$__range] anchored)) / "
                     f"sum by (workbench_harness, gen_ai_tool_name) (increase({VS_TOOL_DUR}_count{{{SEL}}}[$__range] anchored))",
                     legend="{{workbench_harness}} mean {{gen_ai_tool_name}}", ref="A"),
                prom(f"histogram_quantile(0.95, sum by (le, workbench_harness, gen_ai_tool_name) (increase({VS_TOOL_DUR}_bucket{{{SEL}}}[$__range] anchored)))",
                     legend="{{workbench_harness}} p95 (bucket estimate) {{gen_ai_tool_name}}", ref="B"),
                loki("avg_over_time(" + CLAUDE_EVENTS + ' | event_name="tool_result" | unwrap duration_ms [$__range]) by (tool_name)',
                     legend="claude-code mean {{tool_name}}", ref="C"),
                loki("quantile_over_time(0.95, " + CLAUDE_EVENTS + ' | event_name="tool_result" | unwrap duration_ms [$__range]) by (tool_name)',
                     legend="claude-code p95 {{tool_name}}", ref="D")],
               "Milliseconds. VS Code p95 is interpolated from histogram buckets; Claude Code values are exact over events.",
               unit="ms", ds=MIXED, options=table_opts(), decimals=0,
               transformations=[{"id": "reduce", "options": {"reducers": ["lastNotNull"], "mode": "seriesToRows"}}]), 6),
    ], h=8)
    L.add([
        (panel(305, "Model call latency (mean and p95)", "table",
               [prom(f"sum by (workbench_harness, gen_ai_request_model) (increase({VS_OP_DUR}_sum{{{SEL}}}[$__range] anchored)) / "
                     f"sum by (workbench_harness, gen_ai_request_model) (increase({VS_OP_DUR}_count{{{SEL}}}[$__range] anchored))",
                     legend="{{workbench_harness}} mean {{gen_ai_request_model}}", ref="A"),
                prom(f"histogram_quantile(0.95, sum by (le, workbench_harness, gen_ai_request_model) (increase({VS_OP_DUR}_bucket{{{SEL}}}[$__range] anchored)))",
                     legend="{{workbench_harness}} p95 (bucket estimate) {{gen_ai_request_model}}", ref="B"),
                loki("avg_over_time(" + CLAUDE_EVENTS + ' | event_name="api_request" | unwrap duration_ms [$__range]) by (model) / 1000',
                     legend="claude-code mean {{model}}", ref="C"),
                loki("quantile_over_time(0.95, " + CLAUDE_EVENTS + ' | event_name="api_request" | unwrap duration_ms [$__range]) by (model) / 1000',
                     legend="claude-code p95 {{model}}", ref="D")],
               "Seconds per model call. VS Code: gen_ai.client.operation.duration. Claude Code: api_request duration_ms.",
               unit="s", ds=MIXED, options=table_opts(), decimals=2,
               transformations=[{"id": "reduce", "options": {"reducers": ["lastNotNull"], "mode": "seriesToRows"}}]), 8),
        (panel(306, "Model round trips per agent turn / prompt", "stat",
               [prom(f"sum by (workbench_harness, gen_ai_agent_name) (increase({VS_TURNS}_sum{{{SEL}}}[$__range] anchored)) / "
                     f"sum by (workbench_harness, gen_ai_agent_name) (increase({VS_TURNS}_count{{{SEL}}}[$__range] anchored))",
                     legend="{{workbench_harness}} {{gen_ai_agent_name}}", ref="A"),
                loki("sum(count_over_time(" + CLAUDE_EVENTS + ' | event_name="api_request" [$__range])) / '
                     "count(sum by (prompt_id) (count_over_time(" + CLAUDE_EVENTS + ' | event_name="api_request" [$__range])))',
                     legend="claude-code per prompt", ref="B")],
               "VS Code: mean copilot_chat.agent.turn.count per agent invocation. Claude Code: api_request events "
               "per distinct prompt.id (includes subagent requests).", ds=MIXED, decimals=2, options=stat_opts()), 8),
        (panel(307, "Model call errors", "stat",
               [prom(f'sum by (workbench_harness, error_type) (increase({VS_OP_DUR}_count{{{SEL},error_type!=""}}[$__range] anchored))',
                     legend="{{workbench_harness}} {{error_type}}", ref="A"),
                loki("sum by (status_code) (count_over_time(" + CLAUDE_EVENTS + ' | event_name="api_error" [$__range]))',
                     legend="claude-code HTTP {{status_code}}", ref="B")],
               "VS Code: operation duration samples carrying error.type. Claude Code: api_error events.",
               no_value="No model call errors recorded in range", ds=MIXED, decimals=0, options=stat_opts()), 8),
    ], h=7)

    # ------------------------------------------------------------------ traces
    L.row(400, "Agent and subagent traces")
    L.add([
        (panel(401, "Agent invocations (spans)", "table",
               [traceql_table('{span.gen_ai.operation.name="invoke_agent" && ' + TSEL + "} | select(span.gen_ai.agent.name, "
                              "span.gen_ai.request.model, span.copilot_chat.turn_count, span.gen_ai.usage.input_tokens, "
                              "span.gen_ai.usage.output_tokens, resource.session.id, resource.workbench.task.id, "
                              "resource.workbench.harness, resource.service.name)")],
               "invoke_agent spans with their session and task. Token columns here are roll-ups (a parent includes "
               "its subagents); use the canonical panels for totals.", no_value="No agent spans in range",
               options=table_opts()), 14),
        (panel(402, "Subagents: invoke_agent under a tool call", "table",
               [traceql_table('{span.gen_ai.operation.name="execute_tool"} > {span.gen_ai.operation.name="invoke_agent" && '
                              + TSEL + "} | select(span.gen_ai.agent.name, span.gen_ai.request.model, "
                              "span.gen_ai.usage.input_tokens, span.gen_ai.usage.output_tokens)")],
               "Child invoke_agent spans whose parent is an execute_tool span (the subagent pattern the VS Code docs "
               "describe). Open a trace ID to see the tree.", no_value="No subagent spans in range",
               options=table_opts()), 10),
    ], h=8)
    L.add([
        (panel(403, "Roll-up check: input tokens on chat spans", "stat",
               [traceql_metric('{span.gen_ai.operation.name="chat" && ' + TSEL + "} | sum_over_time(span.gen_ai.usage.input_tokens) "
                               "by (resource.workbench.harness)")],
               "Sum over chat spans (one per model call). Should match the canonical VS Code input total.",
               no_value="No chat spans in range", decimals=0, options=stat_opts("value")), 6),
        (panel(404, "Roll-up check: input tokens summed over every invoke_agent span", "stat",
               [traceql_metric('{span.gen_ai.operation.name="invoke_agent" && ' + TSEL + "} | sum_over_time(span.gen_ai.usage.input_tokens) "
                               "by (resource.workbench.harness)")],
               "Do not use as a total. Parents include nested subagent usage, so this double-counts whenever a "
               "subagent ran. Shown only to make the difference visible.", no_value="No agent spans in range",
               decimals=0, options=stat_opts("value")), 6),
        (panel(405, "Claude Code subagent vs main-thread tokens", "stat",
               [prom(f"sum by (query_source) (increase({CC_TOKENS}{{{SEL}}}[$__range] anchored))", legend="{{query_source}}")],
               "claude_code.token.usage by query_source: the same canonical series split, not added on top.",
               decimals=0, options=stat_opts()), 6),
        (panel(406, "Claude Code spans (beta tracing only)", "table",
               [traceql_table('{resource.workbench.harness="claude-code" && ' + TSEL + "}", limit=20)],
               "Claude Code emits spans only with CLAUDE_CODE_ENHANCED_TELEMETRY_BETA and a traces exporter.",
               no_value="No Claude Code spans (beta tracing off)", options=table_opts()), 6),
    ], h=6)

    # ------------------------------------------------------------------ budget
    L.row(500, "Local token budget (user-set; not a provider quota)")
    budget_used = (
        f'sum by (workbench_harness) (increase({VS_TOKENS}{{{SEL},gen_ai_token_type=~"$budget_token_types"}}[$budget_window] anchored)) '
        f'or sum by (workbench_harness) (increase({CC_TOKENS}{{{SEL},type=~"$budget_token_types"}}[$budget_window] anchored))')
    L.add([
        (panel(501, "Budget used per harness (local budget, not provider quota)", "gauge",
               [prom(f"({budget_used}) / $token_budget", legend="{{workbench_harness}}")],
               "Tokens of the selected types over $budget_window divided by token_budget. Both are dashboard "
               "variables you set; nothing here knows your plan, subscription quota or cost. Each harness is "
               "compared separately because token types are not comparable across harnesses.",
               unit="percentunit", decimals=1, no_value="No canonical token data in the budget window",
               options={"reduceOptions": {"calcs": ["lastNotNull"], "values": False}, "showThresholdMarkers": True},
               thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 0.8}, {"color": "red", "value": 1}]), 12),
        (panel(502, "Budget tokens used in window", "stat", [prom(budget_used, legend="{{workbench_harness}}")],
               "Absolute tokens counted against the local budget (selected token types, $budget_window).",
               decimals=0, no_value="No canonical token data in the budget window", options=stat_opts()), 6),
        (panel(503, "Budget settings", "stat", [prom("vector($token_budget)", legend="token_budget")],
               "The number typed in the token_budget variable, for the record. Change it in the variable bar.",
               decimals=0, options=stat_opts("value_and_name")), 6),
    ], h=7)

    return {
        "uid": "adw-workbench",
        "title": "Agent workflow overview",
        "description": "agent-dev-workbench: harness/model/session activity, canonical tokens, tools, traces and a "
                       "local token budget. Generated by infra/grafana/build_dashboard.py; edit that file, not this JSON.",
        "tags": ["agent-dev-workbench", "opentelemetry", "genai"],
        "editable": False,
        "schemaVersion": 39,
        "version": 1,
        "timezone": "browser",
        "refresh": "30s",
        "time": {"from": "now-3h", "to": "now"},
        "templating": {"list": [
            {"name": "provenance", "label": "Data provenance", "type": "custom", "query": "captured,synthetic",
             "current": {"text": "captured", "value": "captured"}, "options": [
                 {"text": "captured", "value": "captured", "selected": True},
                 {"text": "synthetic", "value": "synthetic", "selected": False}],
             "description": "captured = arrived on the OTLP port; synthetic = repo fixtures (selftest only)."},
            {"name": "task", "label": "Task", "type": "query", "datasource": PROM,
             "query": {"query": 'label_values({workbench_provenance="$provenance"}, workbench_task_id)', "refId": "task"},
             "definition": 'label_values({workbench_provenance="$provenance"}, workbench_task_id)',
             "refresh": 2, "includeAll": True, "multi": True, "allValue": ".*",
             "current": {"text": "All", "value": "$__all"}, "sort": 1},
            {"name": "token_budget", "label": "Local token budget (tokens)", "type": "textbox", "query": "1000000",
             "current": {"text": "1000000", "value": "1000000"},
             "description": "Your own number. Not a provider quota."},
            {"name": "budget_window", "label": "Budget window", "type": "custom", "query": "1d,7d,30d",
             "current": {"text": "7d", "value": "7d"}, "options": [
                 {"text": "1d", "value": "1d", "selected": False}, {"text": "7d", "value": "7d", "selected": True},
                 {"text": "30d", "value": "30d", "selected": False}]},
            {"name": "budget_token_types", "label": "Budget token types", "type": "custom", "multi": True,
             "query": "input,output,cacheRead,cacheCreation",
             "current": {"text": ["input", "output"], "value": ["input", "output"]}, "options": [
                 {"text": t, "value": t, "selected": t in ("input", "output")}
                 for t in ("input", "output", "cacheRead", "cacheCreation")],
             "description": "VS Code reports input/output; Claude Code also cacheRead/cacheCreation."},
        ]},
        "panels": L.panels,
    }


def main(argv: list) -> int:
    text = json.dumps(build(), indent=2) + "\n"
    if "--check" in argv:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            print(f"{OUT} is stale: run python -I infra/grafana/build_dashboard.py")
            return 1
        print(f"{OUT} is up to date")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {OUT} ({len(build()['panels'])} panels)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
