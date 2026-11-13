<#
.SYNOPSIS
  RR Trading One - provision or upgrade the native Windows infrastructure (Phase 1).

.DESCRIPTION
  Idempotent. Safe to re-run for upgrades: existing data, secrets and accounts are kept, a verified encrypted
  backup is taken before migrations on an existing database, and migrations are additive (alembic upgrade only).

  Installs (pinned in components.json, SHA-256 or Authenticode verified):
    PostgreSQL 16 (service RRTradingOne-PostgreSQL, 127.0.0.1 only)
    Redis-compatible server (service RRTradingOne-Redis, 127.0.0.1 only, password)
    Python 3.12 + venv with the RR Trading Engine backend
    Windows services (WinSW): RRTradingOne-API (auto), RRTradingOne-Learner (auto),
                              RRTradingOne-Worker (MANUAL - stays STOPPED until explicitly started)
  Each engine service runs under its own virtual account (NT SERVICE\<name>) with least-privilege ACLs.

  Layout:
    C:\Program Files\RR Trading One\{backend, mt5-connector, infrastructure}
    C:\ProgramData\RR Trading One\{data, backups, logs, models, cache, secrets}   (secrets: ACL-restricted)

  The MT5 terminal and the RR MT5 Connector are NOT services: they run in the logged-in user session.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File provision.ps1
#>
[CmdletBinding()]
param(
    [string]$InstallRoot = "$env:ProgramFiles\RR Trading One",
    [string]$DataRoot = "$env:ProgramData\RR Trading One",
    [string]$SourceRoot = "",
    [string]$ComponentsFile = "",
    [int]$PgPort = 5432,
    [int]$RedisPort = 6379,
    [int]$ApiPort = 8000,
    [string]$AdminUser = "admin",
    [string]$AdminPassword = "",
    [switch]$CreateTestDatabase,
    [switch]$NoStart
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $SourceRoot) { $SourceRoot = (Resolve-Path (Join-Path $Here "..\..")).Path }
if (-not $ComponentsFile) { $ComponentsFile = Join-Path $Here "components.json" }
. (Join-Path $Here "common.ps1")

$Infra = Join-Path $InstallRoot "infrastructure"
$Dl = Join-Path $DataRoot "cache\downloads"
$Secrets = Join-Path $DataRoot "secrets"
$Logs = Join-Path $DataRoot "logs"
$EnvFile = Join-Path $Secrets "engine.env"
$Py = Join-Path $Infra "python\python.exe"
$VenvPy = Join-Path $Infra "venv\Scripts\python.exe"
$PgBin = Join-Path $Infra "pgsql\bin"
$PgData = Join-Path $DataRoot "data\postgresql"
$RedisDir = Join-Path $Infra "redis"
$RedisData = Join-Path $DataRoot "data\redis"
$Svc = Join-Path $Infra "services"
$EngineServices = @("RRTradingOne-API", "RRTradingOne-Learner", "RRTradingOne-Worker")

# ---------------------------------------------------------------------------------------------------
Step "Preflight checks"
Assert-Admin
if (-not [Environment]::Is64BitOperatingSystem) { throw "64-bit Windows is required." }
$os = Get-CimInstance Win32_OperatingSystem
if ([int]$os.BuildNumber -lt 17763) { throw "Windows 10 1809 / Windows Server 2019 or later is required (build $($os.BuildNumber))." }
$memGb = [math]::Round($os.TotalVisibleMemorySize / 1MB, 1)
$driveName = (Split-Path -Qualifier $DataRoot).TrimEnd(":")      # ProgramData is hidden; don't Get-Item it
$freeGb = [math]::Round((Get-PSDrive $driveName).Free / 1GB, 1)
Info "$($os.Caption) build $($os.BuildNumber); RAM $memGb GB; free disk on ${driveName}: $freeGb GB"
if ($memGb -lt 7.5) { Warn "Less than 8 GB RAM: the AI learner and MT5 may be slow." }
if ($freeGb -lt 10) { throw "At least 10 GB of free disk space is required." }
foreach ($p in @($PgPort, $RedisPort, $ApiPort)) {
    $owner = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($owner) {
        $proc = (Get-Process -Id $owner.OwningProcess -ErrorAction SilentlyContinue).Path
        if ($proc -and ($proc -notlike "$Infra*")) { throw "Port $p is already used by $proc. Choose another port parameter." }
    }
}

# ---------------------------------------------------------------------------------------------------
Step "Folders and permissions"
foreach ($d in @($InstallRoot, $Infra, $Svc, $DataRoot, $Dl, $Secrets, $Logs, (Join-Path $DataRoot "data"),
                 (Join-Path $DataRoot "backups"), (Join-Path $DataRoot "models"), (Join-Path $DataRoot "cache"),
                 $RedisData)) { New-Item -ItemType Directory -Force -Path $d | Out-Null }
# All user data: no inheritance, SYSTEM + Administrators only. Each service account is then granted only
# what it needs (PostgreSQL: its data dir; Redis: its data dir; engine services: engine.env read, logs/cache/models).
Protect-Folder $DataRoot

# ---------------------------------------------------------------------------------------------------
Step "Components (pinned, verified)"
$C = Get-Content $ComponentsFile -Raw | ConvertFrom-Json
$files = @{}
foreach ($name in @("vcredist", "python", "postgresql", "redis", "winsw")) {
    $files[$name] = Get-VerifiedComponent -Name $name -Spec $C.$name -DownloadDir $Dl
}

Step "Microsoft Visual C++ runtime"
$vc = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64" -ErrorAction SilentlyContinue
if (-not $vc -or $vc.Installed -ne 1) {
    Invoke-Installer $files.vcredist @("/install", "/quiet", "/norestart") -AllowedExit @(0, 1638, 3010)
} else { Info "already installed ($($vc.Version))" }

# ---------------------------------------------------------------------------------------------------
Step "Python $($C.python.version)"
if (-not (Test-Path $Py)) {
    # Official python.org NuGet package (plain zip): private copy, no registry entries, no clash with other Pythons
    $zip = Join-Path $Dl "python-$($C.python.version).zip"
    Copy-Item $files.python $zip -Force                       # Expand-Archive only accepts .zip on PowerShell 5.1
    $stage = Join-Path $Dl "python-stage"
    Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
    Expand-Archive $zip -DestinationPath $stage -Force
    Copy-Item -Recurse -Force (Join-Path $stage "tools") (Join-Path $Infra "python")
    Remove-Item -Recurse -Force $stage, $zip
}
$sig = Get-AuthenticodeSignature $Py
if ($sig.Status -ne "Valid" -or $sig.SignerCertificate.Subject -notmatch "Python Software Foundation") { throw "python.exe signature check failed ($($sig.Status))" }
Info "python.exe signed by Python Software Foundation"
Invoke-Checked $Py @("--version")

Step "Application files"
Stop-EngineServices $EngineServices
Copy-Tree (Join-Path $SourceRoot "backend") (Join-Path $InstallRoot "backend") @("tests", "__pycache__", ".pytest_cache", ".venv")
Copy-Tree (Join-Path $SourceRoot "mt5-connector") (Join-Path $InstallRoot "mt5-connector") @("tests", "__pycache__", ".pytest_cache")
Copy-Item (Join-Path $Here "*.ps1") $Infra -Force
Copy-Item $ComponentsFile (Join-Path $Infra "components.json") -Force

Step "Python environment"
if (-not (Test-Path $VenvPy)) { Invoke-Checked $Py @("-m", "venv", (Join-Path $Infra "venv")) }
Invoke-Checked $VenvPy @("-m", "pip", "install", "--disable-pip-version-check", "--no-input", "--upgrade", "pip")
Invoke-Checked $VenvPy @("-m", "pip", "install", "--disable-pip-version-check", "--no-input", "-r", (Join-Path $InstallRoot "backend\requirements.txt"))
Invoke-Checked $VenvPy @("-m", "compileall", "-q", (Join-Path $InstallRoot "backend\app")) -AllowedExit @(0, 1)

# ---------------------------------------------------------------------------------------------------
Step "Secrets"
$envVals = Read-EnvFile $EnvFile
function Keep([string]$k, [string]$generated) { if ($envVals.ContainsKey($k) -and $envVals[$k]) { return $envVals[$k] } return $generated }
$pgSuperPw = Keep "RRONE_PG_SUPERUSER_PASSWORD" (New-Secret 32)
$pgAppPw = Keep "RRONE_PG_APP_PASSWORD" (New-Secret 32)
$redisPw = Keep "RRONE_REDIS_PASSWORD" (New-Secret 32)
$jwt = Keep "RR_JWT_SECRET" (New-Secret 48)
$fernet = Keep "RR_ENCRYPTION_KEY" (New-FernetKey)
$bridge = Keep "RR_BRIDGE_SHARED_SECRET" (New-Secret 48)
$engine = [ordered]@{
    "RR_ENVIRONMENT" = "production"
    "RR_DATABASE_URL" = "postgresql+psycopg://rr_app:$pgAppPw@127.0.0.1:$PgPort/rr_trading"
    "RR_REDIS_URL" = "redis://:$redisPw@127.0.0.1:$RedisPort/0"
    "RR_JWT_SECRET" = $jwt
    "RR_ENCRYPTION_KEY" = $fernet
    "RR_BRIDGE_SHARED_SECRET" = $bridge
    "RR_BRIDGE_MODE" = "relay"
    "RR_COOKIE_SECURE" = "false"
    "RR_PUBLIC_HOST" = "127.0.0.1"
    "RR_CORS_ORIGINS" = "http://127.0.0.1:$ApiPort,http://localhost:$ApiPort,tauri://localhost,http://tauri.localhost"
    "RRONE_PG_SUPERUSER_PASSWORD" = $pgSuperPw
    "RRONE_PG_APP_PASSWORD" = $pgAppPw
    "RRONE_REDIS_PASSWORD" = $redisPw
}
foreach ($k in $envVals.Keys) { if (-not $engine.Contains($k)) { $engine[$k] = $envVals[$k] } }   # keep user additions (e.g. AI keys)
Write-EnvFile $EnvFile $engine
Info "engine.env written ($($engine.Count) settings; values never printed)"

# ---------------------------------------------------------------------------------------------------
Step "PostgreSQL $($C.postgresql.version)"
if (-not (Test-Path (Join-Path $PgBin "pg_ctl.exe"))) { Expand-Archive $files.postgresql -DestinationPath $Infra -Force }
$pgSvc = "RRTradingOne-PostgreSQL"
if (-not (Test-Path (Join-Path $PgData "PG_VERSION"))) {
    New-Item -ItemType Directory -Force -Path $PgData | Out-Null
    # initdb drops administrator rights (restricted token), so the installing user needs explicit, temporary
    # access to the empty data folder and a password file in its own temp folder.
    $me = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    Grant-Path $PgData "*$me" "(OI)(CI)F"
    $pwDir = Join-Path $env:TEMP ("rrone-" + [Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $pwDir | Out-Null
    $pwFile = Join-Path $pwDir "pw.txt"
    [IO.File]::WriteAllText($pwFile, $pgSuperPw)
    try {
        Invoke-Checked (Join-Path $PgBin "initdb.exe") @("-D", $PgData, "-U", "postgres", "--pwfile=$pwFile", "-A", "scram-sha-256", "-E", "UTF8", "--no-locale")
    } finally { Remove-Item -Recurse -Force $pwDir -ErrorAction SilentlyContinue }
    Add-Content (Join-Path $PgData "postgresql.conf") @"

# RR Trading One
listen_addresses = '127.0.0.1'
port = $PgPort
max_connections = 100
logging_collector = on
log_directory = 'log'
log_rotation_size = 20MB
"@
    Set-Content (Join-Path $PgData "pg_hba.conf") @"
# RR Trading One: loopback only, password (SCRAM) required
host    all    all    127.0.0.1/32    scram-sha-256
host    all    all    ::1/128         scram-sha-256
"@
}
Grant-Path $PgData "*S-1-5-20" "(OI)(CI)F"          # NETWORK SERVICE runs PostgreSQL
$me = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
Invoke-Checked "icacls.exe" @($PgData, "/remove:g", "*$me", "/T", "/C", "/Q") | Out-Null   # drop the temporary initdb grant
if (-not (Get-Service $pgSvc -ErrorAction SilentlyContinue)) {
    Invoke-Checked (Join-Path $PgBin "pg_ctl.exe") @("register", "-N", $pgSvc, "-U", "NT AUTHORITY\NetworkService", "-D", $PgData, "-S", "auto", "-w")
}
Start-Service $pgSvc
Wait-Until "PostgreSQL ready" { & (Join-Path $PgBin "pg_isready.exe") -h 127.0.0.1 -p $PgPort *> $null; $LASTEXITCODE -eq 0 } 90
$env:PGPASSWORD = $pgSuperPw
try {
    $psql = Join-Path $PgBin "psql.exe"
    $q = { param($sql) $o = & $psql -h 127.0.0.1 -p $PgPort -U postgres -d postgres -v ON_ERROR_STOP=1 -tAc $sql
           if ($LASTEXITCODE -ne 0) { throw "psql failed: $sql" }; $o }
    if (-not (& $q "SELECT 1 FROM pg_roles WHERE rolname='rr_app'")) {
        & $q "CREATE ROLE rr_app LOGIN CREATEDB PASSWORD '$pgAppPw'" | Out-Null
    } else { & $q "ALTER ROLE rr_app WITH LOGIN CREATEDB PASSWORD '$pgAppPw'" | Out-Null }
    foreach ($db in @("rr_trading") + $(if ($CreateTestDatabase) { @("rr_test") } else { @() })) {
        if (-not (& $q "SELECT 1 FROM pg_database WHERE datname='$db'")) { & $q "CREATE DATABASE $db OWNER rr_app ENCODING 'UTF8' TEMPLATE template0" | Out-Null; Info "database $db created" }
    }
} finally { Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue }

# ---------------------------------------------------------------------------------------------------
Step "Redis-compatible server $($C.redis.version)"
if (-not (Test-Path (Join-Path $RedisDir "redis-server.exe"))) { Expand-Archive $files.redis -DestinationPath $RedisDir -Force }
$redisConf = Join-Path $RedisData "redis.conf"
Set-Content -Encoding ascii $redisConf @"
bind 127.0.0.1
port $RedisPort
protected-mode yes
requirepass $redisPw
dir "$($RedisData -replace '\\','/')"
appendonly yes
maxmemory 512mb
maxmemory-policy noeviction
"@
$redisSvc = "RRTradingOne-Redis"
Install-WinSWService -WinSW $files.winsw -Dir $Svc -Id $redisSvc -Name "RR Trading One - Redis" `
    -Executable (Join-Path $RedisDir "redis-server.exe") -Arguments "`"$redisConf`"" -WorkingDir $RedisData `
    -StartMode "Automatic" -Account "NT AUTHORITY\LocalService" -Logs $Logs
Grant-Path $RedisData "*S-1-5-19" "(OI)(CI)M"
Grant-Path $Logs "*S-1-5-19" "(OI)(CI)M"
Grant-Path $InstallRoot "*S-1-5-19" "(OI)(CI)RX"
Restart-Service $redisSvc -Force
Wait-Until "Redis ready" { (Test-NetConnection 127.0.0.1 -Port $RedisPort -WarningAction SilentlyContinue).TcpTestSucceeded } 60

# ---------------------------------------------------------------------------------------------------
Step "Database migrations (additive only)"
$py = { param([string[]]$a) Invoke-Engine $VenvPy $a (Join-Path $InstallRoot "backend") $EnvFile $PgBin }
$keyFile = Join-Path $Secrets "backup.key"
if (-not (Test-Path $keyFile)) { & $py @("-m", "app.ops.backup", "keygen", "--key-file", $keyFile) | Out-Null; Info "backup key created (keep an offline copy!)" }
$current = (& $py @("-c", "from app.ops.backup import alembic_revision; from app.core.config import get_settings; print(alembic_revision(get_settings().database_url) or '')")) | Select-Object -Last 1
if ($current) {
    Info "existing database at revision $current - taking a verified backup before upgrading"
    $bk = & $py @("-m", "app.ops.backup", "create", "--out-dir", (Join-Path $DataRoot "backups"), "--key-file", $keyFile, "--label", "pre-upgrade") | Select-Object -Last 1
    $bkFile = ($bk | ConvertFrom-Json).file
    & $py @("-m", "app.ops.backup", "verify", "--file", $bkFile, "--key-file", $keyFile) | Out-Null
    Info "verified backup: $bkFile"
}
& $py @("-m", "alembic", "upgrade", "head") | Out-Null
& $py @("-m", "app.cli", "seed") | Out-Null
$hasAdmin = (& $py @("-m", "app.cli", "user-exists", "--username", $AdminUser)) | Select-Object -Last 1
if ($hasAdmin -ne "1") {
    $generated = $false
    if (-not $AdminPassword) { $AdminPassword = New-Secret 18; $generated = $true }
    $env:RR_ADMIN_PASSWORD = $AdminPassword
    try { & $py @("-m", "app.cli", "create-admin", "--username", $AdminUser) | Out-Null } finally { Remove-Item Env:\RR_ADMIN_PASSWORD }
    if ($generated) {
        $f = Join-Path $Secrets "initial-admin.txt"
        [IO.File]::WriteAllText($f, "username: $AdminUser`r`npassword: $AdminPassword`r`nChange it after the first sign-in, then delete this file.`r`n")
        Info "administrator '$AdminUser' created; one-time password saved to $f (Administrators only)"
    } else { Info "administrator '$AdminUser' created" }
}

# ---------------------------------------------------------------------------------------------------
Step "Engine services"
$winsw = $files.winsw
$common = @{ Python = $VenvPy; Backend = (Join-Path $InstallRoot "backend"); EnvFile = $EnvFile; Logs = $Logs
             PgBin = $PgBin; Cache = (Join-Path $DataRoot "cache") }
Install-EngineService -WinSW $winsw -Dir $Svc -Id "RRTradingOne-API" -Name "RR Trading One - API" -StartMode "Automatic" `
    -Arguments "-m uvicorn app.main:app --host 127.0.0.1 --port $ApiPort --proxy-headers --forwarded-allow-ips 127.0.0.1 --no-server-header" @common
Install-EngineService -WinSW $winsw -Dir $Svc -Id "RRTradingOne-Learner" -Name "RR Trading One - AI Learner" -StartMode "Automatic" `
    -Arguments "-m app.learning_engine.scheduler" @common
Install-EngineService -WinSW $winsw -Dir $Svc -Id "RRTradingOne-Worker" -Name "RR Trading One - Trading Worker" -StartMode "Manual" `
    -Arguments "-m app.workers.main" @common
foreach ($s in $EngineServices) {
    Grant-Path $EnvFile "NT SERVICE\$s" "R"
    Grant-Path $Logs "NT SERVICE\$s" "(OI)(CI)M"
    Grant-Path (Join-Path $DataRoot "cache") "NT SERVICE\$s" "(OI)(CI)M"
    Grant-Path (Join-Path $DataRoot "models") "NT SERVICE\$s" "(OI)(CI)M"
}

Step "Scheduled encrypted backup (daily 02:17, keep 14)"
$taskCmd = "`"$VenvPy`" -m app.ops.backup create --out-dir `"$(Join-Path $DataRoot 'backups')`" --key-file `"$keyFile`" --label scheduled --keep 14"
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c set RR_ENV_FILE=$EnvFile&& set RR_PG_BIN=$PgBin&& cd /d `"$(Join-Path $InstallRoot 'backend')`" && $taskCmd >> `"$(Join-Path $Logs 'backup.log')`" 2>&1"
$trigger = New-ScheduledTaskTrigger -Daily -At "02:17"
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName "RR Trading One - Backup" -Action $action -Trigger $trigger -Principal $principal -Force | Out-Null

# ---------------------------------------------------------------------------------------------------
if (-not $NoStart) {
    Step "Start (worker stays STOPPED)"
    Start-Service "RRTradingOne-API"
    Wait-Until "API health endpoint" { Test-ApiHealth $ApiPort } 120
    Start-Service "RRTradingOne-Learner"
    Wait-Until "Learner running" { (Get-Service "RRTradingOne-Learner").Status -eq "Running" } 60
}

$state = [ordered]@{ version = (Get-Content (Join-Path $InstallRoot "backend\app\__init__.py") -Raw) -replace '(?s).*__version__ = "([^"]+)".*', '$1'
    installed_at = (Get-Date).ToString("o"); install_root = $InstallRoot; data_root = $DataRoot
    ports = @{ postgresql = $PgPort; redis = $RedisPort; api = $ApiPort }
    components = @{ python = $C.python.version; postgresql = $C.postgresql.version; redis = $C.redis.version; winsw = $C.winsw.version } }
[IO.File]::WriteAllText((Join-Path $DataRoot "install-state.json"), ($state | ConvertTo-Json -Depth 4))
Step "Done"
& (Join-Path $Here "status.ps1") -DataRoot $DataRoot -ApiPort $ApiPort
