<#
    register_task.ps1  --  registers InputToggle's on-demand helper task.

    Run by the InputToggle installer, elevated, from the install folder
    (C:\Program Files\InputToggle), never from a temporary folder. It:
      1. refuses to run unless elevated,
      2. checks that the install folder and everything in it can be changed
         only by Administrators / SYSTEM / TrustedInstaller (owner and every
         Allow entry), with no junction or symbolic link, because the task
         runs InputToggleHelper.exe from there with full rights,
      3. registers \InputToggle\Apply for the account that runs this script
         (the account that runs Setup): Interactive logon, RunLevel Highest,
         no triggers (it runs only when InputToggle asks), allowed to start
         and keep running on battery power, 5-minute limit, and queued runs,
      4. verifies the result.
    It writes a short log to <install folder>\logs\setup.log.

    Exit codes: 0 ok, 1 error, 2 the install folder is not protected,
                3 not elevated.
#>
[CmdletBinding()]
param(
    [string]$InstallDir = $PSScriptRoot,
    [string]$TaskPath   = '\InputToggle\',
    [string]$TaskName   = 'Apply'
)

$ErrorActionPreference = 'Stop'

$logDir = Join-Path $InstallDir 'logs'
$logFile = Join-Path $logDir 'setup.log'

function Write-Log([string]$msg) {
    $line = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + ' register: ' + $msg
    Write-Output $line
    try {
        if (Test-Path -LiteralPath $logDir) {
            Add-Content -LiteralPath $logFile -Value $line -Encoding UTF8
        }
    } catch { }
}

function Stop-With([int]$code, [string]$msg) {
    Write-Log "ERROR: $msg"
    exit $code
}

# Only these may own or write to the folder the elevated task runs from.
$trustedSids = @(
    'S-1-5-32-544',                                                  # BUILTIN\Administrators
    'S-1-5-18',                                                      # NT AUTHORITY\SYSTEM
    'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464' # NT SERVICE\TrustedInstaller
)
# CREATOR OWNER resolves to whoever creates a child; only admins can create here.
$creatorOwnerSid = 'S-1-3-0'
# WriteData/CreateFiles, AppendData/CreateDirectories, WriteExtendedAttributes,
# DeleteSubdirectoriesAndFiles, WriteAttributes, Delete, WRITE_DAC, WRITE_OWNER,
# GENERIC_ALL, GENERIC_WRITE
$writeMask = 0x500D0156

function Get-UnsafeReason([string]$path) {
    # $null when only trusted principals can change $path, else why not.
    $item = Get-Item -LiteralPath $path -Force
    if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
        return 'a junction or symbolic link'
    }
    $acl = Get-Acl -LiteralPath $path
    $owner = $acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value
    if ($trustedSids -notcontains $owner) {
        return "owned by $($acl.Owner) ($owner)"
    }
    foreach ($rule in $acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier])) {
        if ($rule.AccessControlType -ne [System.Security.AccessControl.AccessControlType]::Allow) { continue }
        $sid = $rule.IdentityReference.Value
        if ($trustedSids -contains $sid -or $sid -eq $creatorOwnerSid) { continue }
        $rights = [int32]$rule.FileSystemRights
        if (($rights -band $writeMask) -ne 0) {
            return "writable by $sid ($($rule.FileSystemRights))"
        }
    }
    return $null
}

try {
    # 1. elevation ----------------------------------------------------------
    $id = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object System.Security.Principal.WindowsPrincipal($id)
    if (-not $principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Stop-With 3 'This script must run elevated (the installer runs it).'
    }
    $userName = $id.Name
    Write-Log "account: $userName"

    $helper = Join-Path $InstallDir 'InputToggleHelper.exe'
    if (-not (Test-Path -LiteralPath $helper)) { Stop-With 1 "helper not found: $helper" }

    # 2. the folder the task runs from must be admin-only ----------------------
    $items = @(Get-Item -LiteralPath $InstallDir -Force)
    $items += @(Get-ChildItem -LiteralPath $InstallDir -Recurse -Force)
    foreach ($it in $items) {
        $why = Get-UnsafeReason $it.FullName
        if ($why) {
            Stop-With 2 "$($it.FullName) is $why. The helper task was NOT registered, because non-administrators could change what it runs. Remove the folder (as administrator) and install again."
        }
    }
    Write-Log "install folder is protected ($($items.Count) items checked)"

    # 3. register the task ---------------------------------------------------
    # AllowStartIfOnBatteries + DontStopIfGoingOnBatteries: the Task Scheduler
    # defaults would block the task on a laptop running on battery, and on a
    # desktop whose UPS reports itself as a battery during a power blip.
    $action = New-ScheduledTaskAction -Execute $helper
    $taskPrincipal = New-ScheduledTaskPrincipal -UserId $userName -LogonType Interactive -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -MultipleInstances Queue

    $existing = Get-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Log "replacing the existing task $TaskPath$TaskName"
        Unregister-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -Confirm:$false
    }
    Register-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -Action $action `
        -Principal $taskPrincipal -Settings $settings `
        -Description 'InputToggle: apply a device enable/disable request (elevated, on demand).' | Out-Null

    # 4. verify --------------------------------------------------------------
    $t = Get-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -ErrorAction SilentlyContinue
    if (-not $t) { Stop-With 1 'the task could not be found after registering it' }
    $exec = @($t.Actions)[0].Execute
    if ($exec -ne $helper) { Stop-With 1 "the task runs '$exec' instead of '$helper'" }
    if ("$($t.Principal.RunLevel)" -ne 'Highest') { Stop-With 1 "unexpected RunLevel $($t.Principal.RunLevel)" }
    $triggers = @($t.Triggers | Where-Object { $null -ne $_ })
    if ($triggers.Count -ne 0) { Stop-With 1 'the task unexpectedly has triggers' }
    Write-Log "registered $TaskPath$TaskName for $userName -> $helper"
    exit 0
} catch {
    Stop-With 1 ("unexpected error: " + $_.Exception.Message)
}
