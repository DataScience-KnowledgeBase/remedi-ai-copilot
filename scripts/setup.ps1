$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    if (-not (Test-Path "backend\.venv\Scripts\python.exe")) {
        python -m venv backend\.venv
        if ($LASTEXITCODE -ne 0) { throw "Creating isolated Python environment failed." }
    }
    $python = Join-Path $root "backend\.venv\Scripts\python.exe"
    & $python -m pip install --upgrade pip==26.2.2
    if ($LASTEXITCODE -ne 0) { throw "Updating isolated pip failed." }
    & $python -m pip install --require-hashes -r backend\requirements.lock
    if ($LASTEXITCODE -ne 0) { throw "Runtime dependency restore failed." }
    & $python -m pip install --no-deps -e backend
    if ($LASTEXITCODE -ne 0) { throw "Application install failed." }
    & $python -m copilot download-runtime
    if ($LASTEXITCODE -ne 0) { throw "Official Copilot runtime download failed." }
    foreach ($folder in @("frontend", "tools")) {
        Push-Location $folder
        try {
            npm ci
            if ($LASTEXITCODE -ne 0) { throw "$folder dependency restore failed." }
        } finally { Pop-Location }
    }
    Push-Location frontend
    try {
        npm run build
        if ($LASTEXITCODE -ne 0) { throw "Frontend build failed." }
    } finally { Pop-Location }
    if (-not (Test-Path ".env")) { & ".\scripts\bootstrap.ps1" }
    Write-Host "Setup complete. Next: scripts\copilot-login.ps1, scripts\probe.ps1 -Live, scripts\start.ps1"
} finally { Pop-Location }
