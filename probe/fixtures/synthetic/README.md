# Synthetic fixtures: not real telemetry

Every file in this directory was **written by hand** on 2026-11-03 from the
documented schemas. None of it was captured from a real agent session.

| File | Modeled on | Signal |
|---|---|---|
| `vscode-copilot.traces.json` | [VS Code: Monitor agent usage with OpenTelemetry](https://code.visualstudio.com/docs/agents/guides/monitoring-agents) span table | OTLP/JSON traces |
| `vscode-copilot.metrics.json` | the same page, metrics table | OTLP/JSON metrics |
| `claude-code.logs.json` | [Claude Code: Monitoring](https://code.claude.com/docs/en/monitoring-usage) events | OTLP/JSON logs |
| `claude-code.metrics.json` | the same page, metrics table | OTLP/JSON metrics |

How they are labeled:

- Every resource carries `workbench.fixture = "synthetic"`. The receiver and the
  schema tool label any payload with that marker `synthetic-fixture`, and a
  report built only from fixtures has a `PROVENANCE: SYNTHETIC FIXTURE` banner.
- Model names are placeholders (`synthetic-model-a`, `synthetic-claude-model`).
  Token counts and durations are invented, and they are internally consistent so
  the roll-up checks have a known answer.
- Values beginning with `CANARY-` or equal to `canary@example.invalid` stand in
  for prompt text, identity, paths and commands. `selftest` fails if any of them
  survive into a capture file or a report.

Attribute names follow the docs as read on 2026-11-03. Real builds may differ.
Use `probe.ps1 schema` on a real capture to find out.
