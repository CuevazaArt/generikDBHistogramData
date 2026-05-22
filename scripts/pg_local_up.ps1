<#
.SYNOPSIS
    Bring up the local PostgreSQL instance used by the backtesting framework.

.DESCRIPTION
    Preferred path is `docker compose -f infra/docker-compose.yml up -d`.
    If Docker is not running, prints instructions for installing PostgreSQL 16
    natively via chocolatey. After starting, the script waits for the DB to be
    reachable via psycopg and reports success/failure.

.PARAMETER Dsn
    Optional connection string. Defaults to env:PG_DSN or the local docker-compose
    DSN (postgresql://genericbt:genericbt@127.0.0.1:5433/genericbt).

.PARAMETER TimeoutSeconds
    How long to wait for the DB to become reachable. Default 60 seconds.
#>
[CmdletBinding()]
param(
    [string]$Dsn = $env:PG_DSN,
    [int]$TimeoutSeconds = 60
)

$ErrorActionPreference = "Stop"

function Resolve-RepoRoot {
    $here = Split-Path -Parent $MyInvocation.MyCommand.Path
    return (Resolve-Path (Join-Path $here "..")).Path
}

function Get-PsycopgDsn {
    param([string]$Raw)
    if ([string]::IsNullOrWhiteSpace($Raw)) {
        return "postgresql://genericbt:genericbt@127.0.0.1:5433/genericbt"
    }
    # psycopg 3 accepts postgresql:// but not postgresql+psycopg://. Strip the SQLAlchemy
    # driver suffix when running the raw connect probe.
    return ($Raw -replace "^postgresql\+psycopg://", "postgresql://")
}

function Test-DockerAvailable {
    try {
        $null = & docker info 2>$null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Wait-ForPostgres {
    param([string]$ProbeDsn, [int]$Seconds)
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        $probe = @"
import sys
try:
    import psycopg
except ImportError:
    sys.exit(2)
try:
    conn = psycopg.connect(r"$ProbeDsn", connect_timeout=3)
    conn.close()
    sys.exit(0)
except Exception as exc:
    sys.exit(1)
"@
        $tmp = [System.IO.Path]::GetTempFileName() + ".py"
        Set-Content -Path $tmp -Value $probe -Encoding UTF8
        try {
            & python $tmp
            $code = $LASTEXITCODE
        } finally {
            Remove-Item $tmp -ErrorAction SilentlyContinue
        }
        if ($code -eq 0) { return $true }
        if ($code -eq 2) {
            Write-Warning "psycopg not installed. Run: pip install 'psycopg[binary]>=3.1' psycopg_pool"
            return $false
        }
        Start-Sleep -Seconds 2
    }
    return $false
}

$repoRoot = Resolve-RepoRoot
$composeFile = Join-Path $repoRoot "infra\docker-compose.yml"
$probeDsn = Get-PsycopgDsn -Raw $Dsn

Write-Host "Resolved repo root : $repoRoot"
Write-Host "Compose file       : $composeFile"
Write-Host "Probe DSN          : $probeDsn"

if (Test-DockerAvailable) {
    Write-Host "Docker daemon detected, bringing the postgres service up..."
    Push-Location $repoRoot
    try {
        & docker compose -f $composeFile up -d
        if ($LASTEXITCODE -ne 0) {
            throw "docker compose up exited with $LASTEXITCODE"
        }
    } finally {
        Pop-Location
    }
} else {
    Write-Warning "Docker is not running. Two options:"
    Write-Host "  1) Start Docker Desktop and rerun this script."
    Write-Host "  2) Install PostgreSQL 16 natively, e.g.:"
    Write-Host "       choco install postgresql16 --params '/Password:genericbt /Port:5433'"
    Write-Host "       Then create the database and role manually:"
    Write-Host "       psql -U postgres -c `"CREATE ROLE genericbt LOGIN PASSWORD 'genericbt';`""
    Write-Host "       psql -U postgres -c `"CREATE DATABASE genericbt OWNER genericbt;`""
    exit 2
}

Write-Host "Waiting up to $TimeoutSeconds seconds for the database to accept connections..."
if (Wait-ForPostgres -ProbeDsn $probeDsn -Seconds $TimeoutSeconds) {
    Write-Host "PostgreSQL is reachable at $probeDsn"
    Write-Host "Next step: python scripts/pg_init.py --dsn `"$Dsn`""
    exit 0
} else {
    Write-Error "PostgreSQL did not become reachable within $TimeoutSeconds seconds."
    exit 1
}
