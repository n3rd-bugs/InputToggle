# Security policy

## Supported versions

Only the latest release receives fixes.

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub:
**Security > Report a vulnerability** on
<https://github.com/n3rd-bugs/InputToggle/security/advisories/new>.
Do not open a public issue for a vulnerability. If that form is not
available, open an issue titled "Security contact request" without any
details, and you will be contacted.

Include the version (`InputToggle.exe --version --out <file>`), what you did,
what happened, and what you expected. Output of `--status` or `--selftest`
contains your Windows user name and device instance IDs (some include a
serial number); remove them before you share it. This is a small project maintained in
spare time: expect a first answer within a couple of weeks, and a fix as fast
as its severity calls for.

## Threat model in short

InputToggle has two parts:

- the tray application, which runs as the signed-in user with normal rights
  (it exits if it is started elevated from a UAC account), and
- `InputToggleHelper.exe`, the only elevated part. It is started only by the
  on-demand scheduled task `\InputToggle\Apply`, which the installer
  registers for the installing account, and it runs from
  `C:\Program Files\InputToggle`, which only administrators can change.

The helper treats its request file (in the user's profile) as untrusted: it
enforces a size limit, path and link checks, a 60-second freshness window, an
action whitelist and a strict instance-id format, and it only enables or
disables **Mouse- and Keyboard-class** device nodes, never persistently.

In scope, for example:

- a way to make the helper (or the installer or uninstaller, which run
  elevated) do anything other than switch Mouse/Keyboard devices on or off,
- a way to read or write files with elevated rights through InputToggle,
- a way for another account to use the task or the helper,
- a way to make a switched-off device survive a restart.

Known and accepted (see the README's *Security model*):

- any program running as the installing user can switch that user's Mouse
  and Keyboard devices off or on through the task (non-persistently),
- the release binaries are not code-signed.
