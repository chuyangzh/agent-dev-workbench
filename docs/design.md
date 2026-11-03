# Telemetry design: milestone 1 (design and probe)

Status, 2026-11-03: design plus a working probe, tested on **synthetic fixtures
only**. No real session has been captured yet. "Verified" below means checked on
this machine. Everything else is labeled as documented or assumed.

## Goal

Give a local, Windows-friendly **task-level view** across agent sessions:

- which sessions and agents worked on a task;
- how many model round trips and tool calls they made;
- where tools failed or were slow;
- how token use compares with a budget the user sets.

Do this by configuring the harnesses' own OpenTelemetry export and reusing
Microsoft's hve-core local stack, not by building a new pipeline. A *task* is a
unit of work that can span sessions and harnesses, for example Claude Code and
then Codex after an Orca handoff.

## Environment (read-only checks, 2026-11-03; Codex and Docker rechecked the same day)

| Item | Observed |
|---|---|
| VS Code | 1.144.0 running (build `2c33b50b16`); 1.144.1 staged |
| Copilot Chat | **Built in**, `GitHub.copilot-chat` 0.72.0 under `resources/app/extensions/copilot`. `code --list-extensions` doesn't show it because it lists user-installed extensions only. Other extensions: Python, Jupyter, Docker/Containers, Remote, SQLTools, Rainbow CSV. |
| Copilot OTel settings | All 12 `github.copilot.chat.otel.*` keys declared in the manifest, all `scope: application`. None set in user `settings.json`. No VS Code policy key, no Copilot `managed-settings.json`. |
| Claude Code | 2.1.317 on PATH |
| Codex | `codex-cli` 0.166.0 at `C:\Users\zcy16\AppData\Local\Programs\OpenAI\Codex\bin\codex.exe`. Signed in per the user; not checked here. The agent shell can't resolve `codex` through PATH, so it was verified by full path, which is why the first check missed it. `--help` lists `-c, --config <key=value>` overrides. The binary contains the documented `[otel]` keys (`otlp-http`, `log_user_prompt`, `trace_exporter`, `metrics_exporter`) and event names (`codex.api_request`, `codex.tool_result`, `codex.conversation_starts`). |
| Copilot CLI | Not found on the agent shell's PATH. No full path was checked, so it may still be installed. |
| Orca | 1.4.245; its terminals export `ORCA_WORKTREE_ID` and `ORCA_WORKSPACE_ID` (names seen, values not read) |
| Docker | CLI 27.3.1, Compose 2.29.7. Engine reachable after Docker Desktop was started: context `desktop-linux`, server 27.3.1, linux/amd64. No running container publishes 3000, 3200, 4317, 4318 or 9090. |
| Shell / runtime | Windows PowerShell 5.1 (no `pwsh`), Python 3.11.3 |

Not inspected: authentication or credential files, Copilot and Codex sign-in state,
`chatLanguageModels.json`, Claude Code or Codex configuration files.

## Which harness can emit what

| Harness | Telemetry it emits | Verified here | Documented or assumed, not verified |
|---|---|---|---|
| **VS Code Copilot Chat** (agent mode) | The schema the VS Code page documents: `invoke_agent` → `chat` / `execute_tool` / `execute_hook` spans; `gen_ai.*` and `copilot_chat.*` metrics and events; service `copilot-chat` | Extension present. Every documented setting is in the installed manifest, plus `protocol` (`http/json` allowed), `resourceAttributes`, `serviceName`, `headers`. `exporterType: file` exists. | That it emits once enabled (needs a signed-in Copilot account). Whether `protocol: http/json` is honored. The real attribute set. |
| **Claude agent inside Copilot Chat** | Same schema: `invoke_agent claude` spans | — | Availability for this account. The page says managed settings disable it, and none are present. |
| **Claude Code CLI** 2.1.317 | **Its own schema**: `claude_code.*` counters, `claude_code.*` events as OTLP logs, beta `claude_code.interaction` / `llm_request` / `tool` spans; `http/json` supported | Installed | Emission and exact attribute lists. Several came from a summarized read of a long docs page. |
| **Codex CLI** 0.166.0 | Its own schema: `codex.*` log events, optional traces and metrics, from user-level `~/.codex/config.toml` `[otel]`; off by default | Installed (full path). The binary contains the documented `[otel]` keys and event names. `-c` overrides exist. | Emission, attribute names, `service.name`, and the exact `[otel]` TOML shape. The docs read was summarized, and string presence shows the code ships, not that a session emits it. |
| Copilot CLI | Service `github-copilot`, `otlp-http` only | — | Presence unknown (not on the agent shell's PATH) |

So only Copilot Chat in VS Code emits the schema the VS Code docs describe.
Claude Code and Codex, the two harnesses in the Orca handoff and both installed,
each have a different native schema. The dashboard has to
normalize several schemas, and the probe exists to pin each one down from a real
capture before any panel depends on a name.

The **Chat Debug view** and **Agent Debug Logs** panel are in-editor tools. The
panel can export a session "to an OpenTelemetry JSON file in OTLP format". That
export is a possible offline input to `probe schema`, but its shape is unverified.
Neither tool is a backend.

## What we reuse

| From | What | How |
|---|---|---|
| VS Code Copilot Chat | Native OTel export, file exporter, GenAI semantic conventions | Configure only |
| Claude Code, Codex | Native OTel exporters | Configure only (env vars, `config.toml`) |
| [microsoft/hve-core `copilot-otel-metrics`](https://github.com/microsoft/hve-core/tree/main/.github/skills/experimental/copilot-otel-metrics/examples), MIT, commit `b3541b6` | `compose.yaml`: digest-pinned `otel/opentelemetry-collector-contrib` 0.158.0 in front of `grafana/otel-lgtm` 0.29.2, loopback-only ports, required Grafana credentials, `otlp-deltatocumulative`, 120-day retention. `otel-collector-local.yaml`: fail-closed `redaction` allow-list plus `transform/scrub`. `dashboards/copilot-otel.json`. Helpers `verify.py`, `inspect_metrics.py`, `baseline.py`, `validate_dashboard.py`. | Vendor in milestone 2 with the notice in `THIRD_PARTY_NOTICES.md` and list every change |
| hve-core practice | Prove data from the store, not from an HTTP 200. Enumerate names before trusting them. Baseline to separate real data from residue. Treat any store as holding prompt text. | Adopt |

## What we add

1. **Probe** (`probe/`, this milestone). A stdlib OTLP/HTTP-JSON receiver and schema
   reporter with a PowerShell wrapper. It redacts at ingest and reports names, keys,
   types, hierarchy, token fields, parent/child roll-up, privacy flags, and
   documented-vs-observed coverage. See [probe/README.md](../probe/README.md).
2. **Task attribution.** Resource attributes `workbench.task.id` and `workbench.harness`:
   - Claude Code: set through `OTEL_RESOURCE_ATTRIBUTES` (documented).
   - VS Code: set through `github.copilot.chat.otel.resourceAttributes` (declared in the manifest; behavior unverified).
   - Codex: unknown.

   The collector copies them onto metric data points so Prometheus can group by
   them. Sessions that arrive without a task id go to an explicit `unattributed` bucket.
3. **Normalization in the collector.** Extend the allow-list with the Claude Code
   and Codex keys the panels read, and add a `workbench.harness` label.
4. **A workbench dashboard** next to hve-core's seed, specified below.

## Data rules

- **Content capture off by default:**
  - VS Code: `captureContent=false` and `captureIdentity=false`.
  - Claude Code: `OTEL_LOG_USER_PROMPTS`, `OTEL_LOG_TOOL_DETAILS`, `OTEL_LOG_TOOL_CONTENT` and `OTEL_LOG_RAW_API_BODIES` unset.
  - Codex: `log_user_prompt=false`.

  hve-core observed prompt text on Copilot spans *with* `captureContent=false`, so
  those settings are not the control. The controls are redaction at the probe, the
  collector's fail-closed allow-list, and gitignored captures.
- **Identity.** Claude Code attaches `user.email`, the account UUID and
  `organization.id` when signed in. The probe redacts them, the allow-list drops
  them, and the probe launcher sets `OTEL_METRICS_INCLUDE_ACCOUNT_UUID=false`.
- **One canonical token source per harness. Never sum across signals.**

  | Harness | Count tokens from | Never add |
  |---|---|---|
  | VS Code Copilot | `gen_ai.client.token.usage`, which is per model call | `invoke_agent` span usage. It is a roll-up, and a parent includes its nested subagent calls, so summing every `invoke_agent` span double-counts (the synthetic fixture shows 2,700 vs. 2,100). Also `copilot_chat.agent.turn` events. |
  | Claude Code | `claude_code.token.usage`, by `type` | `api_request` event token attributes (the same requests on another signal). `subagent_completed.total_tokens` (final request only, per the docs). `cost.usage`. |
  | Claude agent inside VS Code | `copilot-chat` `chat` spans | `claude-code` service telemetry from the same session. Don't forward `CLAUDE_CODE_ENABLE_TELEMETRY` there. |
  | Codex | Decide from a capture | — |

- **Token types aren't comparable across harnesses yet.** Claude Code's `input`
  excludes cache reads and writes (documented). Whether Copilot's
  `gen_ai.usage.input_tokens` includes cached tokens is unknown. Until a capture
  settles it, chart series per harness, show cache tokens separately, and don't
  show a cross-harness "total".
- **No quota, no cost.** The budget is a number the user types. Nothing infers a
  plan or subscription quota. `claude_code.cost.usage`, `cost_usd` and
  `copilot_chat.copilot_usage_nano_aiu` are not charted: Claude's cost figures are
  documented as approximations, and AIU semantics are not documented on the pages
  read. There is no "savings" panel.
- **Provenance.** Synthetic fixtures carry `workbench.fixture=synthetic`, live in
  `probe/fixtures/synthetic/`, and produce reports with a SYNTHETIC banner. Captures
  go to gitignored `probe/captures/`. A reviewed real schema snapshot may be
  committed to `docs/schemas/captured/` with harness, version and date.

## Proposal: smallest local Grafana implementation (milestone 2)

**Stack.** hve-core's two containers, unchanged in shape. The collector is the only
OTLP ingress (`127.0.0.1:4317/4318`) and feeds `otel-lgtm` (Grafana `:3000`,
Prometheus `:9090`, Tempo `:3200`, Loki internal). Prerequisites: Docker Desktop
running, and Grafana credentials the user chooses, entered with hve-core's
non-echo PowerShell snippet. No Azure and no custom service.

**Collector changes.** Small and reviewable on the vendored file:

1. Allow-list additions:
   - `workbench.task.id`, `workbench.harness`;
   - Claude Code keys the panels read: `type`, `model`, `tool_name`, `success`, `duration_ms`, `query_source`, `event.name`, `input_tokens`, `output_tokens`;
   - Codex keys after its capture.
2. A `transform` statement copying `workbench.task.id`, `workbench.harness` and
   `session.id` from resource to metric data points, defaulting the task to
   `unattributed`.
3. Check the scrub rules against the capture. hve-core rewrites `log.body` and
   `log.event_name` to `[redacted]`, which erases a Claude Code event name if it
   travels there rather than in an attribute. The probe reports which carrier holds it.

**Dashboard `workbench.json`.**

| Row | Panels | Source (names to confirm with `inspect_metrics.py`) |
|---|---|---|
| Coverage (top) | One status per harness × signal: *receiving*, *no data in range*, *never seen*. Count of `unattributed` sessions. A note on what each harness cannot report. | Presence queries per expected series |
| Task / session / agent | Table per `task × harness`: sessions (distinct session id), agent invocations, model calls, tool calls, tokens (canonical source only) | Prometheus; Loki for Claude Code events |
| Tools | Failure rate by tool; p50 and p95 latency by tool | Copilot `copilot_chat.tool.call.count` / `.duration`; Claude `tool_result` (`success`, `duration_ms`) |
| Model round trips | Round trips per agent invocation or prompt; call latency p50/p95; API errors | Copilot `copilot_chat.agent.turn.count`, `gen_ai.client.operation.duration`; Claude `api_request` per `prompt.id`, `api_error` |
| Local budget | Gauge: used ÷ `$token_budget` over `$budget_window` for the selected `$budget_token_types` (default `input`, `output`), per harness. Titled *"Local token budget (user-set; not a provider quota)"*. | Canonical token series |

The budget is configurable through dashboard variables: `token_budget` (textbox),
`budget_window` (`1d`, `7d`, `30d`) and `budget_token_types` (multi-select). Their
defaults live in the dashboard JSON.

**Missing data stays visible.**

- Every panel sets `noValue` to *"No data — see Coverage"*.
- Usage panels never use `or vector(0)`, so a zero means zero, not missing.
- A panel whose metric name was not seen in a capture is left out, not shipped
  empty. hve-core's `validate_dashboard.py` checks that each panel resolves.

Name translation (OTel → Prometheus) is confirmed with `inspect_metrics.py`, not
assumed. hve-core found that `gen_ai.client.operation.duration` has no unit suffix.

**Why not the alternatives.** Aspire and Jaeger, the VS Code page's quickstarts,
show traces but have no metric queries, budget or coverage panels. The Azure path
costs money and needs a cloud identity. A custom app would duplicate what LGTM
already provides in one container.

## Open questions the first real captures must answer

1. VS Code:
   - Is `protocol: http/json` honored, or is the file exporter needed?
   - Do `invoke_agent` tokens equal the sum of descendant `chat` spans, including subagents?
   - Does `input_tokens` include cache reads?
   - Is `resourceAttributes` applied?
   - Which content keys appear with `captureContent=false` on 0.72.0?
2. Claude Code:
   - Where does the event name travel: body, `event.name` or `eventName`?
   - What is the main-thread `query_source` value? The docs say both `main` and `repl_main_thread`.
   - Do `workbench.*` resource attributes survive?
   - Is metric temporality delta?
3. Codex 0.166.0: `service.name`, event attribute names, the exact `[otel]` TOML shape,
   whether `-c` overrides can enable export for a single run without editing
   `config.toml`, and whether `OTEL_RESOURCE_ATTRIBUTES` applies.
4. Orca: can a task id reach sessions Orca launches, including the native Claude Code
   → Codex handoff? Candidates are its workspace-setup automation or
   `ORCA_WORKTREE_ID`. Telemetry exporter variables are ignored in project-level
   Claude settings, so this has to come from the launching environment.
