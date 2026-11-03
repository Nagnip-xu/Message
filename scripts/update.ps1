$ErrorActionPreference = 'Stop'
$projectDirectory = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectDirectory
& (Join-Path $projectDirectory '.venv\Scripts\python.exe') -m pip install -r requirements.lock.txt
if ($LASTEXITCODE -ne 0) { throw 'Python install failed.' }
Push-Location frontend
try {
    & npm.cmd ci
    if ($LASTEXITCODE -ne 0) { throw 'npm install failed.' }
    & npm.cmd run build
    if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
} finally { Pop-Location }
Write-Host 'Dependencies and frontend updated. Restart with Stop.cmd and Start.cmd.'
