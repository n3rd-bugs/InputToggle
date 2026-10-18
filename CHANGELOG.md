# Changelog

All notable changes to InputToggle are listed here. Versions follow
[semantic versioning](https://semver.org/).

## [1.2.2] - 2026-10-18

### Fixed

- Auto mode for Chrome Remote Desktop no longer loses to "Re-enable at
  startup". When InputToggle started while a session was already open (for
  example after signing in through Chrome Remote Desktop), auto mode switched
  the devices off and the startup re-enable switched them back on a second
  later. Auto mode now waits until the startup work has finished.

## [1.2.1] - 2026-10-18

### Fixed

- Setup could not register the helper task, so a new installation reported
  "helper not installed". Windows PowerShell 5.1 leaves `$PSScriptRoot` empty
  in the parameter defaults of an advanced script started with `-File`, and
  `register_task.ps1` used it to find the install folder. Upgrades over an
  existing task kept working with the old task settings; installing 1.2.1
  registers the task again.
- Tests now run `register_task.ps1` under Windows PowerShell the way Setup
  does, and reject `$PSScriptRoot` in parameter defaults.

## [1.2.0] - 2026-10-18

First public release.

### Added

- A Windows installer (`InputToggle-Setup-<version>.exe`, Inno Setup). It
  installs to `C:\Program Files\InputToggle`, registers the helper task
  `\InputToggle\Apply` for the installing account, adds the logon entry and
  starts InputToggle as the signed-in, non-elevated user.
- Upgrades stop a running InputToggle of any earlier version the way its
  tray menu's Exit does, so it switches its devices back on before files
  change.
- An uninstaller. It stops InputToggle (devices back on), re-enables anything
  still recorded as switched off, removes the task, the files, the logs and
  the logon entry, and keeps your settings.
- A self-contained release build (PyInstaller): no Python installation is
  needed. Tray and program icons are drawn at build time.
- `THIRD-PARTY-NOTICES.txt` next to the program, with the licenses of the
  bundled runtime (CPython, Tcl/Tk, mpdecimal, the PyInstaller bootloader).
- `--version`, and `--out <file>` for the read-only modes `--version`,
  `--status` and `--selftest` (the release exe has no console).
- An optional desktop shortcut that switches the selected devices off and on
  (installer option).
- `build.ps1` and a GitHub Actions workflow that test, build and publish
  releases together with `SHA256SUMS.txt`.

### Changed

- Nothing is pre-selected. On the first start, Settings opens once so you can
  choose the devices.
- "Start with Windows" starts `InputToggle.exe`, and the app keeps the logon
  entry in line with that setting.
- The tray application refuses to run elevated (the elevated half of a UAC
  split token) and says so.
- `--selftest` no longer expects particular hardware.
- Messages about a missing helper now say "reinstall InputToggle".

## 1.1 and earlier (not public)

Private, script-based versions: a Python tray script plus PowerShell setup
scripts that compiled the helper and registered the task. They introduced
the design this release keeps: a non-elevated tray application with a small
elevated helper started only by an on-demand scheduled task, non-persistent
disabling, a write-ahead record of what was switched off, re-enabling on
exit, sign-out and start, the Chrome Remote Desktop auto mode, keyboard
lock-out warnings, and strict validation of requests in the helper.
