param(
    [ValidatePattern('^remedi-[a-z0-9]([-a-z0-9]{0,48}[a-z0-9])?$')]
    [string]$Namespace = "remedi-demo",
    [switch]$CreateExamples
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$context = "docker-desktop"
$privateDirectory = Join-Path $root ".runtime\kubernetes"
kubectl --context $context --request-timeout=15s get nodes
if ($LASTEXITCODE -ne 0) { throw "Docker Desktop Kubernetes is not reachable." }
$namespaceNames = @(kubectl --context $context get namespaces -o name)
if ($LASTEXITCODE -ne 0) { throw "Cannot check namespaces." }
if ($namespaceNames -notcontains "namespace/$Namespace") {
    kubectl --context $context create namespace $Namespace
    if ($LASTEXITCODE -ne 0) { throw "Cannot create the dedicated namespace." }
}
$access = (Get-Content -LiteralPath (Join-Path $root "kubernetes\access.yaml") -Raw).Replace("__NAMESPACE__", $Namespace)
$access | kubectl --context $context --namespace $Namespace apply -f -
if ($LASTEXITCODE -ne 0) { throw "Cannot provision namespace-scoped service-account access." }
if ($CreateExamples) {
    $existing = @(kubectl --context $context --namespace $Namespace get deployments -l app.kubernetes.io/managed-by=remedi-ai -o name)
    if ($LASTEXITCODE -ne 0) { throw "Cannot check for existing examples." }
    if ($existing.Count -gt 0) { throw "Examples already exist. Refusing to reset potentially approved workloads. Omit -CreateExamples to refresh credentials only." }
    kubectl --context $context --namespace $Namespace create -f (Join-Path $root "kubernetes\examples.yaml")
    if ($LASTEXITCODE -ne 0) { throw "Demo workload creation failed. Inspect namespace before retrying." }
}
New-Item -ItemType Directory -Force -Path $privateDirectory | Out-Null
$ca = kubectl config view --raw --minify --context $context -o 'jsonpath={.clusters[0].cluster.certificate-authority-data}'
if ($LASTEXITCODE -ne 0 -or -not $ca) { throw "Docker Desktop CA data unavailable; do not disable TLS validation." }
[System.IO.File]::WriteAllBytes((Join-Path $privateDirectory "ca.crt"), [Convert]::FromBase64String($ca))
$server = kubectl config view --minify --context $context -o 'jsonpath={.clusters[0].cluster.server}'
if ($LASTEXITCODE -ne 0 -or -not $server) { throw "Cluster API endpoint is unavailable." }
$endpoint = [Uri]$server
if ($endpoint.Scheme -ne "https" -or $endpoint.UserInfo -or $endpoint.Query -or $endpoint.Fragment) {
    throw "Cluster API must use a credential-free HTTPS endpoint."
}
$tlsName = kubectl config view --minify --context $context -o 'jsonpath={.clusters[0].cluster.tls-server-name}'
if ($LASTEXITCODE -ne 0) { throw "Cannot inspect cluster TLS server name." }
if (-not $tlsName) { $tlsName = $endpoint.DnsSafeHost }
$builder = [UriBuilder]$endpoint
if ($endpoint.Host -in @("127.0.0.1", "localhost", "::1")) {
    $builder.Host = "host.docker.internal"
}
$connection = "KUBERNETES_API_URL=$($builder.Uri.AbsoluteUri.TrimEnd('/'))`nKUBERNETES_TLS_SERVER_NAME=$tlsName`n"
[System.IO.File]::WriteAllText((Join-Path $privateDirectory "connection.env"), $connection, [System.Text.UTF8Encoding]::new($false))
$token = kubectl --context $context --namespace $Namespace create token remedi-agent --duration=24h
if ($LASTEXITCODE -ne 0 -or -not $token) { throw "Cannot issue the short-lived namespace service-account credential." }
[System.IO.File]::WriteAllText((Join-Path $privateDirectory "token"), $token.Trim(), [System.Text.UTF8Encoding]::new($false))
$token = $null
$ca = $null
Write-Host "Namespace-scoped credentials saved in ignored .runtime\kubernetes (requested lifetime 24h; server may shorten it)."
Write-Host "No remediation was executed. Renew by rerunning this script without -CreateExamples."
Write-Host "API transport: $($builder.Uri.Authority); TLS certificate name: $tlsName (CA verification remains enabled)."
Write-Host "Enable compose.kubernetes.yaml explicitly; never mount your administrator kubeconfig."
