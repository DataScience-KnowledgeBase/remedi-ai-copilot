param(
    [Parameter(Mandatory = $true)][string]$EnvFile,
    [Parameter(Mandatory = $true)][ValidatePattern('^[a-f0-9]{40}$')][string]$ImageTag,
    [switch]$LiveSmoke
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
if (-not [System.IO.Path]::IsPathRooted($EnvFile) -or -not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
    throw "EnvFile must be an existing absolute private configuration path."
}
$previousEnvFile = $env:REMEDI_ENV_FILE
$previousTag = $env:REMEDI_IMAGE_TAG
$rollbackTag = "rollback-" + [Guid]::NewGuid().ToString("N")
$previousImages = @{}
$deploymentStarted = $false
Push-Location $root
try {
    $env:REMEDI_ENV_FILE = $EnvFile
    $env:REMEDI_IMAGE_TAG = $ImageTag
    $engine = docker info --format '{{.OSType}}'
    if ($LASTEXITCODE -ne 0 -or $engine -ne "linux") { throw "Docker Desktop Linux engine is required." }
    docker compose config --quiet
    if ($LASTEXITCODE -ne 0) { throw "Compose configuration is invalid." }
    foreach ($service in @("backend", "frontend")) {
        $container = docker compose ps -aq $service
        if ($LASTEXITCODE -ne 0) { throw "Cannot inspect current $service container." }
        if ($container) {
            $image = docker inspect --format '{{.Image}}' $container
            if ($LASTEXITCODE -ne 0) { throw "Cannot inspect current $service image." }
            $previousImages[$service] = $image
            docker image tag $image "remedi-ai-copilot-${service}:$rollbackTag"
            if ($LASTEXITCODE -ne 0) { throw "Cannot preserve $service rollback image." }
        }
    }
    docker compose build
    if ($LASTEXITCODE -ne 0) { throw "Image build failed; existing deployment was not replaced." }
    $deploymentStarted = $true
    docker compose up -d --no-build --wait --wait-timeout 180
    if ($LASTEXITCODE -ne 0) { throw "Deployment failed its container health checks." }
    $ready = Invoke-RestMethod -Uri "http://127.0.0.1:8082/readyz" -TimeoutSec 30
    if ($ready.status -ne "ready" -or $ready.provider -ne "copilot") {
        throw "Live Copilot readiness verification failed."
    }
    foreach ($service in @("backend", "frontend")) {
        $container = docker compose ps -q $service
        if ($LASTEXITCODE -ne 0 -or -not $container) { throw "$service is not running." }
        $runningImage = docker inspect --format '{{.Image}}' $container
        if ($LASTEXITCODE -ne 0) { throw "Cannot inspect running $service image." }
        $expectedImage = docker image inspect --format '{{.Id}}' "remedi-ai-copilot-${service}:$ImageTag"
        if ($LASTEXITCODE -ne 0 -or $runningImage -ne $expectedImage) {
            throw "$service is not running the tested image."
        }
        $imageLabels = docker image inspect --format '{{json .Config.Labels}}' "remedi-ai-copilot-${service}:$ImageTag"
        if ($LASTEXITCODE -ne 0) { throw "Cannot inspect $service image revision." }
        $imageRevision = ($imageLabels | ConvertFrom-Json).'org.opencontainers.image.revision'
        if ($imageRevision -ne $ImageTag) {
            throw "$service image revision does not match the tested commit."
        }
    }
    $smokeArguments = @()
    if ($LiveSmoke) { $smokeArguments += "--live" }
    Get-Content -Raw (Join-Path $PSScriptRoot "smoke.py") |
        docker compose exec -T backend python - @smokeArguments
    if ($LASTEXITCODE -ne 0) { throw "Deployment smoke verification failed." }
    docker compose ps --format '{{.Service}}: {{.State}} ({{.Health}})'
    if ($LASTEXITCODE -ne 0) { throw "Cannot report deployment status." }
    Write-Host "Deployed tested commit $ImageTag to http://127.0.0.1:8082"
} catch {
    $failure = $_
    if ($deploymentStarted -and $previousImages.Count -eq 2) {
        Write-Warning "Deployment verification failed. Restoring previous application images; data is preserved."
        $env:REMEDI_IMAGE_TAG = $rollbackTag
        docker compose up -d --no-build --wait --wait-timeout 180
        if ($LASTEXITCODE -ne 0) { throw "Deployment failed and rollback health checks also failed. Inspect this project's containers locally." }
        Write-Warning "Previous images restored. The deployment remains failed."
    } elseif ($deploymentStarted) {
        Write-Warning "No complete previous deployment exists; automatic rollback is unavailable."
    }
    throw $failure
} finally {
    $env:REMEDI_ENV_FILE = $previousEnvFile
    $env:REMEDI_IMAGE_TAG = $previousTag
    Pop-Location
}
