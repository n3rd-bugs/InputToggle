<#
    unregister_task.ps1  --  uninstall clean-up for InputToggle.

    Run by the uninstaller, elevated, from the install folder, AFTER it asked
    a running InputToggle to exit (which re-enables what it switched off while
    the task still exists). It:
      1. re-enables Mouse/Keyboard device nodes that the user's state.json
         still lists (disabled, auto_disabled and pending) and that are still
         switched off, so uninstalling never leaves input switched off.
         state.json is user-writable, so it is treated as untrusted input: no
         junctions or symbolic links, at most 1 MB, at most 64 well-formed
         instance ids, Mouse/Keyboard class only, and only devices that really
         show problem code 22 (disabled). Its content is never echoed,
      2. unregisters \InputToggle\Apply (and the \InputToggle\ task folder if
         it is then empty).
    The uninstaller itself removes the program files, the logs and the logon
    entry. The user's settings in %LOCALAPPDATA%\InputToggle are kept.

    Exit codes: 0 ok, 1 something could not be done (details in the output),
                3 not elevated.
#>
[CmdletBinding()]
param(
    [string]$StatePath = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'InputToggle\state.json'),
    [string]$TaskPath  = '\InputToggle\',
    [string]$TaskName  = 'Apply'
)

$ErrorActionPreference = 'Stop'
$failed = $false

function Say([string]$msg) {
    Write-Output ((Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + ' unregister: ' + $msg)
}

$id = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object System.Security.Principal.WindowsPrincipal($id)
if (-not $principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Say 'ERROR: this script must run elevated (the uninstaller runs it).'
    exit 3
}

$inputClasses = @('{4d36e96f-e325-11ce-bfc1-08002be10318}',   # Mouse
                  '{4d36e96b-e325-11ce-bfc1-08002be10318}')   # Keyboard
$idPattern = '^[A-Za-z0-9\\&_.#{}+,-]{1,400}$'

# 1. re-enable anything InputToggle left switched off ------------------------
if (Test-Path -LiteralPath $StatePath) {
    $state = $null
    try {
        $stateDir = Split-Path -Parent $StatePath
        foreach ($p in @($stateDir, $StatePath)) {
            if ((Get-Item -LiteralPath $p -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
                throw 'reparse'
            }
        }
        if ((Get-Item -LiteralPath $StatePath -Force).Length -gt 1MB) { throw 'size' }
        $state = Get-Content -Raw -LiteralPath $StatePath | ConvertFrom-Json
    } catch {
        # deliberately generic: never repeat what the (untrusted) file contains
        Say 'state.json was not used (a link, larger than 1 MB, or not valid JSON).'
        $state = $null
    }
    if ($state) {
        $ids = @()
        foreach ($k in @('disabled', 'auto_disabled', 'pending')) {
            $v = $state.$k
            if ($v) { $ids += @($v) }
        }
        $ids = @($ids | Where-Object { $_ -is [string] } | Select-Object -Unique | Select-Object -First 64)
        foreach ($devId in $ids) {
            if ($devId -notmatch $idPattern -or $devId -notlike '*\*') {
                Say 'skipped a malformed entry in state.json'
                continue
            }
            try {
                $dev = Get-PnpDevice -InstanceId $devId -ErrorAction SilentlyContinue
                if (-not $dev) { continue }
                $classGuid = ("$($dev.ClassGuid)").ToLowerInvariant()
                if ($inputClasses -notcontains $classGuid) {
                    Say "skipped $devId (not a Mouse/Keyboard device)"
                    continue
                }
                $disabled = ($dev.ConfigManagerErrorCode -eq 22) -or ("$($dev.ConfigManagerErrorCode)" -eq 'CM_PROB_DISABLED')
                if ($disabled) {
                    Say "re-enabling $devId"
                    Enable-PnpDevice -InstanceId $devId -Confirm:$false -ErrorAction Stop
                }
            } catch {
                Say "could not re-enable $devId"
                $failed = $true
            }
        }
    }
} else {
    Say 'no state.json (nothing to re-enable)'
}

# 2. unregister the task ------------------------------------------------------
try {
    $existing = Get-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -Confirm:$false
        Say "unregistered $TaskPath$TaskName"
        try {
            $leftover = Get-ScheduledTask -TaskPath $TaskPath -ErrorAction SilentlyContinue
            if (-not $leftover) {
                $svc = New-Object -ComObject 'Schedule.Service'
                $svc.Connect()
                $svc.GetFolder('\').DeleteFolder($TaskPath.Trim('\'), 0)
            }
        } catch {
            Say "left the $TaskPath task folder in place"
        }
    } else {
        Say 'task not present'
    }
} catch {
    Say ('could not unregister the task: ' + $_.Exception.Message)
    $failed = $true
}

if ($failed) { exit 1 }
exit 0
