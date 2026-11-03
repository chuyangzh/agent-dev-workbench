# Telemetry probe

Capture one real agent session's OpenTelemetry output on this machine and report
its schema before any dashboard is built on it. The report covers span, metric
and event names, attribute keys and value types, span hierarchy, token fields,
privacy flags, and which documented signals actually showed up.

- Windows PowerShell 5.1+ and Python 3.9+ (standard library only). No Docker.
- The receiver binds `127.0.0.1` only and accepts OTLP/HTTP **JSON**. It rejects
  protobuf with HTTP 415 and does not speak gRPC.
- String values are redacted at ingest. See [What gets stored](#what-gets-stored).

| File | Purpose |
|---|---|
| `probe.ps1` | PowerShell entry point: `selftest`, `receive`, `claude`, `vscode-settings`, `schema` |
| `otel_probe.py` | Receiver, redaction, schema report, self-test |
| `expectations.json` | Documented signals per harness, used for the coverage table |
| `fixtures/synthetic/` | **Synthetic** OTLP payloads written by hand from the docs ([README](fixtures/synthetic/README.md)) |
| `captures/` | Real captures. Gitignored, created on first `receive` |

## Setup

Run everything from the repository root. This defines a shorthand for the session
that bypasses execution policy for the script's own process only:

```powershell
function probe { powershell -NoProfile -ExecutionPolicy Bypass -File .\probe\probe.ps1 @args }
probe selftest     # synthetic fixtures only; expect "17/17 checks passed"
```

## Capture a Claude Code session

Claude Code 2.1.317 is installed here. It emits its own schema (`claude_code.*`),
not the VS Code one.

Use two **fresh** terminals. Do not run this from inside a Claude Code session:
`probe claude` refuses when `CLAUDECODE` is set.

```powershell
# Terminal A: receiver (foreground)
probe receive -Label claude-code

# Terminal B: launches `claude` with telemetry env vars scoped to that process
probe claude -TaskId demo-1            # add -Traces for the beta trace spans
```

In the Claude Code session, give one small task that uses a tool, for example
*"List the files in this folder, then read README.md and summarize it in one line."*
Then type `/exit`. Wait about 10 seconds, press Ctrl+C in terminal A, and run:

```powershell
probe schema       # newest capture; or: probe schema -Path .\probe\captures\<dir>
```

`probe claude` sets `CLAUDE_CODE_ENABLE_TELEMETRY=1`, OTLP exporters for metrics
and logs, `OTEL_EXPORTER_OTLP_PROTOCOL=http/json`, short export intervals,
`OTEL_METRICS_INCLUDE_ACCOUNT_UUID=false`, and
`OTEL_RESOURCE_ATTRIBUTES=workbench.task.id=<id>,workbench.harness=claude-code`.
It removes `OTEL_LOG_USER_PROMPTS`, `OTEL_LOG_TOOL_DETAILS`, `OTEL_LOG_TOOL_CONTENT`,
`OTEL_LOG_RAW_API_BODIES` and related flags. Afterwards it restores the previous
values. Telemetry variables in your *user-level* Claude settings `env` still apply.

## Capture a VS Code Copilot Chat session

VS Code 1.144.0 bundles `GitHub.copilot-chat` 0.72.0, whose manifest declares
every `github.copilot.chat.otel.*` setting. Sign-in state was not checked.

```powershell
probe receive -Label vscode          # terminal A
probe vscode-settings -TaskId demo-1 # terminal B: prints the steps below
```

**User action required.** The probe does not edit `settings.json`:

1. VS Code → **Preferences: Open User Settings (JSON)**. The OTel settings are
   `scope: application` in 0.72.0, so workspace or profile settings files are ignored.
2. Paste the printed keys: `enabled`, `exporterType: otlp-http`, `protocol: http/json`,
   `otlpEndpoint`, `captureContent: false`, `captureIdentity: false`, and `resourceAttributes`.
3. **Developer: Reload Window** and confirm Copilot Chat is signed in.
4. In Chat, choose Agent mode and send one small task that uses a tool.
5. Wait about 30 seconds, press Ctrl+C in terminal A, and run `probe schema`.
6. **Revert:** delete the pasted keys and reload. They affect every VS Code window.

If terminal A prints `REJECTED ... application/x-protobuf`, the `protocol` setting
was not honored. Use `probe vscode-settings -FileExporter`, which points
`exporterType: file` at a new `captures\<timestamp>-vscode-file\` directory. That
file holds **unredacted** values. Run `probe schema -Path <dir>`, then delete it.

## Codex (installed; telemetry unverified)

`codex-cli` 0.166.0 is installed at
`C:\Users\zcy16\AppData\Local\Programs\OpenAI\Codex\bin\codex.exe`. It is signed in
according to the user; this probe doesn't check that. Some shells, including the
agent shell this probe was written in, can't resolve `codex` through PATH, so call
it by full path there. `probe.ps1` has no `codex` subcommand yet.

Checked so far:

- `--version` and `--help`. Help lists `-c, --config <key=value>` overrides.
- The binary contains the documented `[otel]` keys (`otlp-http`, `log_user_prompt`,
  `trace_exporter`, `metrics_exporter`) and event names (`codex.api_request`,
  `codex.tool_result`, `codex.conversation_starts`).

That shows the build ships the code, not that a session emits it.

Per the Codex docs (summarized read):

- Export is configured in the user-level `~/.codex/config.toml` `[otel]` table.
  Project-level `otel` is ignored.
- It is off by default (`exporter = "none"`).
- `log_user_prompt` defaults to `false`.

To capture, point an `otlp-http` exporter with `protocol = "json"` at
`http://127.0.0.1:4318`. Do it in that file, or with one-run `-c` overrides if the
key shape allows. Confirm the exact TOML shape for 0.166.0 before using it. The
probe's coverage table already lists the documented `codex.*` events.

## What gets stored

| Carrier | In `otlp.jsonl` and reports |
|---|---|
| Span, metric and event names; attribute keys; value types; numbers; booleans | kept |
| Enum-like strings for allowlisted keys (`gen_ai.operation.name`, `gen_ai.request.model`, `gen_ai.tool.name`, `type`, `tool_name`, `success`, `service.name` …) | kept |
| IDs (`session.id`, `gen_ai.conversation.id`, `prompt.id`, `tool_use_id` …) | salted hash, joinable within one run only |
| Every other string: prompts, paths, commands, errors, `user.email`, repo URLs | `<probe-redacted string len=N>` |
| Harness redaction tokens such as `<REDACTED>` | kept, so a report can say "harness-redacted" |
| Span status messages / exemplars / trace state | redacted / dropped / cleared |

`-StoreRawValues` on `receive` disables redaction. Don't use it unless you must,
and delete the directory afterwards. Before committing a real schema snapshot,
review `schema.md` and copy it to `docs/schemas/captured/<harness>-<version>-<date>.md`.
Never commit `captures/`.

## Reading a report

- **Provenance banner.** `SYNTHETIC FIXTURE` means fixtures only. `CAPTURED` means
  it came in on the loopback port, which is unauthenticated, so any local process
  could have posted it. `MIXED` means don't use it as a real schema.
- **Coverage.** `not observed in this capture` means absent, not unsupported.
  Idle features emit nothing.
- **Privacy flags.** Content, identity and workspace keys that appeared, and
  whether they carried a value. Values are never shown.
- **Token-bearing fields.** Each row is an alternative view of the same model
  calls. Never add rows. The roll-up table shows whether agent-level spans
  repeat their children's tokens.

## Troubleshooting

- *Port in use:* the receiver binds exclusively and exits with WinError 10048.
  Use `-Port 4319` on both `receive` and `claude`/`vscode-settings`.
- *Nothing from Claude Code:* metrics export every 10 s and logs every 5 s. Set
  `$env:CLAUDE_CODE_OTEL_DIAG_STDERR='1'` before `probe claude` to see exporter
  errors.
- *Nothing from VS Code:* reload after editing settings, check the keys are in
  the default profile's **user** settings, and make sure an agent request ran.
