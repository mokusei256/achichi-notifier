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
}

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    # Let a running check finish first (files in use cannot be deleted; a forced stop could
    # post a notification without saving that it was posted).
    Disable-ScheduledTask -TaskName $TaskName | Out-Null
    $deadline = (Get-Date).AddMinutes(5)
    while ((Get-ScheduledTask -TaskName $TaskName).State -eq "Running" -and (Get-Date) -lt $deadline) {
        Write-Host "   waiting for the running check to finish..."
        Start-Sleep -Seconds 5
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "OK: scheduled task '$TaskName' removed."
} else {
    Write-Host "No scheduled task named '$TaskName' was found."
}

if ($Purge) {
    if (Test-Path -LiteralPath $AppDir) {
        Remove-Item -LiteralPath $AppDir -Recurse -Force
        Write-Host "OK: deleted $AppDir (including the cookie copy)."
    }
} else {
    Write-Host "The app folder was kept: $AppDir"
    Write-Host "To delete it as well (including the cookie copy), run this script with -Purge."
}
Write-Host "If the cookie may have leaked, sign that session out in your Google Account:"
Write-Host "  Security > Your devices."
