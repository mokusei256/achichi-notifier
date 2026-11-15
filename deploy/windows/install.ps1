<#
.SYNOPSIS
    Install achichi-notifier on this Windows PC and run it every few minutes with Task Scheduler.

.DESCRIPTION
    The app is copied into %LOCALAPPDATA%\achichi-notifier (virtual env, .env, optional cookies)
    and a per-user scheduled task runs it with pythonw.exe, so no console window pops up. Logs go
    to %LOCALAPPDATA%\achichi-notifier\notifier.log (rotated at 1 MB).
    Re-run this script after updating the code (git pull), .env or cookies.

    Members-only posts need the member account's YouTube cookies. Two ways:
      -FirefoxProfile   recommended: a dedicated "bot" Firefox profile you log into once; the bot
                        reads its cookies on every run and a daily task opens YouTube in it
                        headlessly so the browser keeps the session fresh (no more exports).
      cookies.txt       an exported file in the repository folder (or -Cookies <path>).

    This file is ASCII only on purpose: Windows PowerShell 5.1 misreads non-ASCII characters in
    scripts saved without a BOM.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\install.ps1 -FirefoxProfile

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\install.ps1 -FirefoxProfile -RunWithoutSignIn
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
    [switch]$Recreate,

    # Use (and create) the dedicated bot Firefox profile. Stays on for later runs.
    [switch]$FirefoxProfile,

    # Stop using the bot Firefox profile (the folder is kept).
    [switch]$NoFirefoxProfile,

    # firefox.exe to use (default: found automatically).
    [string]$FirefoxExe = "",

    # Time of the daily headless YouTube visit (a random delay of up to 1 hour is added).
    [string]$RefreshAt = "05:00",

    [string]$BrowserTaskName = "achichi-notifier-browser",

    # Run the tasks even when nobody is signed in (e.g. after a scheduled restart). Uses the
    # S4U logon type, which stores no password. Needs an administrator PowerShell. Once set,
    # later runs keep it (use -SignInOnly to go back).
    [switch]$RunWithoutSignIn,

    # Go back to running the tasks only while you are signed in.
    [switch]$SignInOnly
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
        Write-Host "   waiting for '$Name' to finish..."
        Start-Sleep -Seconds 5
    }
}

# The signed-in account as Windows knows it (e.g. DESKTOP-ABC\name). $env:USERDOMAIN is not
# reliable: in an SSH session it is "WORKGROUP", which maps to no account.
$Account = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

function Invoke-Icacls([string[]]$Arguments) {
    # Local "Continue": with "Stop", Windows PowerShell 5.1 turns a native command's stderr
    # into a terminating error.
    $ErrorActionPreference = "Continue"
    $out = & icacls.exe @Arguments 2>&1
    return @($LASTEXITCODE, "$out")
}

function Restrict-ToCurrentUser([string]$Path) {
    # %LOCALAPPDATA% is already private to the user; this also drops inherited entries.
    # /reset first: entries left explicit by an earlier run would otherwise survive.
    $r = Invoke-Icacls @($Path, "/reset")
    if ($r[0] -eq 0) { $r = Invoke-Icacls @($Path, "/inheritance:r", "/grant:r", "${Account}:F") }
    if ($r[0] -ne 0) { Write-Host "WARNING: could not restrict permissions on $Path ($($r[1]))" -ForegroundColor Yellow }
}

function Restrict-DirToCurrentUser([string]$Path) {
    # (OI)(CI): files Firefox creates later inherit the same owner-only access.
    $r = Invoke-Icacls @($Path, "/inheritance:r", "/grant:r", "${Account}:(OI)(CI)F")
    if ($r[0] -ne 0) { Write-Host "WARNING: could not restrict permissions on $Path ($($r[1]))" -ForegroundColor Yellow }
}

function Find-Firefox([string]$Explicit) {
    $candidates = @()
    if ($Explicit) { $candidates += $Explicit }
    foreach ($base in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if ($base) { $candidates += (Join-Path $base "Mozilla Firefox\firefox.exe") }
    }
    foreach ($hive in @("HKLM:", "HKCU:")) {
        $key = "$hive\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\firefox.exe"
        $item = Get-ItemProperty -LiteralPath $key -ErrorAction SilentlyContinue
        if ($item -and $item.PSObject.Properties.Name -contains "(default)") { $candidates += $item."(default)" }
    }
    foreach ($c in $candidates) {
        if ($c -and $c -notmatch "\\WindowsApps\\" -and (Test-Path -LiteralPath $c)) { return (Resolve-Path -LiteralPath $c).Path }
    }
    return $null
}

$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$OverSsh = [bool]($env:SSH_CONNECTION -or $env:SSH_CLIENT)

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Fail "uv was not found. Install it with:  winget install --id=astral-sh.uv -e   then open a NEW PowerShell window and run this script again."
}
$RepoEnv = Join-Path $Repo ".env"
if (-not (Test-Path -LiteralPath $RepoEnv)) {
    Fail ".env was not found in $Repo. Copy .env.example to .env and fill it in first."
}
if ($FirefoxProfile -and $NoFirefoxProfile) { Fail "Use either -FirefoxProfile or -NoFirefoxProfile, not both." }
if ($RefreshAt -notmatch "^\d{1,2}:\d{2}$") { Fail "-RefreshAt must look like 05:00" }

# ---------------------------------------------------------------- decide what to do (no changes yet)
$FfProfile = Join-Path $AppDir "firefox-profile"
$FfMarker = Join-Path $FfProfile "achichi-notifier-profile.txt"
$UseFirefox = ($FirefoxProfile -or (Test-Path -LiteralPath $FfMarker)) -and -not $NoFirefoxProfile
$FirefoxPath = $null
if ($UseFirefox) {
    $FirefoxPath = Find-Firefox $FirefoxExe
    if (-not $FirefoxPath) { Fail "Firefox was not found. Install Firefox (or pass -FirefoxExe <path>) and run this script again." }
}
if ($RunWithoutSignIn -and $SignInOnly) { Fail "Use either -RunWithoutSignIn or -SignInOnly, not both." }
# Keep the earlier choice: re-running the script (README, #kanri alerts) must not silently
# switch a "run without sign-in" task back to "only while signed in".
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$UseS4U = $RunWithoutSignIn -or ($existing -and "$($existing.Principal.LogonType)" -eq "S4U" -and -not $SignInOnly)
if ($UseS4U) {
    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $isAdmin) {
        Fail "The tasks run without sign-in (S4U), which needs an administrator PowerShell: right-click PowerShell > Run as administrator, then run this again (or add -SignInOnly to switch back)."
    }
}

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
Wait-TaskIdle $TaskName          # both are re-enabled when registered again below
Wait-TaskIdle $BrowserTaskName
$Venv = Join-Path $AppDir "venv"
if ($Recreate -or -not (Test-Path -LiteralPath (Join-Path $Venv "pyvenv.cfg"))) {
    # Only create the venv when needed: rewriting the launchers of an existing venv fails
    # while Windows has them open.
    $mode = "--allow-existing"
    if ($Recreate) { $mode = "--clear" }
    & uv venv --quiet $mode --python 3.12 $Venv
    if ($LASTEXITCODE -ne 0) { Fail "uv venv failed (the scheduled tasks are paused; run this script again)" }
}
$Python = Join-Path $Venv "Scripts\python.exe"
$PythonW = Join-Path $Venv "Scripts\pythonw.exe"
& uv pip install --quiet --python $Python --reinstall-package achichi-notifier $Repo
if ($LASTEXITCODE -ne 0) { Fail "uv pip install failed (the scheduled tasks are paused; run this script again)" }

# ---------------------------------------------------------------- bot Firefox profile
$Shortcut = Join-Path ([Environment]::GetFolderPath("Programs")) "achichi-notifier YouTube login.lnk"
if ($UseFirefox) {
    New-Item -ItemType Directory -Force -Path $FfProfile | Out-Null
    Restrict-DirToCurrentUser $FfProfile
    $AppTmp = Join-Path $AppDir "tmp"
    New-Item -ItemType Directory -Force -Path $AppTmp | Out-Null
    Restrict-DirToCurrentUser $AppTmp
    Write-Utf8NoBom $FfMarker @("achichi-notifier bot profile: used only to keep the member account logged in to YouTube. Do not browse with it.")
    $prefs = @(
        'user_pref("signon.rememberSignons", false);',
        'user_pref("browser.privatebrowsing.autostart", false);',
        'user_pref("privacy.sanitize.sanitizeOnShutdown", false);',
        'user_pref("privacy.purge_trackers.enabled", false);',
        'user_pref("privacy.bounceTrackingProtection.mode", 0);',
        'user_pref("browser.shell.checkDefaultBrowser", false);',
        'user_pref("browser.aboutwelcome.enabled", false);',
        'user_pref("browser.startup.homepage_override.mstone", "ignore");',
        'user_pref("startup.homepage_welcome_url", "");',
        'user_pref("startup.homepage_welcome_url.additional", "");',
        'user_pref("startup.homepage_override_url", "");',
        'user_pref("browser.startup.page", 0);',
        'user_pref("browser.sessionstore.resume_from_crash", false);',
        'user_pref("toolkit.startup.max_resumed_crashes", -1);',
        'user_pref("browser.tabs.crashReporting.sendReport", false);',
        'user_pref("media.autoplay.default", 5);',
        'user_pref("datareporting.policy.dataSubmissionEnabled", false);',
        'user_pref("datareporting.healthreport.uploadEnabled", false);',
        'user_pref("toolkit.telemetry.unified", false);',
        'user_pref("app.normandy.enabled", false);',
        'user_pref("app.shield.optoutstudies.enabled", false);',
        'user_pref("extensions.update.enabled", false);',
        'user_pref("places.history.enabled", false);',
        'user_pref("browser.formfill.enable", false);'
    )
    Write-Utf8NoBom (Join-Path $FfProfile "user.js") $prefs
    $wsh = New-Object -ComObject WScript.Shell
    $lnk = $wsh.CreateShortcut($Shortcut)
    $lnk.TargetPath = $FirefoxPath
    $lnk.Arguments = "--profile `"$FfProfile`" https://www.youtube.com/"
    $lnk.WorkingDirectory = Split-Path $FirefoxPath
    $lnk.Description = "achichi-notifier: open the bot Firefox profile to log in to YouTube"
    $lnk.Save()
    Write-Host "-> Bot Firefox profile: $FfProfile (start menu: 'achichi-notifier YouTube login')"
} elseif ($NoFirefoxProfile) {
    if (Test-Path -LiteralPath $FfMarker) { Remove-Item -LiteralPath $FfMarker -Force }
    if (Test-Path -LiteralPath $Shortcut) { Remove-Item -LiteralPath $Shortcut -Force }
    if (Get-ScheduledTask -TaskName $BrowserTaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $BrowserTaskName -Confirm:$false
    }
    Write-Host "-> The bot Firefox profile is no longer used (folder kept: $FfProfile)."
    Write-Host "   Before deleting it, sign that session out: Google Account > Security > Your devices."
}

# ---------------------------------------------------------------- cookies + .env
$AppCookies = Join-Path $AppDir "cookies.txt"
if ($CookieSource) {
    if ((Resolve-Path -LiteralPath $CookieSource).Path -ne $AppCookies) {
        Copy-Item -LiteralPath $CookieSource -Destination $AppCookies -Force
    }
    Restrict-ToCurrentUser $AppCookies
    Write-Host "-> Copied the cookie file to $AppCookies."
    Write-Host "   The source file is no longer needed and can be deleted: $CookieSource"
} elseif (Test-Path -LiteralPath $AppCookies) {
    Restrict-ToCurrentUser $AppCookies
}

# @(...) keeps this an array even when .env has a single line (+= would concatenate strings).
$envLines = @(Get-Content -LiteralPath $RepoEnv -Encoding UTF8 |
    Where-Object { $_ -notmatch "^\s*(export\s+)?(YOUTUBE_COOKIES_FILE|YOUTUBE_FIREFOX_PROFILE|FIREFOX_EXE)\s*=" })
if ($UseFirefox) {
    $envLines += "YOUTUBE_FIREFOX_PROFILE=$FfProfile"
    $envLines += "FIREFOX_EXE=$FirefoxPath"
    if (Test-Path -LiteralPath $AppCookies) {
        Write-Host "WARNING: cookies.txt is no longer used (the Firefox profile wins). After check succeeds, delete $AppCookies and sign that old session out (Google Account > Security > Your devices)." -ForegroundColor Yellow
    }
} elseif (Test-Path -LiteralPath $AppCookies) {
    $envLines += "YOUTUBE_COOKIES_FILE=$AppCookies"
    Write-Host "-> Using the cookie file $AppCookies for members-only posts."
} elseif ($envLines -match "^\s*YOUTUBE_COOKIES_B64\s*=\s*\S") {
    Write-Host "WARNING: .env contains YOUTUBE_COOKIES_B64; the bot Firefox profile (-FirefoxProfile) is recommended instead." -ForegroundColor Yellow
}
$AppEnv = Join-Path $AppDir ".env"
Write-Utf8NoBom $AppEnv $envLines
Restrict-ToCurrentUser $AppEnv

# ---------------------------------------------------------------- scheduled tasks
$Exe = $PythonW
if (-not (Test-Path -LiteralPath $PythonW)) {
    Write-Host "WARNING: pythonw.exe not found; using python.exe (a console window may flash)." -ForegroundColor Yellow
    $Exe = $Python
}
if ($UseS4U) {
    $Principal = New-ScheduledTaskPrincipal -UserId $Account -LogonType S4U -RunLevel Limited
} else {
    $Principal = New-ScheduledTaskPrincipal -UserId $Account -LogonType Interactive -RunLevel Limited
}

$LogFile = Join-Path $AppDir "notifier.log"
$Action = New-ScheduledTaskAction -Execute $Exe -WorkingDirectory $AppDir `
    -Argument "-m achichi_notifier --env-file `"$AppEnv`" --log-file `"$LogFile`" run --role $Role"
$Every = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$AtLogon = New-ScheduledTaskTrigger -AtLogOn -User $Account
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 4) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger @($Every, $AtLogon) `
    -Settings $Settings -Principal $Principal -Force `
    -Description "achichi-notifier ($Role): YouTube -> Discord notifications every $IntervalMinutes min" | Out-Null
$registered = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $registered) { Fail "The scheduled task was not registered." }
$interval = $registered.Triggers[0].Repetition.Interval
$logon = $registered.Principal.LogonType
Write-Host "OK: scheduled task '$TaskName' registered (role=$Role, every $IntervalMinutes min, repetition=$interval, logon=$logon)."

if ($UseFirefox) {
    $BLog = Join-Path $AppDir "browser-refresh.log"   # separate file: two writers cannot share a rotating log
    $BAction = New-ScheduledTaskAction -Execute $Exe -WorkingDirectory $AppDir `
        -Argument "-m achichi_notifier --env-file `"$AppEnv`" --log-file `"$BLog`" refresh-browser"
    $BDaily = New-ScheduledTaskTrigger -Daily -At $RefreshAt -RandomDelay (New-TimeSpan -Hours 1)
    $BSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 20) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $BrowserTaskName -Action $BAction -Trigger $BDaily `
        -Settings $BSettings -Principal $Principal -Force `
        -Description "achichi-notifier: daily headless YouTube visit in the bot Firefox profile" | Out-Null
    Write-Host "OK: scheduled task '$BrowserTaskName' registered (daily around $RefreshAt, logon=$logon)."
} elseif (Get-ScheduledTask -TaskName $BrowserTaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $BrowserTaskName -Confirm:$false
}

Start-ScheduledTask -TaskName $TaskName
Write-Host ""
Write-Host "Started one run now. Next steps:"
$step = 1
if ($UseFirefox -and -not (Test-Path -LiteralPath (Join-Path $FfProfile "cookies.sqlite"))) {
    if (-not $OverSsh) {
        Start-Process -FilePath $FirefoxPath -ArgumentList "--profile `"$FfProfile`" https://www.youtube.com/"
    }
    Write-Host "  $step) Open the start menu shortcut 'achichi-notifier YouTube login' (on this PC's screen)."
    Write-Host "     Log in to YouTube with the MEMBER account in that normal (not private) window, accept"
    Write-Host "     Firefox's terms if asked, do not save the password, check the channel shows Member perks,"
    Write-Host "     then quit Firefox from its menu (do not log out)."
    $step++
}
Write-Host "  $step) Check the setup:  & `"$Python`" -m achichi_notifier --env-file `"$AppEnv`" check"; $step++
if ($UseFirefox) {
    Write-Host "  $step) Try the daily refresh once:  Start-ScheduledTask -TaskName $BrowserTaskName"
    Write-Host "     then read $(Join-Path $AppDir 'browser-refresh.log')"; $step++
}
Write-Host "  $step) Watch the log:    Get-Content -Wait -Tail 20 `"$LogFile`""; $step++
Write-Host "  $step) Keep this PC awake while plugged in (Settings > System > Power > Sleep: Never)."
