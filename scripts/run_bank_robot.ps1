param(
  [string]$ConfigPath = "",
  [string[]]$Job,
  [switch]$Headed,
  [switch]$Headless,
  [switch]$DryRun,
  [switch]$SkipUpload
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

$scriptPath = Join-Path $PSScriptRoot "bank_robot.py"
$arguments = @($scriptPath, "--config", $ConfigPath)

foreach ($jobId in $Job) {
  $arguments += @("--job", $jobId)
}
if ($Headed) {
  $arguments += "--headed"
}
if ($Headless) {
  $arguments += "--headless"
}
if ($DryRun) {
  $arguments += "--dry-run"
}
if ($SkipUpload) {
  $arguments += "--skip-upload"
}

Set-Location $repoRoot
& $python @arguments
exit $LASTEXITCODE
