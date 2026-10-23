param([ValidateSet("copilot", "demo", "api")][string]$Provider = "copilot")
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$destination = Join-Path $root ".env"
if (Test-Path $destination) {
    throw ".env already exists. Edit it deliberately; bootstrap will not overwrite secrets."
}
function New-Token {
    $bytes = New-Object byte[] 32
    $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $generator.GetBytes($bytes) } finally { $generator.Dispose() }
    return [Convert]::ToBase64String($bytes)
}
$content = Get-Content (Join-Path $root ".env.example") -Raw
$content = $content.Replace("replace-with-distinct-random-operator-token", (New-Token))
$content = $content.Replace("replace-with-distinct-random-owner-token", (New-Token))
$content = $content -replace "(?m)^PROVIDER=copilot$", "PROVIDER=$Provider"
[System.IO.File]::WriteAllText($destination, $content, (New-Object System.Text.UTF8Encoding $false))
Write-Host "Created .env with distinct random credentials. Keep this file private."
Write-Host "For Copilot, run scripts\copilot-login.ps1 yourself; credentials must never be shared."
