# InputToggle

Switch selected input devices (a mouse, a keyboard) **off and on** with a
global hotkey or from the tray, on Windows 10 and 11.

## Why

Some mice report tiny phantom movements while nobody touches them: a worn
sensor, a vibrating desk, or a wireless receiver picking up noise. Chrome
Remote Desktop treats any local input as "someone is at the desk": it drops
remote input for about 2 seconds after every local event, and its *Stop
sharing* bar can take the focus. A remote session becomes unusable.

InputToggle lets you switch that device off while you are away and back on
when you return, with one hotkey (default **Ctrl+Alt+Shift+M**) or from its
tray icon. It can also do it automatically while a Chrome Remote Desktop
session is connected.

Switching a device off is **non-persistent**: a restart always brings every
device back, so you cannot lock yourself out for good.

## Install

1. Download `InputToggle-Setup-<version>.exe` from the
   [Releases page](https://github.com/n3rd-bugs/InputToggle/releases).
2. Optional: check it against `SHA256SUMS.txt` from the same release:

   ```powershell
   Get-FileHash .\InputToggle-Setup-1.2.1.exe -Algorithm SHA256
   ```

3. Run it from the Windows account that will use InputToggle. That account
   must be an **administrator** (the normal UAC setup where you approve a
   prompt with "Yes"). Setup asks for elevation once.

The binaries are not code-signed, so Windows SmartScreen may say "Windows
protected your PC". Choose *More info* > *Run anyway* if the checksum
matches, or build from source (below).

Setup:

- installs to `C:\Program Files\InputToggle` (the folder is fixed),
- registers the scheduled task `\InputToggle\Apply` **for the account that
  runs Setup**; the task runs the small helper that actually switches devices,
- adds a logon entry so InputToggle starts when that account signs in, and a
  Start-menu shortcut; optionally a desktop shortcut that switches the
  selected devices,
- stops a running InputToggle first (which switches its devices back on),
  so upgrading is safe,
- starts InputToggle at the end, as you (not elevated).

Do not approve the UAC prompt with a *different* account's password: the task
and the logon entry would then belong to that other account, and InputToggle
would report "helper not installed" for you.

## First run

Nothing is pre-selected. On the first start, InputToggle opens its Settings
window once: tick the device(s) to switch and click **Save**. After that it
runs in the tray and never opens a window by itself.

Windows 11 hides new tray icons under the **^** arrow. To keep InputToggle
visible: *Settings > Personalization > Taskbar > Other system tray icons* >
turn on **InputToggle**, or drag the icon from the ^ flyout onto the taskbar.

## Using it

- **Hotkey** (default `Ctrl+Alt+Shift+M`): if any selected device is on, it
  switches them all off; otherwise it switches them on. While a switch is in
  progress (1-8 s) further presses are ignored.
- **Tray icon**: green = on, red = off. The tooltip says what is on or off and
  whether the hotkey is active.
- **Right-click the tray icon** for the menu (it also works over Chrome Remote
  Desktop). The first, grayed line repeats the state for touch clients:
  - *Disable / Enable selected devices*: does exactly what the label says
  - *Re-enable everything InputToggle disabled*
  - *Settings...*
  - *Open log folder*
  - *Exit*: with *Re-enable on exit* on (the default), devices InputToggle
    switched off are turned back on first.
- **Double-click the tray icon** (or press Enter on it): opens Settings.
- Starting InputToggle again (Start menu) while it runs opens its Settings.
- **Notifications** say what happened and why something failed. They never
  take the focus.

### Where the hotkey works

The hotkey is registered with `RegisterHotKey`, so it works in every app on
the desktop, whether you press it at the desk or send it through a remote
session. It does **not** work on the lock screen, the sign-in screen, the
Ctrl+Alt+Del screen or a UAC prompt (the secure desktop). While InputToggle
runs, no other app receives that key combination.

If a remote session starts at the lock screen and phantom movement keeps
interrupting the password, use auto mode: it reacts even while the PC is
locked.

### Auto mode (off by default)

*Auto-disable selected devices while Chrome Remote Desktop is connected*
switches the selected devices off within about 2 s of a Chrome Remote Desktop
session starting (it watches for `remoting_desktop.exe`) - **even when you
are sitting at the desk** - and back on when the session ends, unless you
switched them yourself during the session. A session counts as ended only
after about 6 s without the process, so a short glitch does not flip the
devices back and forth.

### Settings

- **Hotkey**: tick Ctrl / Alt / Shift / Win and pick a key, or click
  *Record...* and press the combination (Esc cancels). At least one of
  Ctrl / Alt / Win is required unless the key is F13-F24 or Pause. Refused:
  Ctrl+Alt+Del, Ctrl+Alt+Esc, Win+L, Alt+F4 and Ctrl+A/C/V/X/Z/Y/S/F/W/T/N/P/R.
  A single modifier with a letter, digit or F1-F12 (e.g. Ctrl+M) takes that
  key away from every other app, so it needs a second Save to confirm. If the
  combination is taken by another program, InputToggle keeps the old one and
  says so.
- **Devices**: groups show the vendor and VID:PID, entries show their class
  and interface (*Mouse - MI_00*, *Keyboard - MI_01/COL01*), and the Status
  column shows ON / OFF / "2 of 4 OFF" / not connected. *Show disconnected*
  reveals devices that are not plugged in; *Show virtual/remote* reveals
  remote-session and virtual input devices (hidden by default: switching them
  off can cut remote access).
- **Confirmations** (shown in red above Save; click Save again to confirm):
  any Keyboard-class entry (many mice also expose Keyboard-class collections
  for their extra buttons), a stronger one when the selection covers every
  connected keyboard **and** mouse, and any virtual/remote device.
- If you untick a device that InputToggle switched off, Save turns it back on.
- **Options**: notifications, sound, re-enable on exit (also tried at
  sign-out) and at start, auto mode, start with Windows.

## If a device stays off

Ways back, most convenient first:

1. **Over Chrome Remote Desktop**: remote input does not go through the
   switched-off devices, so connect and choose tray > *Re-enable everything
   InputToggle disabled*. On the lock screen, sign in with the remote
   keyboard first.
2. **At the desk with a working mouse**: the same menu item (the icon may be
   under ^).
3. **Unplug the device's USB receiver (or cable) for 5 s and plug it back
   in.** Turning a wireless keyboard or mouse off and on does not help: the
   receiver is what was switched off.
4. **Plug in another USB keyboard or mouse**: devices you did not select are
   never switched off.
5. **Restart** (Start > Restart, or the power button). A restart always
   brings devices back. *Shut down* may not when Windows Fast Startup is on,
   because Fast Startup resumes the previous kernel session much like
   hibernation does; sleep and hibernation keep devices off too.

On the lock or sign-in screen neither the tray nor the hotkey is available:
use 1 (after signing in remotely), 3, 4 or 5.

## Command line

`InputToggle.exe <option>` (in `C:\Program Files\InputToggle\app`):

- `--toggle`: toggle once (asks the running instance)
- `--enable-all`: re-enable everything InputToggle disabled
- `--settings`: open Settings
- `--quit`: ask the running instance to exit cleanly (devices back on)
- `--status`: list the Mouse/Keyboard devices and their state (read-only)
- `--selftest`: read-only self checks
- `--version`: print the version

The release exe is a windowed program without a console, so add
`--out <file>` to `--status`, `--selftest` or `--version` to get their
output in a file, e.g.
`& "C:\Program Files\InputToggle\app\InputToggle.exe" --selftest --out "$env:TEMP\it.txt"`.
`--out` never starts the tray application.

The output of `--status` and `--selftest` contains your Windows user name and
device instance IDs, and some instance IDs include a device serial number.
Remove them before you post the output in a public issue.

## Uninstall

*Settings > Apps > Installed apps > InputToggle > Uninstall* (or *Add or
remove programs*). The uninstaller:

1. asks InputToggle to exit, so it switches its devices back on (while the
   helper task still exists),
2. re-enables any Mouse/Keyboard device that InputToggle's record
   (`state.json`) still lists as switched off,
3. removes the task, the program files, the helper's logs, the logon entry
   and the shortcuts.

Your settings and log in `%LOCALAPPDATA%\InputToggle` are **kept**; delete
that folder if you do not need them.

## How it works

| Part | Runs as | Does |
|------|---------|------|
| `InputToggle.exe` (tray app) | you, **not elevated** | tray icon, hotkey, Settings; *reads* device state; writes a request file; asks the task to run |
| `InputToggleHelper.exe` | **elevated**, only through the task `\InputToggle\Apply` | validates the request and enables/disables the named devices |

Changing a device's state needs administrator rights; reading it does not. So
only a tiny compiled helper (C#, .NET Framework 4.x, a few hundred lines) runs
elevated, and only when the on-demand task starts it. The task has no
triggers, is registered for the installing account with "run with highest
privileges", and runs the helper from `C:\Program Files\InputToggle`, which
only administrators can change. The tray app runs with your normal rights; if
it is started elevated (for example from an administrator prompt), it says so
and exits. (With UAC switched off, Windows gives every program of an
administrator full rights; InputToggle cannot change that.)

A toggle works like this: the tray app writes
`%LOCALAPPDATA%\InputToggle\request.txt` (action, time stamp, device
instance ids), records in `state.json` what it is about to switch off, runs
the task, polls the real device state until it changed (or times out), and
deletes the request. The helper disables with `CM_Disable_DevNode` without
`CM_DISABLE_PERSIST`, which is why a restart restores every device.

## Security model

What protects you:

- **Least privilege.** Only the helper is elevated, and only for the duration
  of one request. It touches nothing but **Mouse- and Keyboard-class** device
  nodes, and only non-persistently.
- **The request is untrusted input.** The request file lives in your profile,
  so the helper checks it strictly: at most 16 KB; no junction or symbolic
  link on the way (the opened file must resolve to exactly the expected
  path); a time stamp no older than 60 s (so an old request is never
  replayed); `action=disable|enable` only; at most 32 well-formed instance
  ids, each of which must be a present Mouse/Keyboard device. Rejected lines
  are logged by number and length only, never by content. The helper never
  writes to a location you can write to; its log is
  `C:\Program Files\InputToggle\logs\helper.log`.
- **Protected install folder.** Before registering the task, Setup checks
  that only Administrators, SYSTEM and TrustedInstaller can change the
  install folder; otherwise it does not register the task.
- **No hidden test doors.** The helper's test-only switches work only when it
  is *not* elevated and an explicit test environment variable is set.

Residual risks you accept by installing it:

- **Any program running as you can switch your input devices.** Running the
  task and writing the request file need no more rights than your own
  account has, so another program running as you could switch a Mouse or
  Keyboard device off or on (non-persistently: a restart or replug undoes
  it). It could also replace the request between InputToggle writing it and
  the helper reading it. It cannot use the helper for anything else, and a
  program running as you can already do far more than this.
- **Unsigned binaries.** Releases are not code-signed. Check the published
  `SHA256SUMS.txt`, or build the release yourself from source.
- **Administrator account required.** Setup and the task need an
  administrator account; the task belongs to the account that installed it.
- **You can lock yourself out at the desk.** Switching off the keyboard and
  mouse you use leaves only the ways listed under *If a device stays off*.
  Settings warns before saving such a selection.

To report a vulnerability, see [SECURITY.md](SECURITY.md).

## Files

| Where | What |
|-------|------|
| `C:\Program Files\InputToggle\app\` | the tray app (`InputToggle.exe` and its runtime) |
| `C:\Program Files\InputToggle\InputToggleHelper.exe` | the elevated helper |
| `C:\Program Files\InputToggle\logs\` | `helper.log`, `setup.log` |
| `%LOCALAPPDATA%\InputToggle\` | `settings.json`, `state.json` (what InputToggle switched off), `request.txt` while a switch runs, `inputtoggle.log` (rotating 1 MB x3) |
| Task Scheduler `\InputToggle\Apply` | runs the helper elevated, on demand |
| `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\InputToggle` | the logon entry |

## Build from source

Requirements (Windows x64): Python 3.11 (64-bit), Inno Setup 6.7 or newer,
and the .NET Framework 4.x C# compiler that ships with Windows. Then:

```powershell
git clone https://github.com/n3rd-bugs/InputToggle.git
cd InputToggle
powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1
```

`build.ps1` creates `.venv-build` (never using your global site-packages),
installs the hash-pinned tools from `requirements-build.txt`, runs the unit
tests, draws the icons, builds the app with PyInstaller, compiles the helper,
checks the built exe with its read-only modes, compiles the installer and
writes `dist\SHA256SUMS.txt`. Options: `-Python <python.exe>`,
`-Iscc <ISCC.exe>`, `-Clean`, `-SkipInstaller`, `-SkipTests`, and
`-AllowOldInnoSetup` (a local test build with an Inno Setup older than 6.7;
never publish that installer). Nothing is installed and nothing needs
elevation.

Output: `dist\InputToggle-Setup-<version>.exe` and `dist\SHA256SUMS.txt`.

Tests:

- `tests\test_logic.py`: unit tests (no hardware, no tray, no task needed)
- `tests\test_frozen.py`: runs the built exe with `--version` and
  `--selftest` only
- `tests\test_smoke.py`: needs a real desktop; skipped unless
  `INPUTTOGGLE_LOCAL_SMOKE=1` (the hotkey injection test also needs
  `INPUTTOGGLE_ALLOW_F24=1`). It never enables or disables a device.

Running from source also works: `pythonw src\inputtoggle.pyw` (Python 3.11;
Pillow is optional, for the tray icons). It uses the helper task that the
installer registered.

The version lives in `src\inputtoggle.pyw` (`VERSION`). GitHub Actions builds
every push to `main`, every pull request and every `v*` tag; pushing a tag
`v<version>` that matches the version publishes a release with the installer
and `SHA256SUMS.txt`.

## License

InputToggle is licensed under the [MIT License](LICENSE).

The installer also contains third-party software, each under its own license:
the CPython 3.11 runtime and the libraries of its Windows build (libffi,
bzip2, XZ Utils, zlib, the Microsoft C runtime), mpdecimal, Tcl/Tk 8.6 and
the PyInstaller bootloader. Their license texts are installed as
`THIRD-PARTY-NOTICES.txt` next to the program.
