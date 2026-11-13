<#
  RR Trading One - remove the infrastructure services and program files.
  User data (database, backups, secrets, logs in C:\ProgramData\RR Trading One) is KEPT unless
  -RemoveUserData is given together with -ConfirmRemoveUserData "DELETE MY DATA".
#>
param([string]$InstallRoot = "$env:ProgramFiles\RR Trading One", [string]$DataRoot = "$env:ProgramData\RR Trading One",
      [switch]$RemoveUserData, [string]$ConfirmRemoveUserData = "")
$ErrorActionPreference = "Stop"
. (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "common.ps1")
Assert-Admin
if ($RemoveUserData -and $ConfirmRemoveUserData -ne "DELETE MY DATA") { throw "To delete user data pass -ConfirmRemoveUserData 'DELETE MY DATA'." }
Step "Stopping services"
Stop-EngineServices @("RRTradingOne-Worker", "RRTradingOne-Learner", "RRTradingOne-API")
foreach ($id in @("RRTradingOne-Worker", "RRTradingOne-Learner", "RRTradingOne-API")) {
    $exe = Join-Path $InstallRoot "infrastructure\services\$id.exe"
    if ((Get-Service $id -ErrorAction SilentlyContinue) -and (Test-Path $exe)) { Invoke-Checked $exe @("uninstall") | Out-Null; Info "$id removed" }
}
if (Get-Service "RRTradingOne-Redis" -ErrorAction SilentlyContinue) {
    Stop-Service "RRTradingOne-Redis" -Force
    Invoke-Checked (Join-Path $InstallRoot "infrastructure\redis\redis-server.exe") @("--service-uninstall", "--service-name", "RRTradingOne-Redis")
}
if (Get-Service "RRTradingOne-PostgreSQL" -ErrorAction SilentlyContinue) {
    Stop-Service "RRTradingOne-PostgreSQL" -Force
    Invoke-Checked (Join-Path $InstallRoot "infrastructure\pgsql\bin\pg_ctl.exe") @("unregister", "-N", "RRTradingOne-PostgreSQL")
}
Unregister-ScheduledTask -TaskName "RR Trading One - Backup" -Confirm:$false -ErrorAction SilentlyContinue
Step "Removing program files"
Remove-Item -Recurse -Force $InstallRoot -ErrorAction SilentlyContinue
if ($RemoveUserData) { Step "Removing user data"; Remove-Item -Recurse -Force $DataRoot }
else { Info "User data kept in $DataRoot (database, backups, secrets, logs)." }
