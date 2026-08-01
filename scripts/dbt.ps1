<#
.SYNOPSIS
    Run dbt with the project's .env loaded into the environment.

.DESCRIPTION
    dbt's env_var() reads real OS environment variables — it does NOT read
    .env files. This wrapper bridges the gap so .env stays the single source
    of truth for credentials, shared with pytest and the app container.

    In CI this script is unnecessary: the runner sets environment variables
    natively, so `dbt debug` can be called directly.

.EXAMPLE
    .\scripts\dbt.ps1 debug
    .\scripts\dbt.ps1 run --select stg_polygon__prices
    .\scripts\dbt.ps1 test
#>

$ErrorActionPreference = 'Stop'

$repoRoot   = Split-Path -Parent $PSScriptRoot
$dotenvExe  = Join-Path $repoRoot '.venv\Scripts\dotenv.exe'
$dbtExe     = Join-Path $repoRoot '.venv\Scripts\dbt.exe'
$envFile    = Join-Path $repoRoot '.env'
$projectDir = Join-Path $repoRoot 'dbt'

foreach ($required in @($dotenvExe, $dbtExe, $envFile, $projectDir)) {
    if (-not (Test-Path $required)) {
        throw "Required path not found: $required`nIs the venv created and .env present?"
    }
}

& $dotenvExe -f $envFile run -- $dbtExe @args --project-dir $projectDir
exit $LASTEXITCODE
