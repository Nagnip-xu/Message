param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$projectDirectory = $PSScriptRoot
Set-Location -LiteralPath $projectDirectory
$url = 'http://127.0.0.1:8765'
function Test-Relay {
    try {
        $health = Invoke-RestMethod -Uri "$url/api/health" -TimeoutSec 2
        return ($health.app -eq 'telegram-feishu-relay')
    } catch { return $false }
}
if (Test-Relay) {
    Write-Host "Message Relay is already running: $url"
    if (-not $NoBrowser) { Start-Process $url }
    exit 0
}
$python = Join-Path $projectDirectory '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    Write-Host 'Creating local Python environment...'
    & python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.11 or later and try again.' }
    & $python -m pip install -r requirements.lock.txt
    if ($LASTEXITCODE -ne 0) { throw 'Python dependencies could not be installed.' }
}
if (-not (Test-Path -LiteralPath (Join-Path $projectDirectory 'frontend\dist\index.html'))) {
    Push-Location -LiteralPath (Join-Path $projectDirectory 'frontend')
    try {
        & npm.cmd ci
        if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed. Install Node.js 22 or later.' }
        & npm.cmd run build
        if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
    } finally { Pop-Location }
}
$dataDirectory = Join-Path $projectDirectory 'data'
New-Item -ItemType Directory -Path $dataDirectory -Force | Out-Null
$serverScript = Join-Path $projectDirectory 'run_server.py'
$serverProcess = Start-Process -FilePath $python -ArgumentList @("`"$serverScript`"") -WorkingDirectory $projectDirectory -WindowStyle Hidden -RedirectStandardOutput (Join-Path $dataDirectory 'server.log') -RedirectStandardError (Join-Path $dataDirectory 'server-error.log') -PassThru
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    Start-Sleep -Milliseconds 500
    if (Test-Relay) {
        Write-Host "Message Relay is running: $url"
        Write-Host 'The background service keeps running after the browser closes.'
        if (-not $NoBrowser) { Start-Process $url }
        exit 0
    }
    if ($serverProcess.HasExited) { break }
}
throw 'Startup failed. Check data\server-error.log, or check whether port 8765 is occupied.'
