<#
  RR Trading One - encrypted backup / verify / restore (wraps python -m app.ops.backup).
    backup.ps1 -Create                         take a backup now
    backup.ps1 -Verify  -File <x.rrbak>        decrypt and check integrity
    backup.ps1 -Restore -File <x.rrbak> -TargetDb rr_restore_20261113
       restores into a NEW database and checks every table's row count; the running database is untouched.
#>
param([switch]$Create, [switch]$Verify, [switch]$Restore, [string]$File = "", [string]$TargetDb = "", [string]$Label = "manual",
      [string]$InstallRoot = "$env:ProgramFiles\RR Trading One", [string]$DataRoot = "$env:ProgramData\RR Trading One")
$ErrorActionPreference = "Stop"
. (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "common.ps1")
Assert-Admin
$py = Join-Path $InstallRoot "infrastructure\venv\Scripts\python.exe"
$backend = Join-Path $InstallRoot "backend"
$envFile = Join-Path $DataRoot "secrets\engine.env"
$key = Join-Path $DataRoot "secrets\backup.key"
$pgBin = Join-Path $InstallRoot "infrastructure\pgsql\bin"
if ($Create) { $a = @("-m", "app.ops.backup", "create", "--out-dir", (Join-Path $DataRoot "backups"), "--key-file", $key, "--label", $Label) }
elseif ($Verify) { $a = @("-m", "app.ops.backup", "verify", "--file", $File, "--key-file", $key) }
elseif ($Restore) { if (-not $TargetDb) { throw "-TargetDb is required (a NEW database name)" }; $a = @("-m", "app.ops.backup", "restore", "--file", $File, "--key-file", $key, "--target-db", $TargetDb) }
else { throw "Use -Create, -Verify or -Restore" }
Invoke-Engine $py $a $backend $envFile $pgBin
