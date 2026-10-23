param([int]$Port = 8081)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    if (-not (Test-Path ".env")) { throw "Run scripts\bootstrap.ps1 first." }
    if (-not (Test-Path "frontend\dist\index.html")) {
        throw "Run npm ci and npm run build in frontend first."
    }
    & ".\backend\.venv\Scripts\python.exe" -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port $Port --workers 1 --no-access-log
    if ($LASTEXITCODE -ne 0) { throw "RemediAI Copilot server exited unsuccessfully." }
} finally { Pop-Location }
