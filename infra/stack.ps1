<#
.SYNOPSIS
    Local monitoring stack for agent-dev-workbench (Windows PowerShell 5.1 or later).

.DESCRIPTION
    init             Create infra\.env (gitignored) with the Grafana login. Random password unless -Prompt.
    up               Start the collector + Grafana/Prometheus/Tempo/Loki stack (Docker Desktop must be running).
                     -SchemaTap also copies received telemetry to the probe receiver on 127.0.0.1:-ProbePort.
    down             Stop the stack and keep all data. -DeleteData also deletes telemetry and Grafana's database.
    status           Show containers and run the read-only store checks.
    verify           Read-only: list what the store holds per harness and run every dashboard query.
    selftest         Start a separate, throwaway stack (project adw-selftest, other ports), send the
                     SYNTHETIC fixtures, check the stored results and the dashboard queries, then delete it.
    vscode-settings  Print the VS Code user settings that point Copilot Chat at this stack.
    claude           Launch Claude Code with telemetry aimed at this stack (fresh terminal, not inside Claude Code).
    logs             Show the collector's recent log lines.

    Nothing here edits VS Code or Claude Code settings files.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\infra\stack.ps1 up
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0, Mandatory = $true)]
    [ValidateSet('init', 'up', 'down', 'status', 'verify', 'selftest', 'vscode-settings', 'claude', 'logs')]
    [string]$Command,

    [switch]$SchemaTap,
    [ValidateRange(1, 65535)]
    [int]$ProbePort = 4319,

    [switch]$DeleteData,
    [switch]$Prompt,
    [switch]$Force,
    [switch]$KeepSelftestStack,

    [ValidatePattern('^[A-Za-z0-9._-]{1,64}$')]
    [string]$TaskId,

    [string[]]$ClaudeArgs = @()
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$InfraDir = $PSScriptRoot
$RepoDir = Split-Path -Parent $InfraDir
$EnvFile = Join-Path $InfraDir '.env'
$HelperPy = Join-Path $InfraDir 'workbench.py'
$Self = 'powershell -ExecutionPolicy Bypass -File "' + (Join-Path $InfraDir 'stack.ps1') + '"'
$ProbeSelf = 'powershell -ExecutionPolicy Bypass -File "' + (Join-Path $RepoDir 'probe\probe.ps1') + '"'
$GrafanaUrl = 'http://127.0.0.1:3000'
$OtlpHttp = 'http://127.0.0.1:4318'

function Get-Python {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) { return @($python.Source) }
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) { return @($launcher.Source, '-3') }
    throw 'Python 3.9 or later is required (python or py on PATH).'
}

function Invoke-Helper {
    param([string[]]$HelperArgs)
    $py = @(Get-Python)
    $exe = $py[0]
    $pre = @()
    if ($py.Count -gt 1) { $pre = $py[1..($py.Count - 1)] }
    # Out-Host: a native command's output inside a function would otherwise
    # become part of the function's return value instead of reaching the console.
    & $exe @pre -I -u $HelperPy @HelperArgs | Out-Host
    return $LASTEXITCODE
}

function Assert-Docker {
    $null = & docker info --format '{{.ServerVersion}}' 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw 'The Docker engine is not reachable. Start Docker Desktop and wait until it reports "Engine running".'
    }
}

function Invoke-Compose {
    param([string[]]$ComposeArgs, [switch]$WithTap)
    $files = @('-f', (Join-Path $InfraDir 'compose.yaml'))
    if ($WithTap) { $files += @('-f', (Join-Path $InfraDir 'compose.schema-tap.yaml')) }
    & docker compose --project-directory $InfraDir @files @ComposeArgs | Out-Host
    return $LASTEXITCODE
}

function Test-Port {
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

function New-Password {
    $bytes = New-Object byte[] 18
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return [Convert]::ToBase64String($bytes).Replace('+', '-').Replace('/', '_')
}

function Write-EnvFile {
    if ((Test-Path $EnvFile) -and -not $Force) {
        Write-Host "infra\.env already exists; leaving it unchanged (use -Force to replace it)."
        return
    }
    if ($Prompt) {
        $secure = Read-Host 'Grafana admin password (12+ characters, no spaces, quotes, $ or #)' -AsSecureString
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        try { $password = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
        if ($password.Length -lt 12 -or $password -match '[\s"''`$#]') {
            throw 'Password rejected: use 12 or more characters without spaces, quotes, $ or #.'
        }
    } else {
        $password = New-Password
    }
    $content = @"
# Local Grafana login for the agent-dev-workbench stack (http://127.0.0.1:3000).
# Written by infra\stack.ps1 init. Gitignored; do not commit.
# Grafana adopts these only when it creates its database (first 'up', or after
# 'down -DeleteData'). Changing them later does not change the live login.
ADW_GRAFANA_USER=admin
ADW_GRAFANA_PASSWORD=$password
"@
    [IO.File]::WriteAllText($EnvFile, $content.Replace("`r`n", "`n") + "`n", (New-Object System.Text.UTF8Encoding $false))
    Write-Host "Wrote $EnvFile (user 'admin'). The password is in that file; it is not printed here."
}

function Show-Endpoints {
    Write-Host ''
    Write-Host "Grafana    $GrafanaUrl  (dashboards > Agent Dev Workbench; login in infra\.env)"
    Write-Host "OTLP HTTP  $OtlpHttp   (VS Code / Claude Code export here; the collector filters before storage)"
    Write-Host 'OTLP gRPC  127.0.0.1:4317'
    Write-Host ''
}

$exitCode = 0
switch ($Command) {
    'init' {
        Write-EnvFile
    }

    'up' {
        Assert-Docker
        if (-not (Test-Path $EnvFile)) { Write-EnvFile }
        if ($SchemaTap) {
            $env:ADW_PROBE_PORT = "$ProbePort"
            if (-not (Test-Port $ProbePort)) {
                Write-Warning "Schema tap: nothing listens on 127.0.0.1:$ProbePort yet. Start the probe in another terminal: $ProbeSelf receive -Port $ProbePort -Label vscode"
            }
        }
        $exitCode = Invoke-Compose -ComposeArgs @('up', '-d', '--wait', '--wait-timeout', '300') -WithTap:$SchemaTap
        if ($exitCode -eq 0) {
            Show-Endpoints
            if ($SchemaTap) { Write-Host "Schema tap ON: received telemetry is also copied, unfiltered, to the probe on 127.0.0.1:$ProbePort (it redacts at ingest). Run 'up' without -SchemaTap to turn it off." }
            Write-Host "Next: $Self vscode-settings   (or: $Self claude)"
        }
    }

    'down' {
        Assert-Docker
        if ($DeleteData) {
            $exitCode = Invoke-Compose -ComposeArgs @('down', '-v')
            Write-Host 'Stack stopped; telemetry and Grafana database volumes deleted.'
        } else {
            $exitCode = Invoke-Compose -ComposeArgs @('down')
            Write-Host 'Stack stopped; data kept. -DeleteData removes it.'
        }
    }

    'status' {
        Assert-Docker
        $null = Invoke-Compose -ComposeArgs @('ps')
        $exitCode = Invoke-Helper @('verify', '--brief')
    }

    'verify' {
        $exitCode = Invoke-Helper @('verify')
    }

    'selftest' {
        Assert-Docker
        $helperArgs = @('selftest')
        if ($KeepSelftestStack) { $helperArgs += '--keep' }
        $exitCode = Invoke-Helper $helperArgs
    }

    'logs' {
        Assert-Docker
        $exitCode = Invoke-Compose -ComposeArgs @('logs', '--tail', '80', 'otel-collector')
    }

    'vscode-settings' {
        if (-not $TaskId) { $TaskId = 'unattributed' }
        if (-not (Test-Port 4318)) { Write-Warning "Nothing listens on 127.0.0.1:4318. Start the stack first: $Self up" }
        Write-Host @"
VS Code Copilot Chat -> local stack. Nothing here edits your settings.

1. In VS Code run "Preferences: Open User Settings (JSON)". If you use a non-default profile, run
   "Preferences: Open Application Settings (JSON)" instead: these keys are application-scoped in
   copilot-chat 0.72.0 and are read only from the default profile's settings.json.
2. Add these keys inside the top-level object (keep everything else as is):

  "github.copilot.chat.otel.enabled": true,
  "github.copilot.chat.otel.exporterType": "otlp-http",
  "github.copilot.chat.otel.otlpEndpoint": "$OtlpHttp",
  "github.copilot.chat.otel.captureContent": false,
  "github.copilot.chat.otel.captureIdentity": false,
  "github.copilot.chat.otel.resourceAttributes": {
    "workbench.task.id": "$TaskId"
  },

3. Save, then quit VS Code completely (File > Exit in every window) and start it again.
   "Developer: Reload Window" is not enough: VS Code's agent host (the shared process that runs
   Copilot CLI sessions) keeps the configuration it started with. Copilot Chat must be signed in.
4. In the Chat view pick Agent mode and send a small task that uses a tool, for example
   "List the files in this folder and read README.md".
5. After about 60 seconds: $Self verify   (Grafana: Agent Dev Workbench > Agent workflow overview)
6. To change the task, edit workbench.task.id and restart VS Code. To stop exporting, set
   "github.copilot.chat.otel.enabled": false (or delete the keys) and restart VS Code.

Harness labels: the Copilot Chat extension's own agent reports service.name copilot-chat
(vscode-copilot-chat); Copilot CLI sessions, inside VS Code or standalone, report github-copilot
(github-copilot-runtime).

captureContent=false does not guarantee spans are free of prompt text (hve-core finding); the
collector's allow-list is what keeps content out of storage.
"@
    }

    'claude' {
        if (-not $TaskId) { $TaskId = 'unattributed' }
        if (-not (Test-Port 4318)) { throw "Nothing listens on 127.0.0.1:4318. Start the stack first: $Self up" }
        $probeArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $RepoDir 'probe\probe.ps1'),
                       'claude', '-Port', '4318', '-TaskId', $TaskId)
        if ($ClaudeArgs.Count -gt 0) { $probeArgs += @('-ClaudeArgs') + $ClaudeArgs }
        & powershell @probeArgs
        $exitCode = $LASTEXITCODE
    }
}
exit $exitCode
