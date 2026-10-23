param([switch]$Live)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    $python = Join-Path $root "backend\.venv\Scripts\python.exe"
    if ($Live) { & $python -m app.probe --live } else { & $python -m app.probe }
    if ($LASTEXITCODE -ne 0) { throw "Copilot probe blocked; see reports and sign-in guidance." }
} finally { Pop-Location }
