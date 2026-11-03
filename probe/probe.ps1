<#
.SYNOPSIS
    Telemetry capture/probe workflow for agent-dev-workbench (Windows PowerShell 5.1 or later).

.DESCRIPTION
    selftest         Run the probe against synthetic fixtures. No agent, no network beyond loopback.
    receive          Start the local OTLP/HTTP JSON receiver on 127.0.0.1 (foreground, Ctrl+C stops).
    claude           Launch Claude Code with telemetry aimed at the receiver. Run it in a second terminal.
    vscode-settings  Print the VS Code user settings for a Copilot Chat capture, and how to revert them.
    schema           Summarize a capture directory into schema.json and schema.md.

    Nothing here edits VS Code or Claude Code settings files, starts Docker, or sends data off the machine.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\probe\probe.ps1 selftest
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\probe\probe.ps1 receive -Label claude-code
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\probe\probe.ps1 claude -TaskId demo-1
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0, Mandatory = $true)]
    [ValidateSet('selftest', 'receive', 'claude', 'vscode-settings', 'schema')]
    [string]$Command,

    [ValidateRange(1, 65535)]
    [int]$Port = 4318,

    [ValidatePattern('^[A-Za-z0-9._-]{1,48}$')]
    [string]$Label = 'session',

    [ValidatePattern('^[A-Za-z0-9._-]{1,64}$')]
    [string]$TaskId,

    [string]$Path,

    [switch]$StoreRawValues,
    [switch]$Traces,
    [switch]$FileExporter,

    [string[]]$ClaudeArgs = @()
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$ProbeDir = $PSScriptRoot
$ProbePy = Join-Path $ProbeDir 'otel_probe.py'
$CaptureRoot = Join-Path $ProbeDir 'captures'
$Self = 'powershell -ExecutionPolicy Bypass -File "' + (Join-Path $ProbeDir 'probe.ps1') + '"'

function Invoke-Probe {
    param([string[]]$ProbeArgs)
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        & $python.Source -I $ProbePy @ProbeArgs
        return
    }
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        & $launcher.Source -3 -I $ProbePy @ProbeArgs
        return
    }
    throw 'Python 3.9 or later is required (python or py on PATH).'
}

function Test-Receiver {
    param([int]$TcpPort)
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        return ($client.ConnectAsync('127.0.0.1', $TcpPort).Wait(1000) -and $client.Connected)
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function New-TaskId {
    return 'probe-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
}

$exitCode = 0
switch ($Command) {
    'selftest' {
        Invoke-Probe @('selftest')
        $exitCode = $LASTEXITCODE
    }

    'receive' {
        $probeArgs = @('receive', '--port', "$Port", '--label', $Label)
        if ($StoreRawValues) { $probeArgs += '--store-raw-values' }
        Write-Host "Leave this window open. In a second terminal run one of:"
        Write-Host "  $Self claude -Port $Port"
        Write-Host "  $Self vscode-settings -Port $Port"
        Write-Host ''
        Invoke-Probe $probeArgs
        $exitCode = $LASTEXITCODE
    }

    'claude' {
        if ($env:CLAUDECODE) {
            throw 'This shell was started by a Claude Code session (CLAUDECODE is set). Open a fresh terminal so the probe session is not nested inside another one.'
        }
        $claude = Get-Command claude -ErrorAction SilentlyContinue
        if (-not $claude) { throw 'claude is not on PATH.' }
        if (-not (Test-Receiver $Port)) {
            throw "Nothing is listening on 127.0.0.1:$Port. Start the receiver first, in another terminal: $Self receive -Label claude-code -Port $Port"
        }
        if (-not $TaskId) { $TaskId = New-TaskId }

        # Scoped to this process and restored afterwards. Content flags are removed, not set to 0.
        $set = [ordered]@{
            'CLAUDE_CODE_ENABLE_TELEMETRY'         = '1'
            'OTEL_METRICS_EXPORTER'                = 'otlp'
            'OTEL_LOGS_EXPORTER'                   = 'otlp'
            'OTEL_EXPORTER_OTLP_PROTOCOL'          = 'http/json'
            'OTEL_EXPORTER_OTLP_ENDPOINT'          = "http://127.0.0.1:$Port"
            'OTEL_METRIC_EXPORT_INTERVAL'          = '10000'
            'OTEL_LOGS_EXPORT_INTERVAL'            = '5000'
            'OTEL_METRICS_INCLUDE_ACCOUNT_UUID'    = 'false'
            'CLAUDE_CODE_OTEL_SHUTDOWN_TIMEOUT_MS' = '5000'
            'OTEL_RESOURCE_ATTRIBUTES'             = "workbench.task.id=$TaskId,workbench.harness=claude-code"
        }
        if ($Traces) {
            $set['CLAUDE_CODE_ENHANCED_TELEMETRY_BETA'] = '1'
            $set['OTEL_TRACES_EXPORTER'] = 'otlp'
            $set['OTEL_TRACES_EXPORT_INTERVAL'] = '5000'
        }
        $unset = @('OTEL_LOG_USER_PROMPTS', 'OTEL_LOG_ASSISTANT_RESPONSES', 'OTEL_LOG_TOOL_DETAILS',
                   'OTEL_LOG_TOOL_CONTENT', 'OTEL_LOG_RAW_API_BODIES', 'OTEL_LOG_MANAGED_SETTINGS',
                   'ENABLE_BETA_TRACING_DETAILED', 'BETA_TRACING_ENDPOINT')
        if (-not $Traces) { $unset += @('CLAUDE_CODE_ENHANCED_TELEMETRY_BETA', 'OTEL_TRACES_EXPORTER') }

        $saved = @{}
        foreach ($name in (@($set.Keys) + $unset)) {
            $saved[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
        }
        try {
            foreach ($name in $set.Keys) { [Environment]::SetEnvironmentVariable($name, $set[$name], 'Process') }
            foreach ($name in $unset) { [Environment]::SetEnvironmentVariable($name, $null, 'Process') }
            Write-Host "Launching Claude Code with telemetry -> http://127.0.0.1:$Port (http/json), task id '$TaskId'."
            Write-Host 'Prompt and tool-detail logging are off for this process. Telemetry variables in your'
            Write-Host 'user-level Claude settings still apply; the receiver redacts string values either way.'
            Write-Host 'In the session: give one small task that uses a tool, for example'
            Write-Host '  "List the files in this folder, then read README.md and summarize it in one line."'
            Write-Host 'Then type /exit.'
            Write-Host ''
            & $claude.Source @ClaudeArgs
            $exitCode = $LASTEXITCODE
        } finally {
            foreach ($name in $saved.Keys) { [Environment]::SetEnvironmentVariable($name, $saved[$name], 'Process') }
        }
        Write-Host ''
        Write-Host 'Claude Code exited. Wait about 10 seconds for the final export, stop the receiver with'
        Write-Host "Ctrl+C, then run: $Self schema"
    }

    'vscode-settings' {
        if (-not $TaskId) { $TaskId = New-TaskId }
        if ($FileExporter) {
            $dir = Join-Path $CaptureRoot ((Get-Date -Format 'yyyyMMdd-HHmmss') + '-vscode-file')
            New-Item -ItemType Directory -Force -Path $dir | Out-Null
            $outfile = (Join-Path $dir 'copilot-otel.jsonl').Replace('\', '\\')
            $exporter = @"
  "github.copilot.chat.otel.exporterType": "file",
  "github.copilot.chat.otel.outfile": "$outfile",
"@
            $note = "The file exporter writes unredacted values to $dir. Run schema on it, then delete the raw file."
        } else {
            if (-not (Test-Receiver $Port)) {
                Write-Warning "Nothing is listening on 127.0.0.1:$Port yet. Start: $Self receive -Label vscode -Port $Port"
            }
            $exporter = @"
  "github.copilot.chat.otel.exporterType": "otlp-http",
  "github.copilot.chat.otel.protocol": "http/json",
  "github.copilot.chat.otel.otlpEndpoint": "http://127.0.0.1:$Port",
"@
            $note = 'If the receiver logs REJECTED ... application/x-protobuf, the protocol setting was not honored; rerun with -FileExporter.'
        }
        Write-Host @"
1. In VS Code run "Preferences: Open User Settings (JSON)". These keys are application-scoped in
   copilot-chat 0.72.0, so they are ignored in workspace or profile settings files.
2. Add these keys inside the top-level object (keep everything else as is):

  "github.copilot.chat.otel.enabled": true,
$exporter
  "github.copilot.chat.otel.captureContent": false,
  "github.copilot.chat.otel.captureIdentity": false,
  "github.copilot.chat.otel.resourceAttributes": {
    "workbench.task.id": "$TaskId",
    "workbench.harness": "vscode-copilot-chat"
  },

3. Run "Developer: Reload Window". Make sure Copilot Chat is signed in.
4. In the Chat view, pick Agent mode and send one small task that uses a tool, for example
   "List the files in this folder and read README.md".
5. Wait about 30 seconds, stop the receiver (Ctrl+C), then run: $Self schema
6. Revert: delete the keys above from User Settings (JSON) and reload the window. They apply to
   every VS Code window while present.

Note: $note
hve-core observed prompt text on spans even with captureContent=false, so treat any raw capture as sensitive.
"@
    }

    'schema' {
        if (-not $Path) {
            $latest = $null
            if (Test-Path $CaptureRoot) {
                $latest = Get-ChildItem -Path $CaptureRoot -Directory |
                    Where-Object { @(Get-ChildItem -Path $_.FullName -Filter '*.jsonl' -File).Count -gt 0 } |
                    Sort-Object LastWriteTime -Descending | Select-Object -First 1
            }
            if (-not $latest) { throw "No capture with .jsonl files under $CaptureRoot. Pass -Path." }
            $Path = $latest.FullName
        }
        Invoke-Probe @('schema', $Path)
        $exitCode = $LASTEXITCODE
    }
}
exit $exitCode
