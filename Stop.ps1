$ErrorActionPreference = 'Stop'
$pidFile = Join-Path $PSScriptRoot 'data\server.pid'
if (-not (Test-Path -LiteralPath $pidFile)) { Write-Host 'Message Relay is not running.'; exit 0 }
$relayProcessId = [int](Get-Content -LiteralPath $pidFile)
$relayProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$relayProcessId"
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$serverScript = Join-Path $PSScriptRoot 'run_server.py'
if ($null -eq $relayProcess) { Write-Host 'Background process has already stopped.'; exit 0 }
$expectedCommandLine = '"{0}" "{1}"' -f $python, $serverScript
$isPython = $relayProcess.ExecutablePath -and [IO.Path]::GetFileName($relayProcess.ExecutablePath) -ieq 'python.exe'
$isThisRelay = $relayProcess.CommandLine -and $relayProcess.CommandLine.Equals($expectedCommandLine, [StringComparison]::OrdinalIgnoreCase)
if (-not $isPython -or -not $isThisRelay) { throw 'PID does not belong to this Message Relay. No process was stopped.' }
try { $null = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8765/api/shutdown' -TimeoutSec 3 } catch { }
for ($attempt = 0; $attempt -lt 20; $attempt++) {
    Start-Sleep -Milliseconds 500
    if (-not (Get-Process -Id $relayProcessId -ErrorAction SilentlyContinue)) { Write-Host 'Message Relay stopped safely. Uncertain deliveries will be held for reconciliation.'; exit 0 }
}
throw 'The backend did not stop in time. Check data\server-error.log before closing it manually.'
