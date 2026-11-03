# Third-party notices

## microsoft/hve-core (MIT)

Source: <https://github.com/microsoft/hve-core/tree/main/.github/skills/experimental/copilot-otel-metrics>,
pinned at commit `b3541b6d18fef7418d9b1f16e51d0dfe5cd07009` (2026-09-18).

**Status in this milestone:** referenced, not copied. `docs/design.md` builds on
its local stack design: the digest-pinned Collector + `grafana/otel-lgtm` compose
file, the fail-closed `redaction` allow-list, the PromQL/TraceQL dashboard seed,
and the verify/inspect/baseline helpers. It also builds on its finding that
`captureContent=false` does not keep prompt text off spans. The planned Grafana
milestone vendors `examples/compose.yaml`, `examples/otel-collector-local.yaml`,
`examples/dashboards/copilot-otel.json` and the helper scripts. Those copies must
keep this notice, and our changes to them must be listed here.

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

Attribute, metric and event *names* in `probe/expectations.json` and the synthetic
fixtures come from these documents. No code or prose was copied.

- VS Code docs, [Monitor agent usage with OpenTelemetry](https://code.visualstudio.com/docs/agents/guides/monitoring-agents)
  and [Debug chat interactions](https://code.visualstudio.com/docs/agents/agent-troubleshooting/chat-debug-view)
  (both last updated 2026-10-28).
- [OpenTelemetry GenAI semantic conventions](https://github.com/open-telemetry/semantic-conventions/blob/main/docs/gen-ai/) (Apache-2.0).
- [Claude Code monitoring docs](https://code.claude.com/docs/en/monitoring-usage).
- [Codex advanced configuration docs](https://developers.openai.com/codex/config-advanced).

Container images the planned stack would pull, but this repository does not
redistribute: `otel/opentelemetry-collector-contrib` (Apache-2.0) and
`grafana/otel-lgtm` (Apache-2.0, bundling Grafana AGPL-3.0, Prometheus, Tempo
and Loki under their own licenses).
