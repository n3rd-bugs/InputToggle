; InputToggle installer (Inno Setup 6).
;
; Built by build.ps1, which first creates build\app\InputToggle\ (the
; PyInstaller onedir app), build\helper\InputToggleHelper.exe and
; build\icons\app.ico, then runs:
;   ISCC.exe /DAppVersion=x.y.z /DSourceRoot=<repository> installer\InputToggle.iss
;
; What it does, in order:
;   install:   stop a running InputToggle (any version) the way its tray Exit
;              does, so it switches devices it disabled back on -> copy files
;              -> register the helper task \InputToggle\Apply for the installing
;              account (register_task.ps1, run from the protected install
;              folder) -> logon entry (HKCU Run) -> start InputToggle as the
;              original, non-elevated user.
;   uninstall: stop InputToggle (it re-enables devices while the task still
;              exists) -> unregister_task.ps1 (re-enables anything state.json
;              still lists, removes the task) -> remove files, logs and the
;              logon entry. Settings in %LOCALAPPDATA%\InputToggle are kept.
;
; The installing account must be an administrator (UAC admin-approval mode):
; the task and the logon entry are created for the account that runs Setup.

#ifndef AppVersion
  #error AppVersion is not defined. Build with build.ps1 (or pass /DAppVersion=x.y.z).
#endif
#ifndef SourceRoot
  #define SourceRoot AddBackslash(SourcePath) + ".."
#endif
; Releases need a current Inno Setup: 6.5-6.7 hardened the elevated Setup and
; Uninstall against temp-folder, junction and symlink redirection. build.ps1
; passes /DAllowOldInno=1 only for local test builds (-AllowOldInnoSetup).
#ifndef AllowOldInno
  #if Ver < EncodeVer(6,7,0)
    #error Inno Setup 6.7.0 or newer is required for a release build (build.ps1 -AllowOldInnoSetup makes a local test build).
  #endif
#endif

#define AppName "InputToggle"
#define AppExeName "InputToggle.exe"
#define AppPublisher "Usama Masood"
#define AppURL "https://github.com/n3rd-bugs/InputToggle"

[Setup]
; Never change AppId: it ties upgrades and the uninstaller together.
AppId={{3CC7F60E-A629-4EB9-B520-6C583DD5462F}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
AppCopyright=Copyright (c) 2026 {#AppPublisher}
VersionInfoVersion={#AppVersion}
VersionInfoProductVersion={#AppVersion}
VersionInfoDescription={#AppName} Setup
; The helper logs to C:\Program Files\InputToggle\logs and the app points
; users there, so the folder is fixed (see also PrepareToInstall).
DefaultDirName={autopf}\{#AppName}
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableWelcomePage=no
LicenseFile={#SourceRoot}\LICENSE
PrivilegesRequired=admin
#if Ver >= EncodeVer(6,3,0)
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
#else
; Inno Setup before 6.3 has no x64compatible; x64 is its equivalent here.
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
#endif
MinVersion=10.0
OutputDir={#SourceRoot}\dist
OutputBaseFilename={#AppName}-Setup-{#AppVersion}
SetupIconFile={#SourceRoot}\build\icons\app.ico
UninstallDisplayIcon={app}\app\{#AppExeName}
UninstallDisplayName={#AppName}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
; Restart Manager may close an InputToggle.exe that still uses the files, but
; must never restart it: it would come back elevated.
CloseApplications=yes
RestartApplications=no
; The logon entry, Start-menu shortcut and task belong to the installing
; account on purpose (see the header).
UsedUserAreasWarning=no
SetupLogging=yes

[Messages]
WelcomeLabel2=This will install [name/ver] on your computer.%n%nInputToggle is set up for the Windows account you are signed in with: Setup registers its helper task for this account and starts InputToggle when this account signs in. This account must be an administrator, and you approve one UAC prompt. Do not approve the installation with another account's password.%n%nIt is recommended that you close all other applications before continuing.
UninstalledAll=%1 was successfully removed from your computer.%n%nYour InputToggle settings and log in your profile (AppData\Local\InputToggle) were kept. Delete that folder if you no longer need them.
UninstalledMost=%1 uninstall complete.%n%nSome elements could not be removed. These can be removed manually.%n%nYour InputToggle settings and log in your profile (AppData\Local\InputToggle) were kept.

[Tasks]
Name: "desktoptoggle"; Description: "Create a desktop shortcut that switches the selected devices off and on"; Flags: unchecked

[InstallDelete]
; left over from the earlier script-based setup, which compiled the helper here
Type: files; Name: "{app}\helper.cs"
; the PyInstaller bundle's file list changes between versions: start clean
Type: filesandordirs; Name: "{app}\app"

[Dirs]
; the helper writes helper.log here, but only if the folder exists
Name: "{app}\logs"

[Files]
Source: "{#SourceRoot}\build\app\InputToggle\*"; DestDir: "{app}\app"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourceRoot}\build\helper\InputToggleHelper.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceRoot}\installer\register_task.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceRoot}\installer\unregister_task.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceRoot}\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion
Source: "{#SourceRoot}\build\THIRD-PARTY-NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceRoot}\README.md"; DestDir: "{app}"; Flags: ignoreversion

[Registry]
; replaces the entry of an earlier script-based copy (pythonw.exe + .pyw)
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "InputToggle"; ValueData: """{app}\app\{#AppExeName}"""; Flags: uninsdeletevalue

[Icons]
Name: "{userprograms}\{#AppName}"; Filename: "{app}\app\{#AppExeName}"; Comment: "Start InputToggle, or open its Settings when it is already running"
Name: "{userdesktop}\{#AppName} - switch devices"; Filename: "{app}\app\{#AppExeName}"; Parameters: "--toggle"; Comment: "Switch the selected input devices off or on"; Tasks: desktoptoggle

[Run]
Filename: "{app}\app\{#AppExeName}"; Description: "Start InputToggle"; Flags: postinstall nowait skipifsilent runasoriginaluser

[UninstallDelete]
Type: filesandordirs; Name: "{app}\logs"
Type: filesandordirs; Name: "{app}\app"
Type: dirifempty; Name: "{app}"

[Code]
const
  { Identical in every InputToggle version, so any running copy can be
    asked to exit: the hidden window's class, the single-instance mutex and
    MSG_REQUEST_QUIT (WM_APP + 9), which exits like the tray menu's Exit. }
  InstanceWindowClass = 'InputToggleWndCls';
  InstanceMutex = 'Local\InputToggle';
  MsgRequestQuit = $8009;
  StopTimeoutMs = 30000;

function InstanceRunning(): Boolean;
begin
  Result := (FindWindowByClassName(InstanceWindowClass) <> 0) or CheckForMutexes(InstanceMutex);
end;

{ Ask a running InputToggle to exit and wait until it is gone. With its
  default settings it switches the devices it disabled back on first, through
  the helper task, so this must happen before files or the task change. }
function StopRunningInstance(TimeoutMs: Integer): Boolean;
var
  Wnd: HWND;
  Waited: Integer;
  Posted: Boolean;
begin
  Result := True;
  if not InstanceRunning() then
    Exit;
  Log('InputToggle is running; asking it to exit.');
  Posted := False;
  Waited := 0;
  while InstanceRunning() and (Waited < TimeoutMs) do
  begin
    if not Posted then
    begin
      { it may still be starting: post as soon as its window exists }
      Wnd := FindWindowByClassName(InstanceWindowClass);
      if Wnd <> 0 then
        Posted := PostMessage(Wnd, MsgRequestQuit, 0, 0);
    end;
    Sleep(250);
    Waited := Waited + 250;
  end;
  Result := not InstanceRunning();
  if Result then
    Log(Format('InputToggle exited after about %d ms.', [Waited]))
  else
    Log('InputToggle did not exit in time.');
end;

function PowerShellExe(): String;
begin
  Result := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
end;

{ Runs one of the scripts installed in the protected install folder. }
function RunInstalledScript(const ScriptName, Params: String; var ResultCode: Integer): Boolean;
begin
  Result := Exec(PowerShellExe(),
    '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' +
      ExpandConstant('{app}\') + ScriptName + '" ' + Params,
    ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Expected: String;
begin
  Result := '';
  Expected := ExpandConstant('{commonpf64}\{#AppName}');
  if CompareText(RemoveBackslashUnlessRoot(ExpandConstant('{app}')), Expected) <> 0 then
  begin
    Result := 'InputToggle must be installed in ' + Expected + '. Run Setup without the /DIR option.';
    Exit;
  end;
  if not StopRunningInstance(StopTimeoutMs) then
    Result := 'InputToggle is still running and did not exit within 30 seconds. ' +
      'Exit it from its tray icon (right-click, Exit), then run Setup again.';
end;

procedure RegisterHelperTask();
var
  ResultCode: Integer;
  Ok: Boolean;
begin
  WizardForm.StatusLabel.Caption := 'Registering the InputToggle helper task...';
  ResultCode := -1;
  Ok := RunInstalledScript('register_task.ps1', '', ResultCode) and (ResultCode = 0);
  if Ok then
    Log('Helper task registered.')
  else
  begin
    Log(Format('register_task.ps1 failed with exit code %d.', [ResultCode]));
    SuppressibleMsgBox('Setup could not register the InputToggle helper task (exit code ' +
      IntToStr(ResultCode) + '). InputToggle will report "helper not installed" until this ' +
      'is fixed. Details: ' + ExpandConstant('{app}\logs\setup.log') + '.',
      mbError, MB_OK, IDOK);
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    RegisterHelperTask();
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    { 1. InputToggle first: it re-enables what it switched off, through the
         helper task, which therefore must still be registered }
    if not StopRunningInstance(StopTimeoutMs) then
      Log('InputToggle did not exit; the clean-up below still re-enables devices.');
    { 2. re-enable anything state.json still lists, then remove the task }
    ResultCode := -1;
    if RunInstalledScript('unregister_task.ps1',
        '-StatePath "' + ExpandConstant('{localappdata}\InputToggle\state.json') + '"',
        ResultCode) then
      Log(Format('unregister_task.ps1 exit code %d.', [ResultCode]))
    else
      Log('Could not run unregister_task.ps1.');
  end;
end;
