function Stop-ExistingDjangoServer {
  $listeners = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
  foreach ($listener in $listeners) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)" -ErrorAction SilentlyContinue
    if (-not $process) {
      continue
    }

    $isDjangoServer = $process.Name -match "^python" -and $process.CommandLine -like "*manage.py runserver*"
    if ($isDjangoServer) {
      Write-Host "Stopping previous Django server on port 8000 (PID $($listener.OwningProcess))..."
      Stop-Process -Id $listener.OwningProcess -Force
      Start-Sleep -Seconds 1
      continue
    }

    throw "Port 8000 is already in use by $($process.Name) (PID $($listener.OwningProcess)). Close it before starting the Cloudflare tunnel."
  }
}

function Get-StoredPostgresPassword {
  if ($env:POSTGRES_PASSWORD) {
    return $env:POSTGRES_PASSWORD
  }

  $userValue = [Environment]::GetEnvironmentVariable("POSTGRES_PASSWORD", "User")
  if ($userValue) {
    return $userValue
  }

  return [Environment]::GetEnvironmentVariable("POSTGRES_PASSWORD", "Machine")
}

function Configure-DatabaseEnvironment {
  $requestedDbEngine = ""
  if ($env:DB_ENGINE) {
    $requestedDbEngine = $env:DB_ENGINE.Trim().ToLowerInvariant()
  }

  if ($requestedDbEngine -and $requestedDbEngine -notin @("postgres", "postgresql")) {
    Write-Host "Using DB engine from current environment: $($env:DB_ENGINE)"
    return
  }

  $storedPostgresPassword = Get-StoredPostgresPassword
  if ($storedPostgresPassword) {
    $env:DB_ENGINE = "postgresql"
    $env:POSTGRES_DB = if ($env:POSTGRES_DB) { $env:POSTGRES_DB } else { "inversiones_personales" }
    $env:POSTGRES_USER = if ($env:POSTGRES_USER) { $env:POSTGRES_USER } else { "postgres" }
    $env:POSTGRES_HOST = if ($env:POSTGRES_HOST) { $env:POSTGRES_HOST } else { "127.0.0.1" }
    $env:POSTGRES_PORT = if ($env:POSTGRES_PORT) { $env:POSTGRES_PORT } else { "5432" }
    $env:POSTGRES_PASSWORD = $storedPostgresPassword
    Write-Host "Using PostgreSQL because POSTGRES_PASSWORD is configured."
    return
  }

  throw "POSTGRES_PASSWORD is not configured and SQLite fallback is disabled. Configure PostgreSQL or set DB_ENGINE=sqlite explicitly only for a local temporary session."
}

function Get-CloudflaredPath {
  $command = Get-Command cloudflared -ErrorAction SilentlyContinue
  if ($command) {
    return $command.Source
  }

  $wingetPackageRoot = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages"
  $wingetCloudflared = Get-ChildItem $wingetPackageRoot -Recurse -Filter cloudflared.exe -ErrorAction SilentlyContinue |
    Select-Object -First 1 -ExpandProperty FullName

  if ($wingetCloudflared) {
    return $wingetCloudflared
  }

  return $null
}

$cloudflaredPath = Get-CloudflaredPath
if (-not $cloudflaredPath) {
  throw "cloudflared is not installed or could not be located. Install it first with: winget install --id Cloudflare.cloudflared"
}

Configure-DatabaseEnvironment
$env:APP_HOME_NETWORK_MODE = "0"
$env:APP_ALLOWED_HOSTS = "127.0.0.1,localhost,.trycloudflare.com"
$env:APP_CSRF_TRUSTED_ORIGINS = "http://127.0.0.1:8000,http://localhost:8000,https://*.trycloudflare.com"

Stop-ExistingDjangoServer

Write-Host "Starting Django on http://127.0.0.1:8000/login/"
$djangoProcess = Start-Process -FilePath python -ArgumentList "manage.py", "runserver", "127.0.0.1:8000" -PassThru

try {
  Start-Sleep -Seconds 2
  $response = Invoke-WebRequest -Uri "http://127.0.0.1:8000/login/" -UseBasicParsing -TimeoutSec 10
  if ($response.StatusCode -ne 200) {
    throw "Django did not respond correctly on localhost:8000."
  }

  Write-Host ""
  Write-Host "Starting Cloudflare Quick Tunnel..."
  Write-Host "This creates a public URL on the Internet while the tunnel window stays open."
  Write-Host "When cloudflared prints the https://...trycloudflare.com URL, send that URL to Monica."
  Write-Host "Press Ctrl+C to stop both the tunnel and Django."
  & $cloudflaredPath tunnel --url http://127.0.0.1:8000
}
finally {
  if ($djangoProcess -and -not $djangoProcess.HasExited) {
    Stop-Process -Id $djangoProcess.Id -Force
  }
}
