# Local agent monitoring

A local, Windows-friendly Grafana stack that shows a **task-level view across agent
sessions**: which harnesses, models and sessions worked on a task, canonical token
use, tool failures and latency, model round trips, parent/subagent traces, and a
token budget you set yourself.

It reuses the harnesses' native OpenTelemetry export and Microsoft's
[hve-core](https://github.com/microsoft/hve-core/tree/main/.github/skills/experimental/copilot-otel-metrics/examples)
reference stack, which is a filtering OpenTelemetry Collector in front of `grafana/otel-lgtm`.
It adds no telemetry service of its own. The design and its reasons are in
[design.md](design.md). Adapted files and their changes are listed in
[THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).

```
VS Code Copilot Chat ─┐   OTLP     ┌────────────────────────────┐        ┌─ grafana/otel-lgtm ───────────┐
Claude Code (option) ─┼──────────► │ Collector 127.0.0.1:4318   │ ─────► │ Prometheus · Tempo · Loki      │
                      │ (loopback) │ allow-list, scrub, labels  │ intern │ Grafana 127.0.0.1:3000 (login) │
                      │            └──────────┬─────────────────┘        └────────────────────────────────┘
                      │                       │ optional schema tap (-SchemaTap)
                      │                       └──► probe receiver 127.0.0.1:4319 (redacts at ingest)
```

## Status (2026-11-03)

| What | State |
|---|---|
| Stack starts on Windows 11 / Docker Desktop (compose, digest-pinned images, loopback ports, required login) | **Verified** |
| Collector filtering before storage: no session, user or content labels on metrics; no canary strings in Prometheus, Loki or Tempo | **Verified with synthetic fixtures** (`stack.ps1 selftest`) |
| Every dashboard query runs, returns the hand-computed answers on synthetic data, and returns nothing for `captured` when only synthetic data exists | **Verified with synthetic fixtures** (`stack.ps1 selftest`) |
| VS Code Copilot Chat → collector → store → dashboard with real telemetry | **Not yet received.** In the first attempt the extension host logged its OTLP exporter as enabled for `127.0.0.1:4318`, but the collector received nothing: the receive counters are empty, the store is empty and the schema tap is empty. The workload ran in VS Code's agent host, a shared process started before the settings existed, which Reload Window does not restart. Retry after a full restart; see [Connect VS Code Copilot Chat](#connect-vs-code-copilot-chat). The real schema has not been captured yet. |
| `github-copilot` service mapped neutrally | **Verified with synthetic fixtures.** It becomes `github-copilot-runtime`, never mixed into the VS Code extension's rows. |
| Claude Code → stack | Wired (`stack.ps1 claude`). Not run with real telemetry. |
| Codex | Not wired. Its `[otel]` config shape is unverified ([design.md](design.md)). |

Metric names in the dashboard come from the store, not from the docs. They were
confirmed by sending the synthetic fixtures through this collector into Prometheus.
A real capture can still differ in attributes or units. If it does, the coverage
table shows **not seen** and `stack.ps1 verify` reports the panel as empty.

## Prerequisites

- Windows PowerShell 5.1 or later, and Python 3.9 or later on PATH (`python` or `py`). Standard library only.
- Docker Desktop with the engine running. The images are about 1.5 GB on first pull.
- Free host ports `3000`, `4317` and `4318`. The self-test also uses `13000`, `14317` and `14318`.

## Quick start (PowerShell, from the repository root)

```powershell
function stack { powershell -NoProfile -ExecutionPolicy Bypass -File .\infra\stack.ps1 @args }

stack init       # writes infra\.env (gitignored): user admin + a random Grafana password
stack up         # starts the collector and otel-lgtm; waits until both are healthy
stack selftest   # optional: throwaway stack + SYNTHETIC fixtures; expect "62/62 checks passed"
```

The function bypasses execution policy for that one script process only.

Open <http://127.0.0.1:3000> and sign in with the user and password in
`infra\.env`. The dashboard is **Dashboards › Agent Dev Workbench › Agent workflow
overview**. Use `stack init -Prompt` before the first `up` if you want to choose the
password yourself.

> Grafana adopts the login only when it creates its database: on the first `up`, or
> after `stack down -DeleteData`. Editing `infra\.env` later does not change the live
> login.

## Connect VS Code Copilot Chat

This step needs you. Nothing in this repository edits your VS Code settings.

1. In VS Code, open the Command Palette (Ctrl+Shift+P) and run **Preferences: Open User
   Settings (JSON)**. If you use a non-default profile, run **Preferences: Open
   Application Settings (JSON)** instead. These keys are application-scoped in
   copilot-chat 0.72.0, so they are read from the default profile only.
2. Add these keys inside the top-level object, keeping everything else:

   ```json
   "github.copilot.chat.otel.enabled": true,
   "github.copilot.chat.otel.exporterType": "otlp-http",
   "github.copilot.chat.otel.otlpEndpoint": "http://127.0.0.1:4318",
   "github.copilot.chat.otel.captureContent": false,
   "github.copilot.chat.otel.captureIdentity": false,
   "github.copilot.chat.otel.resourceAttributes": { "workbench.task.id": "demo-vscode-1" },
   ```

   `stack vscode-settings -TaskId <id>` prints the same block.
3. Save, then **quit VS Code completely** (**File › Exit** in every window) and start it
   again. In PowerShell, `Get-Process Code -ErrorAction SilentlyContinue` should return
   nothing in between. **Developer: Reload Window** is not enough: it restarts the
   extension host, but VS Code's agent host, the shared process that runs Copilot CLI
   sessions, keeps the configuration it started with. Copilot Chat must be signed in.
4. In the Chat view choose **Agent** mode and send a small task that uses a tool, for
   example: *"Without editing any files: use a subagent to list the files in this
   folder, then read README.md yourself and summarize it in one line."*
5. After about 60 seconds run `stack verify`, or open the dashboard.

Which label the data gets depends on where the request ran:

| Chat surface | `service.name` | Dashboard harness |
|---|---|---|
| Copilot Chat extension's own agent (the path the VS Code docs describe) | `copilot-chat` (the extension's default, seen in the installed 0.72.0 bundle) | `vscode-copilot-chat` |
| Copilot CLI session, either inside VS Code (agent host) or standalone | `github-copilot` | `github-copilot-runtime` |

`service.name` alone can't tell the embedded runtime from a standalone Copilot CLI,
so the label names the runtime, not the app. The raw `service.name` stays on every
series (`service_name`), and on spans for the tap and probe to inspect. A finer
split needs a distinguishing resource attribute seen in a real capture.

To switch tasks, change `workbench.task.id` and reload. To stop exporting, set
`"github.copilot.chat.otel.enabled": false` (or delete the keys) and reload. These
settings apply to every VS Code window.

## Connect Claude Code (optional, separate source)

Claude Code has its own schema (`claude_code.*`). The dashboard shows it as harness
`claude-code` and never adds it to VS Code numbers. Run this from a **fresh** terminal,
not from inside a Claude Code session:

```powershell
stack claude -TaskId demo-claude-1
```

This uses the probe's launcher (`probe\probe.ps1 claude`) pointed at the stack. It sets
the telemetry variables for that one process: metrics and logs over OTLP `http/json`,
the account UUID off, the task id as a resource attribute. It clears the
prompt and tool-content logging variables, and restores the previous values when the
session ends. Telemetry variables in your user-level Claude settings still apply.

## Verify

```powershell
stack verify     # read-only; queries the store through Grafana's authenticated API
stack status     # containers + a short version of the same checks
```

`verify` prints:

- what the store holds per harness and signal;
- every metric label in use;
- whether a forbidden label (session, user, host or content keys) or a canary string is stored;
- the result of every dashboard query: `DATA`, `EMPTY` (no data in range) or `ERROR`.

A JSON copy goes to `infra\out\` (gitignored). A 200 from the OTLP endpoint proves
nothing: hve-core found that dropped payloads also return 200. Only the store counts.

### Confirm a harness's real schema (schema tap)

```powershell
function probe { powershell -NoProfile -ExecutionPolicy Bypass -File .\probe\probe.ps1 @args }
probe receive -Port 4319 -Label vscode   # terminal A: the probe receiver
stack up -SchemaTap                      # terminal B: the collector also copies what it receives to the probe
# ...run one agent task, wait ~60 s, Ctrl+C in terminal A...
probe schema                             # attribute keys, types, hierarchy, token roll-up, privacy flags
stack up                                 # turns the tap off again
```

The tap copies telemetry **before** filtering, so the report also lists keys the
allow-list drops. The probe redacts string values at ingest, so their contents are
not written. The collector converts any client encoding (protobuf or JSON) to the
OTLP/JSON the probe accepts. The tap has no retry or queue: if the probe isn't
running, the copy is dropped and storage is unaffected.

## The dashboard

`infra/grafana/dashboards/workbench.json` is provisioned read-only. It is generated
by `infra/grafana/build_dashboard.py`. Edit the generator and rerun it with
`python -I infra\grafana\build_dashboard.py`. Grafana reloads the file within 15 s.

| Row | Panels |
|---|---|
| Coverage and activity | **Expected metric signals in range**: one row per documented harness signal, with its series count or **not seen**. Spans and log events per harness. Sessions started (counter). Distinct sessions in log events. Model calls per model. Models with token use. |
| Canonical token usage | Tokens by task × harness × token type. Copilot tokens over time (per harness) and Claude Code tokens over time. |
| Tools, model latency and round trips | Tool calls by outcome (Copilot harnesses, Claude Code). Failure rate per tool. Tool latency mean and p95. Model call latency mean and p95. Round trips per agent turn or per prompt. Model call errors. |
| Agent and subagent traces | `invoke_agent` spans. Subagents (an `invoke_agent` under an `execute_tool`). Roll-up check: chat-span tokens vs. summed `invoke_agent` tokens. Claude Code main vs. subagent tokens. Claude Code beta spans. |
| Local token budget (user-set; not a provider quota) | Budget used per harness. Tokens counted against the budget. The budget value. |

Variables:

- `provenance` (default `captured`);
- `task` (from `workbench_task_id`);
- `token_budget` (a number you type, default 1,000,000);
- `budget_window` (`1d`, `7d` or `30d`);
- `budget_token_types` (default `input` and `output`).

### Rules the queries follow

- **One canonical token source per harness, never summed across signals or harnesses.**
  - Copilot harnesses (`vscode-copilot-chat`, `github-copilot-runtime`): `gen_ai.client.token.usage` (one observation per model call), always grouped by harness. If one VS Code session appears under both labels, the two rows describe the same work: never add them.
  - Claude Code: `claude_code.token.usage`.
  - `invoke_agent` span tokens are roll-ups that include nested subagents. Claude
    Code `api_request` event tokens repeat the same requests on another signal.
    Both appear only in the roll-up check, which shows 2,700 vs. 2,100 on the fixtures.
  - There is no cross-harness total, because the token types are not known to be
    comparable (Claude Code's `input` excludes cache tokens; Copilot's is unverified).
- **Exact totals.** Counters use `increase(x[range] anchored)`, from Prometheus 3's
  `promql-extended-range-selectors`. Series start with a zero sample at their OTLP
  start time (`created-timestamp-zero-ingestion`). Plain `increase()` extrapolates: on
  the fixtures it reported 2,250 tokens for a counter that recorded 1,500.
- **Missing is not zero.**
  - No query uses `or vector(0)`, and every panel's empty text says *No data in
    range (not zero)*.
  - The coverage table uses `-1`, displayed as **not seen**, for expected signals that
    never arrived.
  - The one deliberate zero is a tool's failure rate when it ran and never failed:
    its calls are in the denominator, so 0% is a measurement.
- **Identifiers.**
  - `workbench_task_id` is the only identifier allowed on metric labels. It is
    chosen by you, one value per piece of work, and limited to `[A-Za-z0-9._-]{1,64}`.
    Anything else becomes `invalid`, and a missing id becomes `unattributed`.
  - Session, conversation, prompt and user IDs stay off metrics. Sessions are counted
    from log events (Loki) and listed from spans (Tempo).
- **Provenance.** Each resource is labeled `captured` (arrived on the port) or
  `synthetic` (the repo's fixtures carry `workbench.fixture=synthetic`). The default
  view excludes synthetic data, and the self-test runs in a separate stack with its
  own volumes, so fixtures never enter your real store. `captured` is not proof of
  authenticity: any local process can post to the loopback port.
- **No quota, no cost.** The budget is your own number. `claude_code.cost.usage` is
  dropped at the collector, and no panel infers a plan, a subscription quota or savings.

## What is stored, and what isn't

Telemetry passes four stages in `infra/collector/otel-collector.yaml` before storage:

1. **Workbench labels.**
   - `workbench.harness` comes from `service.name`: `copilot-chat` → `vscode-copilot-chat`, `github-copilot` → `github-copilot-runtime` (neutral: see above), `claude-code*` → `claude-code`, `codex*` → `codex`, anything else → `other`.
   - `workbench.provenance` and the task default are set here too.
2. **Resource allow-list** (`keep_keys`): only service identity and the workbench keys
   (plus `session.id` on spans and logs). Host, user, process and workspace
   attributes never reach storage.
3. **Attribute allow-list** (hve-core's fail-closed `redaction`, one per signal):
   - an attribute not listed is dropped;
   - prompt text, tool arguments and results, error messages, paths, email and account IDs, and cost are not listed;
   - credential-shaped values in surviving attributes are masked.
4. **Scrub** (hve-core's `transform/scrub`):
   - span status messages, trace state, span event names, log severity text and metric descriptions are overwritten;
   - span names outside the GenAI operation families, and log bodies that aren't an event name, are replaced.

**Content capture is off at the source too** (`captureContent=false`; Claude Code
prompt and tool logging unset). That isn't relied on: hve-core observed prompt text on
Copilot spans even with `captureContent=false`.

The self-test feeds canary strings (stand-ins for prompts, paths, emails and account
IDs) through all three signals. It then searches every Prometheus label value, every
Loki stream and every Tempo trace for them. An earlier version of this config let a
span status-message canary through: hve-core's flat statement list ran in the wrong
OTTL context on collector 0.158.0. The selftest caught it, and the config now uses
explicit context groups.

**Not filtered:** the loopback hop itself. Content that a harness emits crosses
`127.0.0.1:4318` in plaintext before the collector drops it. Anything that reads that
port directly sits outside the filter.

**Kept out of Git** (`.gitignore`):

- `infra/.env` (Grafana login);
- `infra/out/` (verification reports);
- `probe/captures/` (probe captures).

Telemetry itself lives in Docker volumes (`agent-dev-workbench_telemetry` and
`agent-dev-workbench_grafana-db`), not in the repository.

**Exposure:** only Grafana (`127.0.0.1:3000`, login required) and the collector
(`127.0.0.1:4317/4318`) are published. Prometheus, Tempo and Loki are reachable only
on the internal Docker network. otel-lgtm enables Prometheus' own OTLP and remote-write
receivers, so publishing `9090` (as hve-core does) would let any local process write
past the filter.

## Stop, reset, remove

```powershell
stack down              # stop; keep telemetry and the Grafana login
stack down -DeleteData  # stop and delete both volumes (all telemetry, Grafana DB)
```

Then revert the VS Code settings (above). A self-test stack left by `stack selftest
-KeepSelftestStack` is removed with `docker compose -p adw-selftest down -v`.

## Troubleshooting

| Symptom | Check |
|---|---|
| `up` fails with `run .\infra\stack.ps1 init` | `infra\.env` is missing; run `stack init`. |
| `up` fails: port is already allocated | Something else uses 3000/4317/4318 (the probe defaults to 4318: use `-Port 4319` while the stack runs). |
| Grafana rejects the login | The database predates `infra\.env`; `stack down -DeleteData` then `up` (deletes telemetry), or change the password in Grafana. |
| VS Code sends nothing | Keys in the default profile's user settings? Window reloaded? Copilot signed in? An agent request sent since the reload? `stack logs` shows collector errors. |
| Panels say *No data in range* | Widen the time range; check `provenance` and `task`; run `stack verify` to see whether the store has the series at all. |
| Spans missing right after a chat | Tempo makes traces searchable after ~30 s. |

## Files

| Path | Purpose |
|---|---|
| `infra/compose.yaml` | Collector + otel-lgtm (adapted from hve-core) |
| `infra/compose.schema-tap.yaml`, `infra/collector/schema-tap.yaml` | Optional copy of received telemetry to the probe |
| `infra/collector/otel-collector.yaml` | Labels, allow-lists, scrub (adapted from hve-core) |
| `infra/grafana/build_dashboard.py` → `dashboards/workbench.json` | Generated, provisioned dashboard |
| `infra/grafana/provisioning/dashboards/workbench.yaml` | Grafana file provider |
| `infra/stack.ps1` | PowerShell entry point |
| `infra/workbench.py` | `verify` and `selftest` (stdlib) |
| `infra/selftest-expected.json` | Known answers computed from the synthetic fixtures |
