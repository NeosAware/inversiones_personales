function Get-PreferredLocalIp {
  $defaultRoute = Get-NetRoute -DestinationPrefix "0.0.0.0/0" -ErrorAction SilentlyContinue |
    Where-Object { $_.NextHop -and $_.NextHop -ne "0.0.0.0" } |
    Sort-Object RouteMetric, InterfaceMetric |
    Select-Object -First 1

  if ($defaultRoute) {
    $defaultRouteIp = Get-NetIPAddress -InterfaceIndex $defaultRoute.InterfaceIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
      Where-Object { $_.IPAddress -notlike "169.254*" -and $_.IPAddress -ne "127.0.0.1" } |
      Select-Object -First 1 -ExpandProperty IPAddress

    if ($defaultRouteIp) {
      return $defaultRouteIp
    }
  }

  $fallbackIp = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notlike "169.254*" -and $_.IPAddress -notlike "127.*" -and $_.PrefixOrigin -ne "WellKnown" } |
    Sort-Object InterfaceMetric |
    Select-Object -First 1 -ExpandProperty IPAddress

  if ($fallbackIp) {
    return $fallbackIp
  }

  return "127.0.0.1"
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

    throw "Port 8000 is already in use by $($process.Name) (PID $($listener.OwningProcess)). Close it before starting the home server."
  }
}

function Ensure-FirewallRuleForActiveProfile {
  $ruleName = "Personal Investments Hub 8000"
  $activeProfiles = Get-NetConnectionProfile -ErrorAction SilentlyContinue |
    Where-Object { $_.IPv4Connectivity -ne "Disconnected" -and $_.NetworkCategory } |
    Select-Object -ExpandProperty NetworkCategory -Unique

  if (-not $activeProfiles) {
    $activeProfiles = @("Private")
  }

  $profileValue = ($activeProfiles | ForEach-Object { $_.ToString() }) -join ","
  try {
    $existingRule = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
    if (-not $existingRule) {
      New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8000 -Profile $profileValue -ErrorAction Stop | Out-Null
      Write-Host "Created Windows firewall rule for TCP 8000 on profiles: $profileValue."
    }
  }
  catch {
    Write-Warning "Could not create the Windows firewall rule automatically. Start PowerShell as Administrator and allow TCP 8000 for the active network profile ($profileValue)."
  }
}

$localIp = Get-PreferredLocalIp
$hostNames = @("127.0.0.1", "localhost", $env:COMPUTERNAME, $localIp) | Where-Object { $_ } | Select-Object -Unique
$trustedOrigins = @(
  "http://127.0.0.1:8000",
  "http://localhost:8000",
  "http://$($env:COMPUTERNAME):8000",
  "http://$localIp`:8000"
) | Select-Object -Unique

Configure-DatabaseEnvironment
$env:APP_HOME_NETWORK_MODE = "1"
$env:APP_ALLOWED_HOSTS = ($hostNames -join ",")
$env:APP_CSRF_TRUSTED_ORIGINS = ($trustedOrigins -join ",")

Stop-ExistingDjangoServer
Ensure-FirewallRuleForActiveProfile

Write-Host "Starting server for local network access at http://$localIp`:8000/login/"
Write-Host "Monica can open: http://$localIp`:8000/login/"
python manage.py runserver 0.0.0.0:8000
