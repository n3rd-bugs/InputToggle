<#
.SYNOPSIS
    Builds InputToggle: tests, icons, the PyInstaller app, the helper, the
    installer, and SHA256SUMS.txt. The same script runs locally and in CI.

.DESCRIPTION
    Steps (it stops at the first failure, with a message saying what failed):
      1. read the version from src\inputtoggle.pyw (VERSION = "x.y.z"),
         optionally checked against -ExpectVersion (CI passes the tag)
      2. create or reuse .venv-build from a 64-bit CPython 3.11 (-Python,
         else $env:PYTHON, else 'py -3.11', else C:\Python311\python.exe),
         never with the system site-packages
      3. install the pinned, hash-checked tools from requirements-build.txt
      4. run the unit tests (tests\test_logic.py)
      5. draw the icons and write the exe version resource
      6. PyInstaller: onedir, windowed, with icon and version info, then
         build\THIRD-PARTY-NOTICES.txt from the bundled components' licenses
      7. compile the helper with the .NET Framework 4.x csc
      8. check the frozen exe (tests\test_frozen.py: --version, --selftest)
      9. compile the installer with Inno Setup's ISCC
     10. write dist\SHA256SUMS.txt
    Everything it writes stays inside the repository: .venv-build\, build\
    and dist\ (all git-ignored). It never elevates and never runs the
    installer or the app (only the read-only --version/--selftest checks).

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1
.EXAMPLE
    .\build.ps1 -Python C:\Python311\python.exe -Clean
#>
[CmdletBinding()]
param(
    # Python used to create .venv-build (path to python.exe, or 'py' for the launcher).
    [string]$Python = '',
    # Path to ISCC.exe; default: $env:ISCC, PATH, then the usual install folders.
    [string]$Iscc = '',
    # Fail unless the version in src\inputtoggle.pyw equals this (e.g. 1.2.0 or v1.2.0).
    [string]$ExpectVersion = '',
    # Remove build\ and recreate .venv-build first.
    [switch]$Clean,
    # Skip the unit tests and the frozen-exe checks (not recommended).
    [switch]$SkipTests,
    # Stop after the app and helper are built (no ISCC needed).
    [switch]$SkipInstaller,
    # Allow an Inno Setup older than 6.7.0 (local test builds only; releases
    # are built in CI with a current Inno Setup).
    [switch]$AllowOldInnoSetup,
    # Fail unless the Inno Setup found has exactly this version (CI pins it).
    [string]$ExpectInnoVersion = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$Root = $PSScriptRoot
$VenvDir = Join-Path $Root '.venv-build'
$BuildDir = Join-Path $Root 'build'
$DistDir = Join-Path $Root 'dist'
$AppName = 'InputToggle'
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Write-Step([string]$msg) {
    Write-Host ''
    Write-Host "==> $msg" -ForegroundColor Cyan
}

function Stop-Build([string]$msg) {
    Write-Host ''
    Write-Host "BUILD FAILED: $msg" -ForegroundColor Red
    exit 1
}

# Runs a native program; throws with a clear message when it fails. Native
# programs write progress to stderr, so stderr must not count as an error.
function Invoke-Native([string]$What, [string]$Exe, [string[]]$Arguments) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Exe @Arguments
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $old
    }
    if ($code -ne 0) { Stop-Build "$What failed (exit code $code)." }
}

# Runs a native program and returns (exit code, stdout as one string).
function Get-NativeOutput([string]$Exe, [string[]]$Arguments) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $Exe @Arguments 2>$null
        $code = $LASTEXITCODE
    } catch {
        $out = $null
        $code = -1
    } finally {
        $ErrorActionPreference = $old
    }
    return @($code, (@($out) -join "`n").Trim())
}

# Deletes a folder, but only one inside the repository.
function Remove-InsideRepo([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $full = [System.IO.Path]::GetFullPath($Path)
    $rootFull = [System.IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
    if (-not $full.StartsWith($rootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        Stop-Build "refusing to delete $full (outside the repository)"
    }
    Remove-Item -LiteralPath $full -Recurse -Force
}

# No double quotes in here: Windows PowerShell 5.1 does not escape them when it
# passes arguments to native programs.
$ProbeCode = 'import sys, struct, platform; print(''%d.%d %d %s'' % (sys.version_info[0], sys.version_info[1], struct.calcsize(''P'') * 8, platform.machine()))'
$WantProbe = '3.11 64 AMD64'

function Test-Interpreter([string[]]$Command) {
    $exe = $Command[0]
    $pre = @()
    if ($Command.Count -gt 1) { $pre = $Command[1..($Command.Count - 1)] }
    $r = Get-NativeOutput $exe ($pre + @('-I', '-c', $ProbeCode))
    return ($r[0] -eq 0 -and $r[1] -eq $WantProbe)
}

function Find-BasePython {
    $candidates = @()
    if ($Python) {
        if ($Python -eq 'py') { $candidates += ,@('py', '-3.11') } else { $candidates += ,@($Python) }
    } elseif ($env:PYTHON) {
        $candidates += ,@($env:PYTHON)
    } else {
        if (Get-Command py.exe -ErrorAction SilentlyContinue) { $candidates += ,@('py', '-3.11') }
        $candidates += ,@('C:\Python311\python.exe')
    }
    foreach ($c in $candidates) {
        if (Test-Interpreter $c) { return ,$c }
    }
    Stop-Build ("no 64-bit CPython 3.11 (x64) found (tried: " + (($candidates | ForEach-Object { $_ -join ' ' }) -join '; ') + "). Pass -Python <path to python.exe> or set `$env:PYTHON.")
}

function Find-Iscc {
    $list = @()
    if ($Iscc) { $list += $Iscc }
    if ($env:ISCC) { $list += $env:ISCC }
    foreach ($base in @(${env:ProgramFiles(x86)}, $env:ProgramFiles, (Join-Path $env:LOCALAPPDATA 'Programs'))) {
        if ($base) { $list += (Join-Path $base 'Inno Setup 6\ISCC.exe') }
    }
    # last: PATH (for example a package manager's shim)
    $cmd = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($cmd) { $list += $cmd.Source }
    foreach ($p in $list) {
        if ($p -and (Test-Path -LiteralPath $p)) { return (Resolve-Path -LiteralPath $p).Path }
    }
    Stop-Build 'ISCC.exe (Inno Setup 6) not found. Install Inno Setup 6, or pass -Iscc <path>, or set $env:ISCC.'
}

$started = Get-Date

# 1. version ------------------------------------------------------------------
Write-Step 'Version'
$src = Join-Path $Root 'src\inputtoggle.pyw'
if (-not (Test-Path -LiteralPath $src)) { Stop-Build "missing $src" }
$m = Select-String -LiteralPath $src -Pattern '^VERSION\s*=\s*"(\d+\.\d+\.\d+)"\s*$' | Select-Object -First 1
if (-not $m) { Stop-Build 'VERSION = "x.y.z" not found in src\inputtoggle.pyw' }
$Version = $m.Matches[0].Groups[1].Value
Write-Host "InputToggle $Version"
if ($ExpectVersion) {
    $want = $ExpectVersion.Trim()
    if ($want.StartsWith('v')) { $want = $want.Substring(1) }
    if ($want -ne $Version) { Stop-Build "version mismatch: expected $want (tag/parameter), but src\inputtoggle.pyw says $Version" }
    Write-Host "matches the expected version $want"
}

# 2. build venv ---------------------------------------------------------------
Write-Step 'Build environment (.venv-build)'
if ($Clean) {
    Remove-InsideRepo $BuildDir
    Remove-InsideRepo $VenvDir
}
$VenvPy = Join-Path $VenvDir 'Scripts\python.exe'
$reuse = $false
if (Test-Path -LiteralPath $VenvPy) {
    $cfg = Join-Path $VenvDir 'pyvenv.cfg'
    $isolated = (Test-Path -LiteralPath $cfg) -and
        -not (Select-String -LiteralPath $cfg -Pattern '^\s*include-system-site-packages\s*=\s*true' -Quiet)
    if ($isolated -and (Test-Interpreter @($VenvPy))) { $reuse = $true }
    else { Write-Host 'existing .venv-build is not a clean CPython 3.11 x64 venv; recreating it' }
}
if ($reuse) {
    Write-Host "reusing $VenvDir"
} else {
    Remove-InsideRepo $VenvDir
    $base = Find-BasePython
    Write-Host ("creating $VenvDir with: " + ($base -join ' '))
    $baseExe = $base[0]
    $basePre = @()
    if ($base.Count -gt 1) { $basePre = $base[1..($base.Count - 1)] }
    Invoke-Native 'creating .venv-build' $baseExe ($basePre + @('-I', '-m', 'venv', $VenvDir))
    if (-not (Test-Interpreter @($VenvPy))) { Stop-Build '.venv-build was created but is not CPython 3.11 x64' }
}

# 3. pinned tools ---------------------------------------------------------------
Write-Step 'Build tools (requirements-build.txt, hash-checked)'
New-Item -ItemType Directory -Force -Path $BuildDir | Out-Null
# pip's download cache stays inside the repository too (build\pip-cache)
Invoke-Native 'pip install' $VenvPy @('-I', '-m', 'pip', 'install', '--disable-pip-version-check',
    '--no-input', '--cache-dir', (Join-Path $BuildDir 'pip-cache'), '--require-hashes',
    '--only-binary', ':all:', '-r', (Join-Path $Root 'requirements-build.txt'))
$freeze = Get-NativeOutput $VenvPy @('-I', '-m', 'pip', 'freeze', '--disable-pip-version-check')
[System.IO.File]::WriteAllText((Join-Path $BuildDir 'pip-freeze.txt'), $freeze[1] + "`n", $Utf8NoBom)
Write-Host $freeze[1]

# 4. unit tests -----------------------------------------------------------------
if ($SkipTests) {
    Write-Step 'Unit tests: SKIPPED (-SkipTests)'
} else {
    Write-Step 'Unit tests (tests\test_logic.py)'
    Invoke-Native 'unit tests' $VenvPy @('-I', '-B', (Join-Path $Root 'tests\test_logic.py'))
}

# 5. icons + version resource -----------------------------------------------------
Write-Step 'Icons and version resource'
$IconDir = Join-Path $BuildDir 'icons'
Invoke-Native 'icon generation' $VenvPy @('-I', '-B', (Join-Path $Root 'tools\make_icons.py'), '--out', $IconDir)
$VersionFile = Join-Path $BuildDir 'version_info.txt'
Invoke-Native 'version resource' $VenvPy @('-I', '-B', (Join-Path $Root 'tools\make_version_info.py'),
    '--version', $Version, '--out', $VersionFile)

# 6. PyInstaller ------------------------------------------------------------------
Write-Step 'PyInstaller (onedir, windowed)'
$AppOut = Join-Path $BuildDir 'app'
Remove-InsideRepo $AppOut
$PyiWork = Join-Path $BuildDir 'pyinstaller'
# keep PyInstaller's cache inside the repository as well
$oldPyiConfig = $env:PYINSTALLER_CONFIG_DIR
$env:PYINSTALLER_CONFIG_DIR = Join-Path $BuildDir 'pyinstaller-cache'
# PyInstaller collects DLLs it finds through PATH: limit PATH to Windows itself
# so the bundle does not depend on whatever else this machine has installed
# (for example the api-ms-win-*.dll / ucrtbase.dll copies some JREs ship)
$oldPath = $env:PATH
$env:PATH = (Join-Path $env:SystemRoot 'System32') + ';' + $env:SystemRoot
try {
    Invoke-Native 'PyInstaller' $VenvPy @('-I', '-B', '-m', 'PyInstaller',
        '--noconfirm', '--clean', '--log-level', 'WARN',
        '--onedir', '--windowed', '--noupx',
        '--name', $AppName,
        '--icon', (Join-Path $IconDir 'app.ico'),
        '--version-file', $VersionFile,
        '--add-data', ((Join-Path $IconDir '*.ico') + ';icons'),
        # the icons are drawn at build time; the app does not need Pillow
        '--exclude-module', 'PIL',
        # nor OpenSSL: no network code; hashlib falls back to its built-in hashes
        '--exclude-module', 'ssl', '--exclude-module', '_ssl', '--exclude-module', '_hashlib',
        '--distpath', $AppOut,
        '--workpath', $PyiWork,
        '--specpath', $PyiWork,
        $src)
} finally {
    $env:PYINSTALLER_CONFIG_DIR = $oldPyiConfig
    $env:PATH = $oldPath
}
$FrozenExe = Join-Path $AppOut "$AppName\$AppName.exe"
if (-not (Test-Path -LiteralPath $FrozenExe)) { Stop-Build "PyInstaller did not produce $FrozenExe" }
$pilLeft = @(Get-ChildItem -LiteralPath (Join-Path $AppOut $AppName) -Recurse -Directory -Filter 'PIL' -ErrorAction SilentlyContinue)
if ($pilLeft.Count -gt 0) { Stop-Build 'Pillow ended up in the app bundle (it should be excluded)' }
$bundleFiles = @(Get-ChildItem -LiteralPath (Join-Path $AppOut $AppName) -Recurse -File -ErrorAction SilentlyContinue)
$ucrt = @($bundleFiles | Where-Object { $_.Name -like 'api-ms-win-*.dll' -or $_.Name -ieq 'ucrtbase.dll' })
if ($ucrt.Count -gt 0) {
    Stop-Build ('the app bundle picked up Universal CRT DLLs (Windows provides them): ' + (($ucrt | ForEach-Object { $_.Name }) -join ', '))
}
$ssl = @($bundleFiles | Where-Object { $_.Name -like 'libssl-*.dll' -or $_.Name -like 'libcrypto-*.dll' -or $_.Name -like '_ssl*.pyd' -or $_.Name -like '_hashlib*.pyd' })
if ($ssl.Count -gt 0) {
    Stop-Build ('OpenSSL ended up in the app bundle (it should be excluded): ' + (($ssl | ForEach-Object { $_.Name }) -join ', '))
}
foreach ($n in @('icon_on.ico', 'icon_off.ico', 'app.ico')) {
    $hit = @(Get-ChildItem -LiteralPath (Join-Path $AppOut $AppName) -Recurse -File -Filter $n -ErrorAction SilentlyContinue)
    if ($hit.Count -eq 0) { Stop-Build "the app bundle is missing icons\$n" }
}

# 6b. third-party notices ---------------------------------------------------------
# The app folder ships the CPython runtime, the libraries of its Windows build,
# Tcl/Tk and the PyInstaller bootloader; their licenses travel with the installer.
Write-Step 'Third-party notices'
$bp = Get-NativeOutput $VenvPy @('-I', '-c', 'import sys; print(sys.base_prefix)')
if ($bp[0] -ne 0 -or -not $bp[1]) { Stop-Build 'could not read sys.base_prefix from .venv-build' }
$PyLicense = Join-Path $bp[1] 'LICENSE.txt'
$TkLicense = Join-Path $bp[1] 'tcl\tk8.6\license.terms'
foreach ($f in @($PyLicense, $TkLicense)) {
    if (-not (Test-Path -LiteralPath $f)) { Stop-Build "license source not found: $f" }
}
$PyiLicense = @(Get-ChildItem -LiteralPath (Join-Path $VenvDir 'Lib\site-packages') -Recurse -File -Filter 'COPYING.txt' -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -match '\\pyinstaller-[^\\]+\.dist-info\\' } | Select-Object -First 1)
if ($PyiLicense.Count -eq 0) { Stop-Build 'PyInstaller COPYING.txt not found in .venv-build' }
$MpdecLicense = @'
Copyright (c) 2008-2020 Stefan Krah. All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions
are met:

1. Redistributions of source code must retain the above copyright
   notice, this list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright
   notice, this list of conditions and the following disclaimer in the
   documentation and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE AUTHOR AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
ARE DISCLAIMED.  IN NO EVENT SHALL THE AUTHOR OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY
OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF
SUCH DAMAGE.
'@
$Utf8 = [System.Text.Encoding]::UTF8
$Rule = '=' * 78
$parts = @(
    "InputToggle $Version - third-party notices",
    '',
    'InputToggle itself is licensed under the MIT License (LICENSE.txt). Its',
    'application folder also contains third-party software, each under its own',
    'license:',
    '',
    '  - the CPython 3.11 runtime (python311.dll, the standard library and its',
    '    extension modules): Python Software Foundation License Version 2',
    '  - libraries of the CPython Windows build: libffi (MIT), bzip2, XZ Utils,',
    '    zlib and the Microsoft C runtime (VCRUNTIME140.dll)',
    '  - mpdecimal (libmpdec, inside _decimal.pyd): BSD 2-Clause',
    '  - Tcl/Tk 8.6 (tcl86t.dll, tk86t.dll and their script libraries)',
    '  - the PyInstaller bootloader: GPL-2.0 with the PyInstaller bootloader',
    '    exception, which allows distributing it with any program',
    '',
    'The full license texts follow.',
    '',
    $Rule, 'CPython 3.11, including the libraries of its Windows build', $Rule, '',
    [System.IO.File]::ReadAllText($PyLicense, $Utf8),
    $Rule, 'Tcl/Tk 8.6', $Rule, '',
    [System.IO.File]::ReadAllText($TkLicense, $Utf8),
    $Rule, 'mpdecimal (libmpdec)', $Rule, '',
    $MpdecLicense,
    $Rule, 'PyInstaller', $Rule, '',
    [System.IO.File]::ReadAllText($PyiLicense[0].FullName, $Utf8)
)
$Notices = Join-Path $BuildDir 'THIRD-PARTY-NOTICES.txt'
[System.IO.File]::WriteAllText($Notices, ((($parts -join "`n") -replace "`r`n", "`n") -replace "`n", "`r`n"), $Utf8NoBom)
Write-Host "wrote $Notices"

# 7. helper -------------------------------------------------------------------
Write-Step 'Helper (csc, C# 5, .NET Framework 4.x)'
$Csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $Csc)) { Stop-Build "csc.exe not found at $Csc (.NET Framework 4.x is part of Windows 10/11 and Windows Server)" }
$HelperDir = Join-Path $BuildDir 'helper'
New-Item -ItemType Directory -Force -Path $HelperDir | Out-Null
$AsmInfo = Join-Path $HelperDir 'AssemblyInfo.cs'
$asmText = @"
using System.Reflection;
[assembly: AssemblyTitle("InputToggle helper")]
[assembly: AssemblyDescription("Applies an InputToggle device request (run elevated by its scheduled task).")]
[assembly: AssemblyProduct("InputToggle")]
[assembly: AssemblyCopyright("Copyright (c) 2026 Usama Masood")]
[assembly: AssemblyVersion("$Version.0")]
[assembly: AssemblyFileVersion("$Version.0")]
[assembly: AssemblyInformationalVersion("$Version")]
"@
[System.IO.File]::WriteAllText($AsmInfo, $asmText, $Utf8NoBom)
$HelperExe = Join-Path $HelperDir 'InputToggleHelper.exe'
if (Test-Path -LiteralPath $HelperExe) { Remove-Item -LiteralPath $HelperExe -Force }
Invoke-Native 'helper compile' $Csc @('/nologo', '/target:winexe', '/optimize+', '/langversion:5',
    '/platform:anycpu', '/warnaserror+', ('/out:' + $HelperExe),
    (Join-Path $Root 'helper\InputToggleHelper.cs'), $AsmInfo)
if (-not (Test-Path -LiteralPath $HelperExe)) { Stop-Build 'csc did not produce InputToggleHelper.exe' }

# 8. frozen-exe checks ----------------------------------------------------------
if ($SkipTests) {
    Write-Step 'Frozen-exe checks: SKIPPED (-SkipTests)'
} else {
    Write-Step 'Frozen-exe checks (tests\test_frozen.py: --version and --selftest only)'
    $env:INPUTTOGGLE_FROZEN_EXE = $FrozenExe
    $env:INPUTTOGGLE_REQUIRE_FROZEN = '1'
    try {
        Invoke-Native 'frozen-exe checks' $VenvPy @('-I', '-B', (Join-Path $Root 'tests\test_frozen.py'))
    } finally {
        Remove-Item Env:\INPUTTOGGLE_FROZEN_EXE, Env:\INPUTTOGGLE_REQUIRE_FROZEN -ErrorAction SilentlyContinue
    }
}

# 9. installer ------------------------------------------------------------------
Remove-InsideRepo $DistDir
New-Item -ItemType Directory -Force -Path $DistDir | Out-Null
if ($SkipInstaller) {
    Write-Step 'Installer: SKIPPED (-SkipInstaller)'
} else {
    Write-Step 'Installer (Inno Setup)'
    $IsccExe = Find-Iscc
    $innoVer = 'version unknown'
    foreach ($k in @('HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1',
                     'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1',
                     'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1')) {
        $props = Get-ItemProperty -LiteralPath $k -ErrorAction SilentlyContinue
        if ($props -and $props.PSObject.Properties['DisplayVersion']) {
            $innoVer = 'Inno Setup ' + $props.DisplayVersion
            break
        }
    }
    Write-Host "ISCC: $IsccExe ($innoVer)"
    if ($ExpectInnoVersion -and $innoVer -ne "Inno Setup $ExpectInnoVersion") {
        Stop-Build "expected Inno Setup $ExpectInnoVersion, but found: $innoVer"
    }
    $isccArgs = @('/Qp', "/DAppVersion=$Version", "/DSourceRoot=$Root")
    if ($AllowOldInnoSetup) {
        Write-Host 'WARNING: -AllowOldInnoSetup: test build only, do not publish this installer' -ForegroundColor Yellow
        $isccArgs += '/DAllowOldInno=1'
    }
    Invoke-Native 'ISCC' $IsccExe ($isccArgs + @((Join-Path $Root 'installer\InputToggle.iss')))
    $Setup = Join-Path $DistDir "$AppName-Setup-$Version.exe"
    if (-not (Test-Path -LiteralPath $Setup)) { Stop-Build "ISCC did not produce $Setup" }
}

# 10. checksums -----------------------------------------------------------------
Write-Step 'SHA256SUMS.txt'
$files = @(Get-ChildItem -LiteralPath $DistDir -File | Where-Object { $_.Name -ne 'SHA256SUMS.txt' } | Sort-Object Name)
if ($files.Count -eq 0) {
    Write-Host 'nothing in dist\ to checksum'
} else {
    $lines = foreach ($f in $files) {
        $h = (Get-FileHash -LiteralPath $f.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        "$h  $($f.Name)"
    }
    # LF line endings, two spaces: the format 'sha256sum -c' reads
    [System.IO.File]::WriteAllText((Join-Path $DistDir 'SHA256SUMS.txt'), (($lines -join "`n") + "`n"), $Utf8NoBom)
    $lines | ForEach-Object { Write-Host $_ }
}

$elapsed = [int]((Get-Date) - $started).TotalSeconds
Write-Host ''
Write-Host "BUILD OK: InputToggle $Version in $elapsed s" -ForegroundColor Green
Write-Host "  app:       $FrozenExe"
Write-Host "  helper:    $HelperExe"
Get-ChildItem -LiteralPath $DistDir -File | ForEach-Object {
    Write-Host ("  dist:      {0} ({1:N0} bytes)" -f $_.FullName, $_.Length)
}
exit 0
