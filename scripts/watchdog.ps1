# watchdog.ps1 - checks Postgres, the dashboard, and the bot every time it
# runs, and (re)starts anything that isn't running. Scheduled to run every
# 5 minutes via a Windows Scheduled Task ("OTPSystemWatchdog") so recovery
# happens on its own instead of relying on someone noticing.

$ErrorActionPreference = "SilentlyContinue"

$root      = "C:\Users\hp\Documents\Projects\telegram-otp-system"
$pgBin     = "C:\Users\hp\pgsql16\pgsql\bin"
$pgData    = "C:\Users\hp\pgsql16\data"
$logDir    = "$root\scripts\logs"
$nodePath  = "C:\Program Files\nodejs"
$pyPath    = "C:\Users\hp\AppData\Local\Programs\Python\Python312"

New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$watchdogLog = "$logDir\watchdog.log"

function Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"
    Add-Content -Path $watchdogLog -Value $line
}

$env:Path = "$pgBin;$nodePath;$pyPath;$pyPath\Scripts;" + $env:Path

# ---------------------------------------------------------------------------
# Postgres
# ---------------------------------------------------------------------------
$pgReady = & "$pgBin\pg_isready.exe" -p 5433 2>$null
if ($LASTEXITCODE -ne 0) {
    Log "Postgres DOWN - restarting"
    & "$pgBin\pg_ctl.exe" -D $pgData -l "$root\scripts\logs\postgres.log" -o "-p 5433" start | Out-Null
    Start-Sleep -Seconds 3
    & "$pgBin\pg_isready.exe" -p 5433 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { Log "Postgres restarted OK" } else { Log "Postgres restart FAILED" }
} else {
    Log "Postgres OK"
}

# ---------------------------------------------------------------------------
# Dashboard (node server.js) - check the HTTP port actually answers, not
# just that some node.exe process exists.
# ---------------------------------------------------------------------------
$dashboardUp = $false
try {
    $resp = Invoke-WebRequest -Uri "http://localhost:3000/" -UseBasicParsing -TimeoutSec 5
    if ($resp.StatusCode -eq 200) { $dashboardUp = $true }
} catch {}

if (-not $dashboardUp) {
    Log "Dashboard DOWN - restarting"
    Get-Process node -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
    Start-Process -FilePath "$nodePath\node.exe" -ArgumentList "server.js" `
        -WorkingDirectory "$root\web-dashboard" `
        -RedirectStandardOutput "$logDir\dashboard.out.log" `
        -RedirectStandardError "$logDir\dashboard.err.log" `
        -WindowStyle Hidden
    Log "Dashboard restart issued"
} else {
    Log "Dashboard OK"
}

# ---------------------------------------------------------------------------
# Bot (python main.py) - match on command line, since other python.exe
# processes could be running for unrelated reasons.
# ---------------------------------------------------------------------------
$botProc = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*main.py*" }

if (-not $botProc) {
    Log "Bot DOWN - restarting"
    Start-Process -FilePath "$pyPath\python.exe" -ArgumentList "main.py" `
        -WorkingDirectory "$root\bot" `
        -RedirectStandardOutput "$logDir\bot.out.log" `
        -RedirectStandardError "$logDir\bot.err.log" `
        -WindowStyle Hidden
    Log "Bot restart issued"
} else {
    Log "Bot OK"
}
