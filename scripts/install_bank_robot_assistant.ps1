param(
  [string]$ConfigPath = "",
  [string]$TaskName = "Asistente bancario local",
  [int]$Port = 8765
)

$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $ConfigPath) {
  $ConfigPath = Join-Path $PSScriptRoot "bank_robot.config.json"
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
  Copy-Item (Join-Path $PSScriptRoot "bank_robot.example.json") $ConfigPath
}

$startScript = Join-Path $PSScriptRoot "start_bank_robot_bridge.ps1"
$taskAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$startScript`" -ConfigPath `"$ConfigPath`" -Port $Port"
$taskTrigger = New-ScheduledTaskTrigger -AtLogOn
$taskSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName $TaskName -Action $taskAction -Trigger $taskTrigger -Settings $taskSettings -Force | Out-Null

& $startScript -ConfigPath $ConfigPath -Port $Port
Write-Host "Asistente bancario local instalado y arrancado."
