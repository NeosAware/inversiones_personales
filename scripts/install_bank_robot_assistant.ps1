param(
  [string]$ConfigPath = "",
  [string]$TaskName = "Asistente bancario local",
  [int]$Port = 8765,
  [string]$RepoRoot = ""
)

function Resolve-RepoRoot {
  param(
    [string]$PreferredRoot
  )

  $candidates = @()
  if ($PreferredRoot) {
    $candidates += $PreferredRoot
  }
  if ($env:BANK_ROBOT_REPO_ROOT) {
    $candidates += $env:BANK_ROBOT_REPO_ROOT
  }
  if ($PSScriptRoot) {
    $candidates += (Split-Path -Parent $PSScriptRoot)
  }
  $candidates += (Join-Path $HOME "Documents\inversiones_personales")
  $candidates += (Join-Path $HOME "OneDrive\Documents\inversiones_personales")

  foreach ($candidate in ($candidates | Where-Object { $_ } | Select-Object -Unique)) {
    $requirementsPath = Join-Path $candidate "requirements-robot.txt"
    $robotPath = Join-Path $candidate "scripts\bank_robot.py"
    if ((Test-Path $requirementsPath) -and (Test-Path $robotPath)) {
      return (Resolve-Path $candidate).Path
    }
  }

  throw "No se ha encontrado la carpeta del proyecto. Si hace falta, vuelve a ejecutar el instalador indicando -RepoRoot `"C:\Users\Gerencia\Documents\inversiones_personales`"."
}

$repoRoot = Resolve-RepoRoot -PreferredRoot $RepoRoot
$scriptsDir = Join-Path $repoRoot "scripts"
if (-not $ConfigPath) {
  $ConfigPath = Join-Path $scriptsDir "bank_robot.config.json"
}

$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (Test-Path $venvPython) {
  $python = $venvPython
}
else {
  $python = "python"
}

Set-Location $repoRoot
& $python -m pip install -r requirements-robot.txt
if ($LASTEXITCODE -ne 0) {
  exit $LASTEXITCODE
}

& $python -m playwright install chromium
if ($LASTEXITCODE -ne 0) {
  exit $LASTEXITCODE
}

if (-not (Test-Path $ConfigPath)) {
  Copy-Item (Join-Path $scriptsDir "bank_robot.example.json") $ConfigPath
}

$startScript = Join-Path $scriptsDir "start_bank_robot_bridge.ps1"
$taskAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$startScript`" -ConfigPath `"$ConfigPath`" -Port $Port"
$taskTrigger = New-ScheduledTaskTrigger -AtLogOn
$taskSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName $TaskName -Action $taskAction -Trigger $taskTrigger -Settings $taskSettings -Force | Out-Null

& $startScript -ConfigPath $ConfigPath -Port $Port
Write-Host "Proyecto localizado en: $repoRoot"
Write-Host "Asistente bancario local instalado y arrancado."
