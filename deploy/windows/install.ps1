<#
.SYNOPSIS
    Install achichi-notifier on this Windows PC and run it every few minutes with Task Scheduler.

.DESCRIPTION
    The app is copied into %LOCALAPPDATA%\achichi-notifier (virtual env, .env and an optional
    cookies.txt) and a per-user scheduled task runs it with pythonw.exe, so no console window
    pops up. Logs go to %LOCALAPPDATA%\achichi-notifier\notifier.log (rotated at 1 MB).
    Re-run this script after updating the code (git pull), .env or cookies.txt.

    This file is ASCII only on purpose: Windows PowerShell 5.1 misreads non-ASCII characters in
    scripts saved without a BOM.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\install.ps1

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\install.ps1 -Role fallback -IntervalMinutes 3
#>
[CmdletBinding()]
param(
    [ValidateSet("primary", "fallback")]
    [string]$Role = "primary",

    [ValidateRange(1, 60)]
    [int]$IntervalMinutes = 5,

    # Path to an exported cookies.txt (default: cookies.txt in the repository folder).
    [string]$Cookies = "",

    [string]$AppDir = (Join-Path $env:LOCALAPPDATA "achichi-notifier"),

    [string]$TaskName = "achichi-notifier",

    # Rebuild the Python virtual environment from scratch (normally it is reused).
    [switch]$Recreate
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

function Fail([string]$Message) {
    Write-Host "ERROR: $Message" -ForegroundColor Red
    exit 1
}

function Write-Utf8NoBom([string]$Path, [string[]]$Lines) {
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines($Path, $Lines, $encoding)
}

function Wait-TaskIdle([string]$Name) {
    # Pause the scheduled task and let a running check finish (files in use cannot be replaced).
    $task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if (-not $task) { return }
    Disable-ScheduledTask -TaskName $Name | Out-Null
    $deadline = (Get-Date).AddMinutes(5)
    while ((Get-ScheduledTask -TaskName $Name).State -eq "Running" -and (Get-Date) -lt $deadline) {
        Write-Host "   waiting for the running check to finish..."
        Start-Sleep -Seconds 5
    }
}

function Restrict-ToCurrentUser([string]$Path) {
    # %LOCALAPPDATA% is already private to the user; this also drops inherited entries.
    $account = "$env:USERDOMAIN\$env:USERNAME"
    & icacls.exe $Path /inheritance:r /grant:r "${account}:F" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "WARNING: could not restrict permissions on $Path" -ForegroundColor Yellow
    }
}

$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Fail "uv was not found. Install it with:  winget install --id=astral-sh.uv -e   then open a NEW PowerShell window and run this script again."
}
$RepoEnv = Join-Path $Repo ".env"
if (-not (Test-Path -LiteralPath $RepoEnv)) {
    Fail ".env was not found in $Repo. Copy .env.example to .env and fill it in first."
}

# ---------------------------------------------------------------- cookies (validated first)
$CookieSource = $null
if ($Cookies) {
    if (-not (Test-Path -LiteralPath $Cookies)) { Fail "Cookie file not found: $Cookies" }
    $CookieSource = (Resolve-Path -LiteralPath $Cookies).Path
} elseif (Test-Path -LiteralPath (Join-Path $Repo "cookies.txt")) {
    $CookieSource = Join-Path $Repo "cookies.txt"
}
if ($CookieSource) {
    $cookieText = [System.IO.File]::ReadAllText($CookieSource)
    if ($cookieText -notmatch "youtube\.com\t") {
        Fail "$CookieSource does not look like a Netscape cookies.txt with youtube.com lines. Nothing was changed."
    }
}
Get-ChildItem -LiteralPath $Repo -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match "cookies" -and $_.Name -ne "cookies.txt" -and $_.Extension -in ".txt", ".json" } |
    ForEach-Object { Write-Host "WARNING: extra cookie file in the repository folder: $($_.Name) (delete it when done)" -ForegroundColor Yellow }

# ---------------------------------------------------------------- app + virtual env
Write-Host "-> Installing into $AppDir"
New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
Wait-TaskIdle $TaskName   # re-enabled when the task is registered again below
$Venv = Join-Path $AppDir "venv"
if ($Recreate -or -not (Test-Path -LiteralPath (Join-Path $Venv "pyvenv.cfg"))) {
    # Only create the venv when needed: rewriting the launchers of an existing venv fails
    # while Windows has them open.
    $mode = "--allow-existing"
    if ($Recreate) { $mode = "--clear" }
    & uv venv --quiet $mode --python 3.12 $Venv
    if ($LASTEXITCODE -ne 0) { Fail "uv venv failed (the scheduled task is paused; run this script again)" }
}
$Python = Join-Path $Venv "Scripts\python.exe"
$PythonW = Join-Path $Venv "Scripts\pythonw.exe"
& uv pip install --quiet --python $Python --reinstall-package achichi-notifier $Repo
if ($LASTEXITCODE -ne 0) { Fail "uv pip install failed (the scheduled task is paused; run this script again)" }

# ---------------------------------------------------------------- cookies + .env
$AppCookies = Join-Path $AppDir "cookies.txt"
if ($CookieSource) {
    if ((Resolve-Path -LiteralPath $CookieSource).Path -ne $AppCookies) {
        Copy-Item -LiteralPath $CookieSource -Destination $AppCookies -Force
    }
    Restrict-ToCurrentUser $AppCookies
    Write-Host "-> Copied the cookie file to $AppCookies (members-only post checks enabled)."
    Write-Host "   The source file is no longer needed and can be deleted: $CookieSource"
} elseif (Test-Path -LiteralPath $AppCookies) {
    Write-Host "-> Keeping the existing cookie file $AppCookies."
    Write-Host "   To stop using cookies: delete it and run this script again."
}

# @(...) keeps this an array even when .env has a single line (+= would concatenate strings).
$envLines = @(Get-Content -LiteralPath $RepoEnv -Encoding UTF8 |
    Where-Object { $_ -notmatch "^\s*(export\s+)?YOUTUBE_COOKIES_FILE\s*=" })
if (Test-Path -LiteralPath $AppCookies) {
    $envLines += "YOUTUBE_COOKIES_FILE=$AppCookies"
} elseif ($envLines -match "^\s*YOUTUBE_COOKIES_B64\s*=\s*\S") {
    Write-Host "WARNING: .env contains YOUTUBE_COOKIES_B64; a cookie FILE is recommended instead." -ForegroundColor Yellow
}
$AppEnv = Join-Path $AppDir ".env"
Write-Utf8NoBom $AppEnv $envLines
Restrict-ToCurrentUser $AppEnv

# ---------------------------------------------------------------- scheduled task
$LogFile = Join-Path $AppDir "notifier.log"
$Exe = $PythonW
if (-not (Test-Path -LiteralPath $PythonW)) {
    Write-Host "WARNING: pythonw.exe not found; using python.exe (a console window may flash)." -ForegroundColor Yellow
    $Exe = $Python
}
$Arguments = "-m achichi_notifier --env-file `"$AppEnv`" --log-file `"$LogFile`" run --role $Role"
$Account = "$env:USERDOMAIN\$env:USERNAME"

$Action = New-ScheduledTaskAction -Execute $Exe -Argument $Arguments -WorkingDirectory $AppDir
$Every = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$AtLogon = New-ScheduledTaskTrigger -AtLogOn -User $Account
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 4) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$Principal = New-ScheduledTaskPrincipal -UserId $Account -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger @($Every, $AtLogon) `
    -Settings $Settings -Principal $Principal -Force `
    -Description "achichi-notifier ($Role): YouTube -> Discord notifications every $IntervalMinutes min" | Out-Null

$registered = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $registered) { Fail "The scheduled task was not registered." }
$interval = $registered.Triggers[0].Repetition.Interval
Write-Host "OK: scheduled task '$TaskName' registered (role=$Role, every $IntervalMinutes min, repetition=$interval)."

Start-ScheduledTask -TaskName $TaskName
Write-Host ""
Write-Host "Started one run now. Next steps:"
Write-Host "  1) Check the setup:  & `"$Python`" -m achichi_notifier --env-file `"$AppEnv`" check"
Write-Host "  2) Watch the log:    Get-Content -Wait -Tail 20 `"$LogFile`""
Write-Host "  3) Keep this PC awake while plugged in (Settings > System > Power > Sleep: Never)."
Write-Host "     Scheduled runs do not happen while Windows is asleep."
