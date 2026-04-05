param(
  [string]$ConfigPath = "",
  [int]$Port = 8765
)

$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $ConfigPath) {
  $ConfigPath = Join-Path $PSScriptRoot "bank_robot.config.json"
}

$venvPython = Join-Path $repoRoot ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $venvPython)) {
  $venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
}
if (Test-Path $venvPython) {
  $python = $venvPython
}
else {
  $python = "pythonw"
}

$scriptPath = Join-Path $PSScriptRoot "bank_robot_bridge.py"
$arguments = @($scriptPath, "--config", $ConfigPath, "--port", "$Port")

Set-Location $repoRoot
Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $repoRoot -WindowStyle Hidden | Out-Null
