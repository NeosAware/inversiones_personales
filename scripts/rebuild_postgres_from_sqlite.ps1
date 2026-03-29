param(
  [switch]$Force,
  [switch]$SkipBackup,
  [switch]$IncludeMedia
)

if (-not $Force) {
  Write-Error "This command deletes all PostgreSQL data in the target database. Re-run with -Force."
  exit 1
}

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

$env:POSTGRES_DB = if ($env:POSTGRES_DB) { $env:POSTGRES_DB } else { "inversiones_personales" }
$env:POSTGRES_USER = if ($env:POSTGRES_USER) { $env:POSTGRES_USER } else { "postgres" }
$env:POSTGRES_HOST = if ($env:POSTGRES_HOST) { $env:POSTGRES_HOST } else { "127.0.0.1" }
$env:POSTGRES_PORT = if ($env:POSTGRES_PORT) { $env:POSTGRES_PORT } else { "5432" }

if (-not $env:POSTGRES_PASSWORD) {
  Write-Error "POSTGRES_PASSWORD is not available in the environment."
  exit 1
}

$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$backupDir = Join-Path $projectRoot ("backups\" + (Get-Date -Format 'yyyy') + "\" + (Get-Date -Format 'MM'))
New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
$fixturePath = Join-Path $backupDir "sqlite_refresh_source_$stamp.json"

Write-Host "Exporting current SQLite data to $fixturePath"
$env:DB_ENGINE = "sqlite"
python manage.py dumpdata --natural-foreign --natural-primary --exclude contenttypes --exclude auth.permission --output $fixturePath
if ($LASTEXITCODE -ne 0) {
  Write-Error "SQLite export failed."
  exit $LASTEXITCODE
}

$env:DB_ENGINE = "postgresql"
if (-not $SkipBackup) {
  Write-Host "Creating PostgreSQL backup before reset"
  if ($IncludeMedia) {
    python manage.py backup_database --include-media
  } else {
    python manage.py backup_database
  }
  if ($LASTEXITCODE -ne 0) {
    Write-Error "Backup failed."
    exit $LASTEXITCODE
  }
}

Write-Host "Dropping and recreating PostgreSQL database $($env:POSTGRES_DB)"
@'
import os
import psycopg2
from psycopg2 import sql

target_db = os.environ["POSTGRES_DB"]
conn = psycopg2.connect(
    dbname="postgres",
    user=os.environ["POSTGRES_USER"],
    password=os.environ["POSTGRES_PASSWORD"],
    host=os.environ["POSTGRES_HOST"],
    port=os.environ["POSTGRES_PORT"],
)
conn.autocommit = True
cur = conn.cursor()
cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()", (target_db,))
cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(target_db)))
cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target_db)))
cur.close()
conn.close()
print(f"Recreated database {target_db}")
'@ | python -
if ($LASTEXITCODE -ne 0) {
  Write-Error "PostgreSQL database recreation failed."
  exit $LASTEXITCODE
}

Write-Host "Running migrations on PostgreSQL"
python manage.py migrate
if ($LASTEXITCODE -ne 0) {
  Write-Error "Migration failed on PostgreSQL."
  exit $LASTEXITCODE
}

Write-Host "Loading exported SQLite data into PostgreSQL"
python manage.py loaddata $fixturePath
if ($LASTEXITCODE -ne 0) {
  Write-Error "Fixture load failed."
  exit $LASTEXITCODE
}

Write-Host "Capturing fresh portfolio snapshot"
python manage.py capture_portfolio_snapshot
if ($LASTEXITCODE -ne 0) {
  Write-Error "Snapshot capture failed."
  exit $LASTEXITCODE
}

Write-Host "PostgreSQL rebuild completed successfully."
