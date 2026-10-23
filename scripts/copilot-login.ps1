$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$cli = Join-Path $root "tools\node_modules\.bin\copilot.cmd"
if (-not (Test-Path $cli)) { throw "Run npm ci in tools first." }
$homeDirectory = Join-Path $root ".runtime\copilot"
New-Item -ItemType Directory -Force -Path $homeDirectory | Out-Null
$oldHome = $env:COPILOT_HOME
try {
    $env:COPILOT_HOME = $homeDirectory
    Write-Host "Sign in to your own Copilot-enabled GitHub account in the browser."
    Write-Host "Never paste an authorization code, token, or password into chat."
    & $cli login --web-flow
    if ($LASTEXITCODE -ne 0) { throw "Copilot sign-in did not complete." }
} finally {
    $env:COPILOT_HOME = $oldHome
}
