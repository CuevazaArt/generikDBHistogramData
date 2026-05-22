<#
.SYNOPSIS
    Stop the local PostgreSQL container started by pg_local_up.ps1.

.DESCRIPTION
    Runs `docker compose -f infra/docker-compose.yml down`. The persistent volume
    `pg_data` is preserved by default; pass -RemoveVolume to drop it as well.

.PARAMETER RemoveVolume
    When set, runs `docker compose down -v`, wiping the pg_data volume. Use with
    caution: this destroys all rows stored locally.
#>
[CmdletBinding()]
param(
    [switch]$RemoveVolume
)

$ErrorActionPreference = "Stop"

function Resolve-RepoRoot {
    $here = Split-Path -Parent $MyInvocation.MyCommand.Path
    return (Resolve-Path (Join-Path $here "..")).Path
}

function Test-DockerAvailable {
    try {
        $null = & docker info 2>$null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

$repoRoot = Resolve-RepoRoot
$composeFile = Join-Path $repoRoot "infra\docker-compose.yml"

if (-not (Test-DockerAvailable)) {
    Write-Warning "Docker is not running. Nothing to do for the containerized path."
    Write-Host "If you installed PostgreSQL natively, stop it via:"
    Write-Host "  Stop-Service postgresql-x64-16  # or whichever service name"
    exit 0
}

Push-Location $repoRoot
try {
    if ($RemoveVolume) {
        & docker compose -f $composeFile down -v
    } else {
        & docker compose -f $composeFile down
    }
    $code = $LASTEXITCODE
} finally {
    Pop-Location
}

if ($code -ne 0) {
    Write-Error "docker compose down exited with $code"
    exit $code
}

Write-Host "PostgreSQL container stopped."
exit 0
