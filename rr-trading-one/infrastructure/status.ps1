<#  RR Trading One - infrastructure status. A service is reported healthy only when its real check passes. #>
param([string]$DataRoot = "$env:ProgramData\RR Trading One", [int]$ApiPort = 8000)
$ErrorActionPreference = "Continue"
. (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "common.ps1")
$state = $null
$sf = Join-Path $DataRoot "install-state.json"
if (Test-Path $sf) { $state = Get-Content $sf -Raw | ConvertFrom-Json; $ApiPort = $state.ports.api }
$rows = @()
foreach ($n in @("RRTradingOne-PostgreSQL", "RRTradingOne-Redis", "RRTradingOne-API", "RRTradingOne-Learner", "RRTradingOne-Worker")) {
    $s = Get-Service $n -ErrorAction SilentlyContinue
    $acct = (Get-CimInstance Win32_Service -Filter "Name='$n'" -ErrorAction SilentlyContinue).StartName
    $check = "-"
    if ($s -and $s.Status -eq "Running") {
        switch ($n) {
            "RRTradingOne-PostgreSQL" { $check = if ((Test-NetConnection 127.0.0.1 -Port $state.ports.postgresql -WarningAction SilentlyContinue).TcpTestSucceeded) { "port open" } else { "NOT LISTENING" } }
            "RRTradingOne-Redis"      { $check = if ((Test-NetConnection 127.0.0.1 -Port $state.ports.redis -WarningAction SilentlyContinue).TcpTestSucceeded) { "port open" } else { "NOT LISTENING" } }
            "RRTradingOne-API"        { $check = if (Test-ApiHealth $ApiPort) { "health OK" } else { "HEALTH FAILING" } }
            default                   { $check = "running" }
        }
    }
    $rows += [pscustomobject]@{ Service = $n; State = $(if ($s) { $s.Status } else { "not installed" }); StartType = $(if ($s) { $s.StartType } else { "" }); Account = $acct; Check = $check }
}
$rows | Format-Table -AutoSize | Out-String | Write-Host
$last = Get-ChildItem (Join-Path $DataRoot "backups") -Filter *.rrbak -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
Write-Host ("Last backup: " + $(if ($last) { "$($last.Name) ($($last.LastWriteTime))" } else { "none yet" }))
if ($state) { Write-Host "Version $($state.version); data in $DataRoot" }
