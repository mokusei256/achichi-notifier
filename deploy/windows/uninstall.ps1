<#
.SYNOPSIS
    Remove the achichi-notifier scheduled task. The app folder (venv, .env, cookies, log) is kept
    unless -Purge is given.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\uninstall.ps1

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\uninstall.ps1 -Purge
#>
[CmdletBinding()]
param(
    [string]$AppDir = (Join-Path $env:LOCALAPPDATA "achichi-notifier"),
    [string]$TaskName = "achichi-notifier",
    [string]$BrowserTaskName = "achichi-notifier-browser",
    [switch]$Purge
)

$ErrorActionPreference = "Stop"

if ($Purge) {
    # Refuse anything that is not clearly the app's own folder, before changing anything.
    # (One element per line: in PowerShell "," binds tighter than "+".)
    $resolved = [System.IO.Path]::GetFullPath($AppDir).TrimEnd("\")
    $forbidden = @(
        "$env:SystemDrive\"
        $env:USERPROFILE
        $env:LOCALAPPDATA
        $env:APPDATA
        (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
    ) | Where-Object { $_ } | ForEach-Object { [System.IO.Path]::GetFullPath($_).TrimEnd("\") }
    if ($forbidden -contains $resolved -or (Split-Path $resolved -Leaf) -ne "achichi-notifier") {
        Write-Host "ERROR: refusing to delete '$resolved' (not an achichi-notifier app folder)." -ForegroundColor Red
        exit 1
    }
    # The bot Firefox profile must be closed (Firefox keeps parent.lock open while running).
    $lock = Join-Path $AppDir "firefox-profile\parent.lock"
    if (Test-Path -LiteralPath $lock) {
        try {
            $fs = [System.IO.File]::Open($lock, "Open", "Read", "ReadWrite")
            $fs.Close()
        } catch [System.IO.IOException] {
            Write-Host "ERROR: the bot Firefox profile is open. Quit that Firefox window and run this again." -ForegroundColor Red
            exit 1
        }
    }
}

foreach ($name in @($TaskName, $BrowserTaskName)) {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        # Let a running check finish first (files in use cannot be deleted; a forced stop could
        # post a notification without saving that it was posted).
        Disable-ScheduledTask -TaskName $name | Out-Null
        $deadline = (Get-Date).AddMinutes(5)
        while ((Get-ScheduledTask -TaskName $name).State -eq "Running" -and (Get-Date) -lt $deadline) {
            Write-Host "   waiting for '$name' to finish..."
            Start-Sleep -Seconds 5
        }
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
        Write-Host "OK: scheduled task '$name' removed."
    }
}
$Shortcut = Join-Path ([Environment]::GetFolderPath("Programs")) "achichi-notifier YouTube login.lnk"
if (Test-Path -LiteralPath $Shortcut) {
    Remove-Item -LiteralPath $Shortcut -Force
    Write-Host "OK: start menu shortcut removed."
}

if ($Purge) {
    if (Test-Path -LiteralPath $AppDir) {
        Remove-Item -LiteralPath $AppDir -Recurse -Force
        Write-Host "OK: deleted $AppDir (including the cookie copy and the bot Firefox profile)."
    }
} else {
    Write-Host "The app folder was kept: $AppDir"
    Write-Host "To delete it as well (cookie copy and bot Firefox profile included), run this script with -Purge."
}
Write-Host "Deleting files does not end the login on Google's side. Sign that session out in your Google Account:"
Write-Host "  Security > Your devices."
