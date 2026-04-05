param(
  [string]$TaskName = "Personal Investments - Bank Robot",
  [string]$Time = "08:00",
  [string]$ConfigPath = "",
  [string[]]$Job
)

$scriptPath = Join-Path $PSScriptRoot "run_bank_robot.ps1"
if (-not $ConfigPath) {
  $ConfigPath = Join-Path $PSScriptRoot "bank_robot.config.json"
}

$argumentList = @(
  "-NoProfile",
  "-ExecutionPolicy", "Bypass",
  "-File", ('"{0}"' -f $scriptPath),
  "-ConfigPath", ('"{0}"' -f $ConfigPath)
)
foreach ($jobId in $Job) {
  $argumentList += @("-Job", ('"{0}"' -f $jobId))
}
$argumentString = $argumentList -join " "

$trigger = New-ScheduledTaskTrigger -Daily -At $Time
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argumentString
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Host "Tarea programada creada: $TaskName a las $Time"
