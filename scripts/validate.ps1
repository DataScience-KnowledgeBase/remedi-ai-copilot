param([switch]$Browser)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    & ".\backend\.venv\Scripts\ruff.exe" check backend\app backend\tests scripts\smoke.py scripts\verify_vendor.py
    if ($LASTEXITCODE -ne 0) { throw "Backend lint failed" }
    & ".\backend\.venv\Scripts\python.exe" -m pytest backend\tests -q
    if ($LASTEXITCODE -ne 0) { throw "Backend tests failed" }
    & ".\backend\.venv\Scripts\python.exe" scripts\verify_vendor.py
    if ($LASTEXITCODE -ne 0) { throw "Private dependency artifact verification failed" }
    & ".\backend\.venv\Scripts\python.exe" -m pip check
    if ($LASTEXITCODE -ne 0) { throw "Dependency consistency check failed" }
    New-Item -ItemType Directory -Force reports | Out-Null
    & ".\backend\.venv\Scripts\python.exe" -m pip_audit --local --format json --output reports\python-audit.json
    if ($LASTEXITCODE -ne 0) { throw "Python dependency audit failed" }
    & ".\backend\.venv\Scripts\python.exe" -m pip_audit --no-deps --disable-pip -r backend\vendor\upstream-audit.txt
    if ($LASTEXITCODE -ne 0) { throw "Private ADK upstream-base audit failed" }
    docker compose config --quiet
    if ($LASTEXITCODE -ne 0) { throw "Compose validation failed" }
    Push-Location frontend
    try {
        npm ci
        if ($LASTEXITCODE -ne 0) { throw "Frontend dependency restore failed" }
        npm run build
        if ($LASTEXITCODE -ne 0) { throw "Frontend build failed" }
        npm audit --audit-level=high
        if ($LASTEXITCODE -ne 0) { throw "Dependency audit failed" }
        if ($Browser) {
            npm test
            if ($LASTEXITCODE -ne 0) { throw "Browser workflow test failed" }
        }
    } finally { Pop-Location }
} finally { Pop-Location }
