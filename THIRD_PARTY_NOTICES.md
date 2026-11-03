# Third-party notices

## microsoft/hve-core (MIT)

Source: <https://github.com/microsoft/hve-core/tree/main/.github/skills/experimental/copilot-otel-metrics>,
pinned at commit `b3541b6d18fef7418d9b1f16e51d0dfe5cd07009` (2026-09-18).

### Adapted files (milestone 2)

These files are modified copies. Each one carries a header naming the source and
this notice.

**`infra/compose.yaml`**, from `examples/compose.yaml`.

Kept:

- a collector as the only OTLP ingress, in front of one `grafana/otel-lgtm` container;
- the same image digests (collector-contrib 0.158.0, otel-lgtm 0.29.2);
- loopback-only ports;
- anonymous Grafana access off, with the login form and basic auth restored;
- required admin credentials, and a separate Grafana database volume;
- the collector hardening (`read_only`, `cap_drop: ALL`, `no-new-privileges`, `mem_limit`);
- `otlp-deltatocumulative`;
- 120-day retention;
- the health-checked startup order.

Changed:

1. Prometheus `9090` and Tempo `3200` are not published. otel-lgtm enables
   Prometheus' OTLP and remote-write receivers, so a published `9090` is an
   unfiltered ingest path. Reads go through Grafana.
2. Volumes are project-managed under a fixed project name, not `external`. The
   self-test uses its own project and volumes.
3. Credentials are `ADW_GRAFANA_USER` / `ADW_GRAFANA_PASSWORD` from a gitignored
   `infra/.env` written by `stack.ps1 init`.
4. Host ports can be overridden (`ADW_*_PORT`), and fixed container names are removed
   so a second stack can run next to the first.
5. The workbench dashboard is provisioned from files.
6. Grafana usage reporting, update checks, the news feed and plugin preinstall are off.
7. Two Prometheus feature flags are added: `created-timestamp-zero-ingestion` and
   `promql-extended-range-selectors`.
8. The health check also waits for Loki and Tempo.

**`infra/collector/otel-collector.yaml`**, from `examples/otel-collector-local.yaml`.

Kept:

- the receivers;
- `memory_limiter`;
- the fail-closed `redaction` design with hve-core's allowed keys and `blocked_values` list;
- `summary: silent`;
- the `transform/scrub` targets;
- the internal `otlp_grpc` exporter;
- `health_check`.

Changed:

1. Added `transform/workbench`, which derives `workbench.harness` from `service.name`,
   sets `workbench.provenance` and validates `workbench.task.id`, and copies these
   three keys onto metric data points.
2. Added `transform/resource`: a `keep_keys` resource allow-list.
3. Split `redaction` into one processor per signal:
   - `session.id` and other join IDs are allowed on spans and logs only;
   - Claude Code event and metric keys are added;
   - `copilot_chat.copilot_usage_nano_aiu`, the edit-source keys and
     `gen_ai.usage.*` on metrics are not listed.
4. `transform/scrub` uses explicit `span` and `spanevent` context groups. As a flat
   list, the pinned collector skipped the span statements on spans without events.
5. Span names of the GenAI operation families and event-name-shaped log bodies are
   kept. A map-valued log body is replaced, not skipped.
6. Metric units are kept (hve-core overwrote them).
7. Added `filter/no_cost`, which drops `claude_code.cost.usage`.

**Code not copied.** `infra/workbench.py` and `infra/grafana/build_dashboard.py` are
new code. They follow hve-core's approach from `verify.py`, `inspect_metrics.py`,
`validate_dashboard.py` and `references/verification.md`:

- prove data from the store, not from an HTTP 200;
- enumerate names before trusting them;
- run each panel's query;
- one TraceQL metrics query per panel;
- `tableType: spans` for span tables.

No hve-core dashboard JSON or helper code was copied. The finding that
`captureContent=false` does not keep prompt text off spans is cited from hve-core.

```
MIT License

Copyright (c) Microsoft Corporation. All rights reserved.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Referenced, not copied

Attribute, metric and event *names* in `probe/expectations.json`, the synthetic
fixtures, the collector allow-lists and the dashboard queries come from these
documents. No code or prose was copied.

- VS Code docs, [Monitor agent usage with OpenTelemetry](https://code.visualstudio.com/docs/agents/guides/monitoring-agents)
  and [Debug chat interactions](https://code.visualstudio.com/docs/agents/agent-troubleshooting/chat-debug-view)
  (both last updated 2026-10-28).
- [OpenTelemetry GenAI semantic conventions](https://github.com/open-telemetry/semantic-conventions/blob/main/docs/gen-ai/) (Apache-2.0).
- [Claude Code monitoring docs](https://code.claude.com/docs/en/monitoring-usage).
- [Codex advanced configuration docs](https://developers.openai.com/codex/config-advanced).

## Container images (pulled at run time, not redistributed)

- `otel/opentelemetry-collector-contrib` 0.158.0, pinned by digest (Apache-2.0).
- `grafana/otel-lgtm` 0.29.2, pinned by digest (Apache-2.0). It bundles Grafana
  13.1.0, Tempo 3.0.2 and Loki 3.7.3 (AGPL-3.0), Prometheus 3.13.1 (Apache-2.0),
  Pyroscope and an OpenTelemetry Collector, each under its own license.
