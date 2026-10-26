param(
    [Parameter(Mandatory = $true)][string]$EnvFile,
    [Parameter(Mandatory = $true)][ValidatePattern('^[a-f0-9]{40}$')][string]$ImageTag,
    [switch]$LiveSmoke,
    [string]$KubernetesCredentialsDirectory = "",
    [ValidatePattern('^remedi-[a-z0-9]([-a-z0-9]{0,48}[a-z0-9])?$')]
    [string]$KubernetesNamespace = "remedi-demo",
    [string]$GitHubPrivateKeyFile = "",
    [long]$GitHubAppId = 0,
    [long]$GitHubInstallationId = 0
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
if (-not [System.IO.Path]::IsPathRooted($EnvFile) -or -not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
    throw "EnvFile must be an existing absolute private configuration path."
}
$previousEnvFile = $env:REMEDI_ENV_FILE
$previousTag = $env:REMEDI_IMAGE_TAG
$previousComposeFile = $env:COMPOSE_FILE
$previousCredentials = $env:REMEDI_KUBERNETES_CREDENTIALS_DIR
$previousNamespace = $env:REMEDI_KUBERNETES_NAMESPACE
$previousGitHubKey = $env:REMEDI_GITHUB_PRIVATE_KEY_FILE
$previousAppId = $env:REMEDI_GITHUB_APP_ID
$previousInstallationId = $env:REMEDI_GITHUB_INSTALLATION_ID
$rollbackTag = "rollback-" + [Guid]::NewGuid().ToString("N")
$previousImages = @{}
$deploymentStarted = $false
Push-Location $root
try {
    $env:REMEDI_ENV_FILE = $EnvFile
    $env:REMEDI_IMAGE_TAG = $ImageTag
    if ($KubernetesCredentialsDirectory) {
        if (-not [System.IO.Path]::IsPathRooted($KubernetesCredentialsDirectory)) {
            throw "Kubernetes credentials directory must be an absolute private path."
        }
        foreach ($file in @("token", "ca.crt", "connection.env")) {
            if (-not (Test-Path -LiteralPath (Join-Path $KubernetesCredentialsDirectory $file) -PathType Leaf)) {
                throw "Missing Kubernetes credential file: $file. Run setup-kubernetes.ps1."
            }
        }
        $env:COMPOSE_FILE = "compose.yaml;compose.kubernetes.yaml"
        $env:REMEDI_KUBERNETES_CREDENTIALS_DIR = $KubernetesCredentialsDirectory
        $env:REMEDI_KUBERNETES_NAMESPACE = $KubernetesNamespace
    } else {
        $env:COMPOSE_FILE = "compose.yaml"
    }
    if ($GitHubPrivateKeyFile) {
        if (-not $KubernetesCredentialsDirectory -or $GitHubAppId -le 0 -or $GitHubInstallationId -le 0) {
            throw "GitHub mode requires Kubernetes credentials and valid App/installation IDs."
        }
        if (-not [System.IO.Path]::IsPathRooted($GitHubPrivateKeyFile) -or -not (Test-Path -LiteralPath $GitHubPrivateKeyFile -PathType Leaf)) {
            throw "GitHub private key must be an existing absolute private path outside the checkout."
        }
        $keyPath = (Resolve-Path -LiteralPath $GitHubPrivateKeyFile).Path
        if ($keyPath.StartsWith($root + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "GitHub private key must not be inside the repository."
        }
        $env:COMPOSE_FILE += ";compose.github.yaml"
        $env:REMEDI_GITHUB_PRIVATE_KEY_FILE = $keyPath
        $env:REMEDI_GITHUB_APP_ID = [string]$GitHubAppId
        $env:REMEDI_GITHUB_INSTALLATION_ID = [string]$GitHubInstallationId
    }
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
    if ($KubernetesCredentialsDirectory) { $smokeArguments += "--kubernetes" }
    if ($GitHubPrivateKeyFile) { $smokeArguments += "--github" }
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
    $env:COMPOSE_FILE = $previousComposeFile
    $env:REMEDI_KUBERNETES_CREDENTIALS_DIR = $previousCredentials
    $env:REMEDI_KUBERNETES_NAMESPACE = $previousNamespace
    $env:REMEDI_GITHUB_PRIVATE_KEY_FILE = $previousGitHubKey
    $env:REMEDI_GITHUB_APP_ID = $previousAppId
    $env:REMEDI_GITHUB_INSTALLATION_ID = $previousInstallationId
    Pop-Location
}
