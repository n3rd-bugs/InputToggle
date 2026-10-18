# -*- coding: utf-8 -*-
"""InputToggle - switch selected input devices off and on with a global hotkey.

WHY THIS EXISTS
    A mouse that reports tiny phantom movements while nobody touches it (a
    worn sensor, a vibrating desk, a noisy wireless receiver) makes Chrome
    Remote Desktop believe that someone is at the desk: it drops all remote
    input for about 2 s after every local event, and its "Stop sharing" bar can
    take the focus. Switching the physical device off while you are away fixes
    that. InputToggle does it with one global hotkey (default
    Ctrl+Alt+Shift+M) or from its tray menu, and the same hotkey switches the
    device back on.

DESIGN (split privilege: this program never runs elevated)
    Enabling or disabling a device node needs administrator rights. Running a
    whole GUI program (and its interpreter and libraries) elevated would widen
    the attack surface for no benefit, so the actual switch is done by a tiny
    compiled helper (InputToggleHelper.exe) that is started only through the
    on-demand Scheduled Task \\InputToggle\\Apply (RunLevel Highest), which the
    installer registers for the installing account. This frontend runs
    NON-elevated: it only *reads* device state (cfgmgr32 works unelevated),
    writes a request file into the user's profile, and asks the task to run.
    The helper treats that request as untrusted input.

    - Tk lives in the main thread with a withdrawn root; Settings is a Toplevel
      opened on demand (and once on the very first run, so that a fresh install
      can pick its devices). Nothing else is ever shown unprompted. Tk is
      touched from the main thread only.
    - A Win32 thread owns a hidden top-level window (so it receives the
      "TaskbarCreated" broadcast), the tray icon and the global hotkey, and runs
      its own message loop.
    - Device switches run on worker threads, serialized by one lock, so neither
      loop blocks. The Chrome Remote Desktop poller has its own thread.
    - The threads talk through a queue (polled by root.after) and PostMessageW.
    - state.json records every device InputToggle switched off. A disable is
      recorded BEFORE the helper runs ("pending") and reconciled afterwards, so
      a crash or a slow helper never leaves an unrecorded disabled device.

FROZEN BUILD
    Releases are a PyInstaller "onedir", windowed build installed under
    Program Files. It behaves like the script, except that "Start with
    Windows" points at InputToggle.exe, the tray icons come from the bundle,
    and the read-only CLI modes accept --out <file>, because a windowed exe
    has no console to print to.

CLI
    (no args)       run the tray application (pythonw.exe or InputToggle.exe)
    --version       print the version
    --settings      open Settings (or ask a running instance to)
    --toggle        toggle the selected devices once (asks a running instance)
    --enable-all    re-enable everything InputToggle disabled (asks a running
                    instance if there is one)
    --quit          ask a running instance to exit cleanly and wait for it
    --status        print the Mouse/Keyboard device inventory (read-only)
    --selftest      read-only self checks (no device changes, no window)
    --out <file>    with --version, --status, --selftest or a --selftest-*
                    mode: also write the output to <file> (UTF-8). --out never
                    starts the tray application.
    --selftest-hotkey [--attempts N] / --selftest-tray / --selftest-settings /
    --selftest-controller    (tests only)

Logs: %LOCALAPPDATA%\\InputToggle\\inputtoggle.log (rotating 1 MB x3).
"""

import os
import sys
import json
import time
import copy
import tempfile
import threading
import collections
import subprocess
import queue as _queue
import logging
import logging.handlers
import ctypes
from ctypes import wintypes as wt

APP_NAME = "InputToggle"
# The mutex name, window class and private message ids below are shared with
# every earlier version: the installer (and --quit) stop a running instance of
# ANY version by posting MSG_REQUEST_QUIT to this window class. Keep them.
MUTEX_NAME = "Local\\InputToggle"
WND_CLASS = "InputToggleWndCls"
WND_TITLE = "InputToggle"
TASK_NAME = "\\InputToggle\\Apply"
VERSION = "1.2.2"
HELPER_LOG_HINT = r"C:\Program Files\InputToggle\logs\helper.log"
CRD_PROCESS = "remoting_desktop.exe"
# Auto mode waits (at most this long) for the startup re-enable to finish, so
# that re-enable cannot undo the switch-off for a session that is already open.
STARTUP_GATE_S = 60.0

# True when running as the PyInstaller-built InputToggle.exe.
FROZEN = bool(getattr(sys, "frozen", False))

# Input-device setup class GUIDs (lower-case, braces) ----------------------
GUID_MOUSE = "{4d36e96f-e325-11ce-bfc1-08002be10318}"
GUID_KEYBOARD = "{4d36e96b-e325-11ce-bfc1-08002be10318}"
CLASS_NAME_BY_GUID = {GUID_MOUSE: "Mouse", GUID_KEYBOARD: "Keyboard"}


def helper_fix_hint():
    """What to do when the helper task is missing. The release is installed
    by its setup program, which registers the task; there are no scripts."""
    if FROZEN:
        return "reinstall InputToggle"
    return "install InputToggle with its setup program"


# ========================================================================
# Paths (no side effects at import; dirs are created lazily)
# ========================================================================
def resource_dir():
    """Folder with the bundled resources (icons): the PyInstaller bundle when
    frozen, else the folder of this script."""
    if FROZEN:
        return getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


ICON_ON_NAME = "icon_on.ico"
ICON_OFF_NAME = "icon_off.ico"
APP_ICON_NAME = "app.ico"


def bundled_icon(name):
    """Path of a bundled icon, or None when it is not there (running from
    source, or an incomplete bundle)."""
    p = os.path.join(resource_dir(), "icons", name)
    return p if os.path.isfile(p) else None

def data_dir():
    """%LOCALAPPDATA%\\InputToggle, or $INPUTTOGGLE_DATADIR for tests."""
    d = os.environ.get("INPUTTOGGLE_DATADIR")
    if not d:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        d = os.path.join(base, APP_NAME)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def settings_path():
    return os.path.join(data_dir(), "settings.json")


def state_path():
    return os.path.join(data_dir(), "state.json")


def request_path():
    return os.path.join(data_dir(), "request.txt")


def log_path():
    return os.path.join(data_dir(), "inputtoggle.log")


def atomic_write(path, text, retries=3):
    """Write text to path atomically (temp file in the same dir + os.replace).

    os.replace can hit a sharing violation for a moment while the helper is
    reading the old file, so it is retried briefly.
    """
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp_", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        for attempt in range(retries):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == retries - 1:
                    raise
                time.sleep(0.1)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def _read_text_quiet(path):
    try:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            return fh.read()
    except (OSError, ValueError):
        return None


def _short(text, n):
    text = text or ""
    return text if len(text) <= n else text[:max(0, n - 3)].rstrip() + "..."


# ========================================================================
# Hotkey model  (PURE logic -- unit tested, no Win32)
# ========================================================================
# Windows virtual-key codes for the keys the Settings combobox offers.
VK_BY_NAME = {}
NAME_BY_VK = {}


def _reg(name, vk):
    VK_BY_NAME[name] = vk
    NAME_BY_VK.setdefault(vk, name)


for _c in range(ord("A"), ord("Z") + 1):
    _reg(chr(_c), _c)                      # A-Z  == VK 0x41..0x5A
for _d in range(0, 10):
    _reg(str(_d), 0x30 + _d)               # 0-9  == VK 0x30..0x39
for _f in range(1, 25):
    _reg("F%d" % _f, 0x70 + _f - 1)        # F1..F24 == 0x70..0x87
_reg("Pause", 0x13)
_reg("ScrollLock", 0x91)
_reg("Insert", 0x2D)
_reg("Home", 0x24)
_reg("End", 0x23)
_reg("PageUp", 0x21)
_reg("PageDown", 0x22)
for _n in range(0, 10):
    _reg("Numpad%d" % _n, 0x60 + _n)       # VK_NUMPAD0..9 == 0x60..0x69
_reg("Numpad*", 0x6A)
_reg("Numpad+", 0x6B)
_reg("Numpad-", 0x6D)
_reg("Numpad.", 0x6E)
_reg("Numpad/", 0x6F)

KEY_CHOICES = (
    [chr(c) for c in range(ord("A"), ord("Z") + 1)]
    + [str(d) for d in range(0, 10)]
    + ["F%d" % f for f in range(1, 25)]
    + ["Pause", "ScrollLock", "Insert", "Home", "End", "PageUp", "PageDown"]
    + ["Numpad%d" % n for n in range(0, 10)]
    + ["Numpad*", "Numpad+", "Numpad-", "Numpad.", "Numpad/"]
)

# Win32 RegisterHotKey modifier bits (verified against WinUser.h).
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

VK_SHIFT, VK_CONTROL, VK_MENU = 0x10, 0x11, 0x12
VK_LWIN, VK_RWIN = 0x5B, 0x5C
VK_DELETE = 0x2E
VK_ESCAPE = 0x1B
VK_L = 0x4C
VK_F4 = 0x73
VK_F13, VK_F24 = 0x7C, 0x87
VK_PAUSE = 0x13
MODIFIER_VKS = frozenset([0x10, 0x11, 0x12, 0x5B, 0x5C,
                          0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5])

# Ctrl+<letter> shortcuts every app relies on; never grab them globally.
CTRL_RESERVED_KEYS = frozenset("ACVXZYSFWTNPR")

DEFAULT_HOTKEY = {"ctrl": True, "alt": True, "shift": True, "win": False,
                  "key": "M", "vk": VK_BY_NAME["M"]}


def normalize_hotkey(hk):
    """Return a clean hotkey dict; derive vk<->key when one side is missing."""
    hk = dict(hk or {})
    out = {
        "ctrl": bool(hk.get("ctrl")),
        "alt": bool(hk.get("alt")),
        "shift": bool(hk.get("shift")),
        "win": bool(hk.get("win")),
    }
    vk = hk.get("vk")
    key = hk.get("key")
    if vk is None and key is not None:
        vk = VK_BY_NAME.get(str(key))
    if vk is not None and (key is None or str(key) not in VK_BY_NAME):
        key = NAME_BY_VK.get(int(vk), key)
    if key is None and vk is None:
        key, vk = DEFAULT_HOTKEY["key"], DEFAULT_HOTKEY["vk"]
    out["vk"] = int(vk) if vk is not None else None
    out["key"] = key if key is not None else (("VK_%02X" % int(vk)) if vk is not None else None)
    return out


def format_hotkey(hk):
    """'Ctrl+Alt+Shift+M'. Order: Ctrl, Alt, Shift, Win, key."""
    hk = normalize_hotkey(hk)
    parts = []
    if hk["ctrl"]:
        parts.append("Ctrl")
    if hk["alt"]:
        parts.append("Alt")
    if hk["shift"]:
        parts.append("Shift")
    if hk["win"]:
        parts.append("Win")
    parts.append(hk["key"] if hk["key"] else "?")
    return "+".join(parts)


def parse_hotkey(text):
    """Inverse of format_hotkey for strings like 'Ctrl+Alt+Shift+M'."""
    hk = {"ctrl": False, "alt": False, "shift": False, "win": False,
          "key": None, "vk": None}
    for tok in str(text).replace(" ", "").split("+"):
        if not tok:
            continue
        low = tok.lower()
        if low in ("ctrl", "control"):
            hk["ctrl"] = True
        elif low == "alt":
            hk["alt"] = True
        elif low == "shift":
            hk["shift"] = True
        elif low in ("win", "windows", "super"):
            hk["win"] = True
        else:
            # canonicalise key casing against the known table
            match = None
            for name in VK_BY_NAME:
                if name.lower() == low:
                    match = name
                    break
            hk["key"] = match if match else tok
    return normalize_hotkey(hk)


def hotkey_win32(hk):
    """(fsModifiers, vk) for RegisterHotKey, with MOD_NOREPEAT set."""
    hk = normalize_hotkey(hk)
    mods = MOD_NOREPEAT
    if hk["ctrl"]:
        mods |= MOD_CONTROL
    if hk["alt"]:
        mods |= MOD_ALT
    if hk["shift"]:
        mods |= MOD_SHIFT
    if hk["win"]:
        mods |= MOD_WIN
    return mods, hk["vk"]


def _modifiers(hk):
    return [m for m in ("ctrl", "alt", "shift", "win") if hk[m]]


def validate_hotkey(hk):
    """(ok, reason). Enforce the hotkey policy plus the common-key guard."""
    hk = normalize_hotkey(hk)
    vk = hk["vk"]
    if vk is None:
        return False, "No key chosen."
    exempt = (VK_F13 <= vk <= VK_F24) or vk == VK_PAUSE
    if not exempt and not (hk["ctrl"] or hk["alt"] or hk["win"]):
        return False, ("Add Ctrl, Alt or Win (Shift alone is not enough) "
                       "unless the key is F13-F24 or Pause.")
    if hk["ctrl"] and hk["alt"] and vk == VK_DELETE:
        return False, "Ctrl+Alt+Del is reserved by Windows."
    if hk["ctrl"] and hk["alt"] and vk == VK_ESCAPE:
        return False, "Ctrl+Alt+Esc is reserved (CRD disconnect)."
    if hk["win"] and vk == VK_L:
        return False, "Win+L is reserved by Windows (lock)."
    mods = _modifiers(hk)
    if mods == ["alt"] and vk == VK_F4:
        return False, "Alt+F4 closes windows in every app; pick another combination."
    key = hk["key"] or ""
    if mods == ["ctrl"] and len(key) == 1 and key in CTRL_RESERVED_KEYS:
        return False, ("Ctrl+%s is a standard shortcut in every app; "
                       "pick another combination." % key)
    return True, ""


def hotkey_warning(hk):
    """Text for an allowed combination that takes a common key away from
    every other app (one modifier + letter, digit or F1-F12); '' if none."""
    hk = normalize_hotkey(hk)
    ok, _ = validate_hotkey(hk)
    if not ok:
        return ""
    vk = hk["vk"]
    common = (0x41 <= vk <= 0x5A) or (0x30 <= vk <= 0x39) or (0x70 <= vk <= 0x7B)
    if len(_modifiers(hk)) == 1 and common:
        return ("%s will stop working in every other app while InputToggle runs."
                % format_hotkey(hk))
    return ""


def hotkey_from_record(vk, ctrl, alt, shift, win):
    """Map one key press captured by 'Record...' to (status, hotkey).

    status: 'wait' (a modifier alone), 'cancel' (Esc with no modifier),
    'unsupported' (a key Settings does not offer) or 'ok'. The modifier flags
    come from GetKeyState, not from Tk's event.state (on Windows Tk reports
    NumLock in the bit that looks like Alt, and has no Win bit at all).
    """
    if vk in MODIFIER_VKS:
        return "wait", None
    if vk == VK_ESCAPE and not (ctrl or alt or shift or win):
        return "cancel", None
    name = NAME_BY_VK.get(vk)
    if not name:
        return "unsupported", None
    return "ok", normalize_hotkey({"ctrl": bool(ctrl), "alt": bool(alt),
                                   "shift": bool(shift), "win": bool(win),
                                   "key": name, "vk": vk})


# ========================================================================
# Settings + state  (PURE logic -- unit tested)
# ========================================================================
DEFAULT_SETTINGS = {
    "version": 2,
    "hotkey": DEFAULT_HOTKEY,
    "devices": [],               # list of selected instance-id strings
    "show_disconnected": False,
    "show_notifications": True,
    "play_sound": False,
    "reenable_on_exit": True,
    "reenable_at_startup": True,
    "auto_disable_crd": False,
    "start_with_windows": True,
    "first_run_done": False,
}


def default_settings():
    return copy.deepcopy(DEFAULT_SETTINGS)


def migrate_settings(raw):
    """Merge a loaded dict onto current defaults, upgrading old shapes.

    v1 stored the hotkey as a 'Ctrl+Alt+M' string under 'hotkey'; v2 stores a
    dict. Unknown keys are dropped, missing keys filled from defaults.
    """
    out = default_settings()
    if not isinstance(raw, dict):
        return out
    hk = raw.get("hotkey")
    if isinstance(hk, str):
        out["hotkey"] = parse_hotkey(hk)
    elif isinstance(hk, dict):
        out["hotkey"] = normalize_hotkey(hk)
    for key in ("show_disconnected", "show_notifications", "play_sound",
                "reenable_on_exit", "reenable_at_startup", "auto_disable_crd",
                "start_with_windows", "first_run_done"):
        if key in raw:
            out[key] = bool(raw[key])
    devs = raw.get("devices")
    if isinstance(devs, list):
        out["devices"] = [str(d) for d in devs if isinstance(d, str) and d.strip()]
    out["version"] = 2
    return out


def first_run_plan(settings):
    """(settings, open_settings) for the start of the tray application.

    Nothing is ever pre-selected. On the first start of a fresh install no
    device is selected yet, so Settings opens once (the user just installed
    the program) and first_run_done is set, so that it never opens on its own
    again. Existing settings (an upgrade, or a later start) are left alone.
    """
    s = dict(settings or {})
    if s.get("first_run_done"):
        return s, False
    s["first_run_done"] = True
    return s, not s.get("devices")


def load_settings():
    try:
        with open(settings_path(), "r", encoding="utf-8") as fh:
            return migrate_settings(json.load(fh))
    except (OSError, ValueError):
        return default_settings()


def save_settings(s):
    atomic_write(settings_path(), json.dumps(s, indent=2))


# state.json: what InputToggle switched off.
#   disabled      - switched off (manually or automatically) and confirmed
#   auto_disabled - the subset auto mode switched off for a CRD session
#   pending       - recorded BEFORE a disable request is handed to the helper;
#                   moved to 'disabled' once confirmed. Every re-enable path
#                   reads all three lists, and re-enable checks the real device
#                   status, so recording too much is harmless.
STATE_KEYS = ("disabled", "auto_disabled", "pending")


def empty_state():
    return {"disabled": [], "auto_disabled": [], "pending": []}


def normalize_state(data):
    out = empty_state()
    if isinstance(data, dict):
        for k in STATE_KEYS:
            v = data.get(k, [])
            if isinstance(v, list):
                out[k] = sorted(set(str(x) for x in v if isinstance(x, str) and x))
    return out


def load_state():
    try:
        with open(state_path(), "r", encoding="utf-8") as fh:
            return normalize_state(json.load(fh))
    except (OSError, ValueError):
        return empty_state()


def save_state(st):
    atomic_write(state_path(), json.dumps(normalize_state(st), indent=2))


def tracked_ids(st):
    """Every id the state mentions, in a stable order."""
    st = normalize_state(st)
    out = []
    for k in STATE_KEYS:
        for i in st[k]:
            if i not in out:
                out.append(i)
    return out


def state_mark_pending(st, targets):
    st = normalize_state(st)
    st["pending"] = sorted(set(st["pending"]) | set(targets))
    return st


def device_is_off(probe):
    """A probe shows the device switched off (or no longer present)."""
    probe = probe or {}
    return (not probe.get("present")) or probe.get("enabled") is False


def device_is_on(probe):
    probe = probe or {}
    return bool(probe.get("present")) and probe.get("enabled") is True


def reconcile_state(st, action, targets, final, ok, auto=False, may_still_apply=False):
    """Fold the outcome of one operation into the state record.

    final: {id: probe} read after the operation. When in doubt an id stays
    recorded: a recorded-but-enabled device costs nothing, an unrecorded
    disabled one is stranded.
    may_still_apply: the helper task was started but the poll gave up, so a
    late helper run might still switch the device off.
    """
    st = normalize_state(st)
    dis = set(st["disabled"])
    autod = set(st["auto_disabled"])
    pend = set(st["pending"])
    final = final or {}
    for t in targets:
        p = final.get(t)
        if action == "disable":
            if ok or (p is not None and device_is_off(p)):
                dis.add(t)
                if auto:
                    autod.add(t)
                pend.discard(t)
            elif not may_still_apply:
                pend.discard(t)
            # else: keep it pending; re-enable will check its real status
        else:
            if ok or (p is not None and device_is_on(p)):
                dis.discard(t)
                autod.discard(t)
                pend.discard(t)
    return {"disabled": sorted(dis), "auto_disabled": sorted(autod),
            "pending": sorted(pend)}


# ========================================================================
# Request file  (PURE logic -- unit tested).  Mirrors helper.cs parsing.
# ========================================================================
# Device-instance-id character policy, shared with the C# helper. Instance ids
# look like  HID\VID_1234&PID_5678&MI_00\7&1a2b3c4d&0&0000 .
_ID_ALLOWED = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    "\\&_.#-{}+,"
)
MAX_DEVICE_LINES = 32
MAX_ID_LEN = 400
MAX_REQUEST_BYTES = 16384      # the helper refuses anything larger
REQUEST_MAX_AGE_S = 60         # the helper refuses older requests ...
REQUEST_MAX_SKEW_S = 5         # ... and ones dated more than 5 s ahead


def valid_instance_id(s):
    if not isinstance(s, str):
        return False
    if not (1 <= len(s) <= MAX_ID_LEN):
        return False
    if "\\" not in s:
        return False
    return all(ch in _ID_ALLOWED for ch in s)


def format_request(action, device_ids, issued=None):
    if action not in ("disable", "enable"):
        raise ValueError("action must be disable|enable")
    if issued is None:
        issued = int(time.time())
    lines = ["action=%s" % action, "issued=%d" % int(issued)]
    for d in device_ids:
        lines.append("device=%s" % d)
    return "\n".join(lines) + "\n"


def parse_request(text):
    """Return (action, ids, skipped, issued). Raises ValueError if action bad.

    issued is the request's unix time or None. The helper additionally refuses
    a request without a fresh issued= line (see request_is_fresh).
    """
    action = None
    issued = None
    ids, skipped = [], []
    for raw in str(text).splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            skipped.append(line)
            continue
        k, v = line.split("=", 1)
        k = k.strip().lower()
        v = v.strip()
        if k == "action":
            action = v.lower()
        elif k == "issued":
            if v.isdigit() and len(v) <= 12:
                issued = int(v)
            else:
                skipped.append(line)
        elif k == "device":
            if len(ids) >= MAX_DEVICE_LINES or not valid_instance_id(v):
                skipped.append(v)
            else:
                ids.append(v)
        else:
            skipped.append(line)
    if action not in ("disable", "enable"):
        raise ValueError("missing or invalid action")
    return action, ids, skipped, issued


def request_is_fresh(issued, now=None):
    if issued is None:
        return False
    if now is None:
        now = time.time()
    return (now - REQUEST_MAX_AGE_S) <= issued <= (now + REQUEST_MAX_SKEW_S)


# ========================================================================
# Device-instance-id helpers and names  (PURE logic -- unit tested)
# ========================================================================
def parse_ids_from_devid(instance_id):
    """Pull VID/PID/MI/COL tokens out of an instance id (case-insensitive)."""
    up = (instance_id or "").upper()
    out = {"vid": None, "pid": None, "mi": None, "col": None}

    def grab(tag, n):
        i = up.find(tag)
        if i < 0:
            return None
        return up[i + len(tag): i + len(tag) + n]

    out["vid"] = grab("VID_", 4)
    out["pid"] = grab("PID_", 4)
    mi = grab("MI_", 2)
    if mi:
        out["mi"] = "MI_" + mi
    i = up.find("COL")
    if i >= 0:
        out["col"] = up[i:i + 5]
    return out


def is_composite_parent_id(instance_id):
    """True for a 'USB\\VID_xxxx&PID_xxxx\\...' node with no interface suffix."""
    up = (instance_id or "").upper()
    return ("VID_" in up and "PID_" in up and "&MI_" not in up and "&COL" not in up)


def decide_action(enabled_flags):
    """Toggle rule: if ANY present target is enabled, disable all; else enable."""
    return "disable" if any(enabled_flags) else "enable"


# Root-enumerated / software / remote-session input stacks (e.g. ROOT\RDP_KBD,
# ROOT\RDP_MOU, TS_INPT\..., Hyper-V VMBUS\...). They are not hardware at the
# desk, and switching them off can cut the remote way back in, so they are
# hidden by default, never pre-selected and never count as "a keyboard".
VIRTUAL_ID_PREFIXES = ("ROOT\\", "SWD\\", "TS_INPT\\", "TERMINPUT_BUS\\",
                       "UMB\\", "VMBUS\\")


def is_virtual_input_id(instance_id):
    up = (instance_id or "").upper()
    return any(up.startswith(p) for p in VIRTUAL_ID_PREFIXES)


VENDOR_BY_VID = {
    "046D": "Logitech", "1532": "Razer", "045E": "Microsoft",
    "1038": "SteelSeries", "1B1C": "Corsair", "05AC": "Apple", "413C": "Dell",
    "03F0": "HP", "17EF": "Lenovo", "0B05": "ASUS", "3434": "Keychron",
    "046A": "Cherry", "0951": "Kingston", "1E7D": "ROCCAT",
    "2516": "Cooler Master",
}


def display_name(name, vid=None, pid=None, with_ids=True):
    """'Logitech USB Receiver (046D:C548)': adds the vendor when the name lacks
    it, and VID:PID so two look-alike receivers can be told apart."""
    base = (name or "").strip() or "Device"
    vendor = VENDOR_BY_VID.get((vid or "").upper())
    if vendor and vendor.lower() not in base.lower():
        base = "%s %s" % (vendor, base)
    if with_ids and vid and pid:
        base = "%s (%s:%s)" % (base, vid.upper(), pid.upper())
    return base


def iface_text(dev):
    mi = dev.get("mi") or ""
    col = dev.get("col") or ""
    if mi and col:
        return "%s/%s" % (mi, col)
    return mi or col or ""


def child_label(dev):
    """'Mouse - MI_00', 'Keyboard - MI_01/COL01'."""
    cls = dev.get("class_name") or "Device"
    it = iface_text(dev)
    if it:
        label = "%s - %s" % (cls, it)
    else:
        nm = dev.get("name")
        label = "%s - %s" % (cls, nm) if nm and nm != cls else cls
    if dev.get("virtual"):
        label += " [virtual/remote]"
    return label


def group_status_text(devs):
    present = [d for d in devs if d.get("present")]
    if not present:
        return "not connected"
    off = [d for d in present if d.get("enabled") is False]
    if not off:
        return "ON"
    if len(off) == len(present):
        return "OFF"
    return "%d of %d OFF" % (len(off), len(present))


def group_type_text(devs):
    classes = sorted(set(d.get("class_name") or "?" for d in devs))
    if classes == ["Keyboard", "Mouse"]:
        return "Mouse + Keyboard"
    return " + ".join(classes) if classes else "?"


def device_description(d):
    grp = d.get("group_display") or display_name(d.get("name") or "", d.get("vid"), d.get("pid"))
    return "%s, %s" % (grp, child_label(d))


def selection_label_from_probes(probes, max_len=48):
    """Short human name for the selected devices, e.g. 'Logitech USB Receiver'."""
    names = []
    for p in probes:
        iid = p.get("id") or ""
        ids = parse_ids_from_devid(iid)
        raw = p.get("name") if (p.get("name") and p.get("name") != iid) else ""
        if raw or ids.get("vid"):
            nm = display_name(raw, ids.get("vid"), ids.get("pid"), with_ids=False)
        else:
            nm = iid
        if nm and nm not in names:
            names.append(nm)
    if not names:
        return ""
    if len(names) == 1:
        label = names[0]
    elif len(names) == 2:
        label = "%s + %s" % (names[0], names[1])
    else:
        label = "%s + %d more" % (names[0], len(names) - 1)
    return _short(label, max_len)


# ========================================================================
# Pure UI logic (tooltip, menu, balloons, warnings, CRD edges) -- unit tested
# ========================================================================
_STATE_WORDS = {"enabled": "ON", "disabled": "OFF", "mixed": "partly OFF"}


def _verb(label):
    return "are" if " + " in (label or "") else "is"


def selection_warnings(flat, selected, enum_ok=True):
    """Warnings (code, text) that need a second Save to confirm.

    flat: the current device list (None when it could not be read). Virtual /
    remote-session devices never count as the keyboard or mouse at the desk.
    """
    sel = set(selected or [])
    if not enum_ok or flat is None:
        return [("unknown",
                 "InputToggle could not read the device list, so it cannot check "
                 "whether this selection includes the keyboard you type on. "
                 "Click Save again to save anyway.")]
    out = []
    phys = [d for d in flat if d.get("present") and not d.get("virtual")]
    kbs = [d for d in phys if d.get("class_name") == "Keyboard"]
    mice = [d for d in phys if d.get("class_name") == "Mouse"]
    kb_sel = [d for d in kbs if d.get("id") in sel]
    all_kb = bool(kbs) and len(kb_sel) == len(kbs)
    all_mice = bool(mice) and all(d.get("id") in sel for d in mice)
    if all_kb and all_mice:
        out.append(("total",
                    "This switches off EVERY connected keyboard AND mouse. While they "
                    "are off nothing at the desk works - not the hotkey, not the tray "
                    "menu. Only Chrome Remote Desktop, unplugging and replugging a USB "
                    "receiver, plugging in a spare USB keyboard or mouse, or a restart "
                    "brings input back. Click Save again to confirm."))
    elif kb_sel:
        names = "; ".join(device_description(d) for d in kb_sel[:3])
        if len(kb_sel) > 3:
            names += "; and %d more" % (len(kb_sel) - 3)
        out.append(("keyboard",
                    "This includes keyboard input (%s). If it is the keyboard you type "
                    "on, you cannot type or press the hotkey at the desk while it is "
                    "off. To get it back: use the tray menu with a mouse, Chrome Remote "
                    "Desktop, unplug and replug its USB receiver, or restart. Click "
                    "Save again to confirm." % names))
    virt = [d for d in flat if d.get("virtual") and d.get("id") in sel]
    if virt:
        out.append(("virtual",
                    "This includes a virtual or remote-session input device (%s). "
                    "Switching it off can cut remote access to this PC until a "
                    "restart. Click Save again to confirm."
                    % "; ".join(device_description(d) for d in virt[:3])))
    return out


def menu_toggle_spec(state, busy=False):
    """(label, action, grayed) for the tray's toggle item. The click does
    exactly what the label says; only the hotkey decides at run time."""
    if state == "disabled":
        label, action = "Enable selected devices", "enable"
    else:
        label, action = "Disable selected devices", "disable"
    grayed = bool(busy) or state not in ("enabled", "disabled", "mixed")
    return label, action, grayed


def status_text(state, label, busy=False, helper_ok=True):
    """One line of state for the tray menu's (grayed) first item. Touch
    clients over CRD get no hover tooltip, so the menu carries it too."""
    label = _short(label or "Selected devices", 48)
    if busy:
        return "Switching %s..." % label
    if helper_ok is False:
        return "Helper not installed - %s" % helper_fix_hint()
    if state == "nothing":
        return "No device selected (open Settings)"
    if state == "absent":
        return "%s: not connected" % label
    if state in _STATE_WORDS:
        return "%s: %s" % (label, _STATE_WORDS[state])
    return "%s: state unknown" % label


def build_tooltip(state, label, hk_text, hotkey_ok=True, helper_ok=True, busy=False,
                  hotkey_error=""):
    """Tray tooltip (<= 127 chars): what is on/off and whether the hotkey works."""
    label = _short(label or "Selected devices", 48)
    if busy:
        tip = "InputToggle: switching %s..." % label
    elif helper_ok is False:
        tip = "InputToggle: helper not installed - %s" % helper_fix_hint()
    else:
        if state == "nothing":
            st = "no device selected"
        elif state == "absent":
            st = "%s not connected" % label
        elif state in _STATE_WORDS:
            st = "%s %s" % (label, _STATE_WORDS[state])
        else:
            st = label
        if hotkey_ok:
            tip = "InputToggle: %s - %s" % (st, hk_text)
        else:
            tip = "InputToggle: %s - hotkey %s NOT active (%s)" % (
                st, hk_text, hotkey_error or "used by another program")
    return tip[:127]


_SOURCE_PREFIX = {"crd_start": "Chrome Remote Desktop connected. ",
                  "crd_end": "Chrome Remote Desktop disconnected. ",
                  "startup": "At startup: ",
                  "save": "Settings saved: "}
_QUIET_SOURCES = ("startup", "crd_start", "crd_end", "save", "exit")


def outcome_message(res, label, hk_text=None, source="manual"):
    """Balloon text for an operation result ('' when there is nothing to say)."""
    res = res or {}
    label = label or "The selected devices"
    verb = _verb(label)
    reason = res.get("reason") or ""
    action = res.get("action")
    quiet = source in _QUIET_SOURCES
    if res.get("task_missing") or reason == "task_missing":
        msg = "Helper not installed - %s." % helper_fix_hint()
    elif res.get("ok"):
        if not res.get("targets"):
            if quiet:
                return ""
            if action == "enable":
                msg = "Nothing to re-enable - InputToggle has nothing switched off."
            else:
                msg = "Nothing to change."
        elif action == "disable":
            if hk_text:
                msg = "%s %s OFF. Press %s to turn it back on." % (label, verb, hk_text)
            else:
                msg = "%s %s OFF. Use the tray menu to turn it back on." % (label, verb)
        else:
            msg = "%s %s ON again." % (label, verb)
    else:
        if reason == "none_selected":
            if quiet:
                return ""
            msg = "No device selected - open Settings to choose one."
        elif reason == "none_present":
            if quiet:
                return ""
            msg = "%s %s not connected." % (label, verb)
        elif reason == "timeout":
            msg = "Could not switch %s %s (timed out). See %s" % (
                label, "off" if action == "disable" else "on", HELPER_LOG_HINT)
        elif reason == "task_failed":
            msg = "Could not start the InputToggle helper task (%s)." % (
                res.get("outcome") or "error")
        else:
            msg = "InputToggle: %s" % (res.get("outcome") or "operation failed")
    return (_SOURCE_PREFIX.get(source, "") + msg)[:255]


class CrdTracker(object):
    """Debounced Chrome Remote Desktop session edges from periodic polls.

    step(pids) takes the set of remoting_desktop.exe PIDs, or None when the
    process snapshot failed (then nothing changes). A session ends only after
    end_after consecutive polls without the process, so one failed or empty
    poll never re-enables mid-session. A different PID while active is a new
    session ('restart'), so a reconnect between two polls is not missed.
    """

    def __init__(self, end_after=3):
        self.end_after = max(1, int(end_after))
        self.reset()

    def reset(self):
        self.active = False
        self.pids = frozenset()
        self.misses = 0

    def step(self, pids):
        if pids is None:
            return None
        pids = frozenset(pids)
        if pids:
            self.misses = 0
            if not self.active:
                self.active = True
                self.pids = pids
                return "start"
            if not (pids & self.pids):
                self.pids = pids
                return "restart"
            self.pids = pids
            return None
        if self.active:
            self.misses += 1
            if self.misses >= self.end_after:
                self.reset()
                return "end"
        return None


def signed16(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


# ========================================================================
# ctypes device layer (cfgmgr32) -- READ ONLY, works unelevated.
# Loaded at import but makes no calls until a function runs.
# ========================================================================
_cfg = ctypes.WinDLL("cfgmgr32", use_last_error=True)

ULONG = wt.ULONG
DEVINST = wt.DWORD
PDEVINST = ctypes.POINTER(wt.DWORD)
PULONG = ctypes.POINTER(ULONG)

CR_SUCCESS = 0x00000000
CR_BUFFER_SMALL = 0x0000001A

CM_GETIDLIST_FILTER_PRESENT = 0x00000100
CM_GETIDLIST_FILTER_CLASS = 0x00000200
CM_LOCATE_DEVNODE_NORMAL = 0x00000000
CM_LOCATE_DEVNODE_PHANTOM = 0x00000001
CM_PROB_DISABLED = 0x00000016          # 22  (verified in cfg.h)
CM_PROB_HARDWARE_DISABLED = 0x0000001D  # 29
CM_PROB_DISABLED_SERVICE = 0x00000020   # 32

DEVPROP_TYPE_STRING = 0x00000012
DEVPROP_TYPE_GUID = 0x0000000D


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


class DEVPROPKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", ctypes.c_ulong)]


def _devpropkey(d1, d2, d3, b0, b1, b2, b3, b4, b5, b6, b7, pid):
    g = GUID(d1, d2, d3, (ctypes.c_ubyte * 8)(b0, b1, b2, b3, b4, b5, b6, b7))
    k = DEVPROPKEY()
    k.fmtid = g
    k.pid = pid
    return k


# DEVPKEYs (verified against devpkey.h, SDK 10.0.26100.0)
DEVPKEY_NAME = _devpropkey(
    0xb725f130, 0x47ef, 0x101a, 0xa5, 0xf1, 0x02, 0x60, 0x8c, 0x9e, 0xeb, 0xac, 10)
DEVPKEY_Device_DeviceDesc = _devpropkey(
    0xa45c254e, 0xdf1c, 0x4efd, 0x80, 0x20, 0x67, 0xd1, 0x46, 0xa8, 0x50, 0xe0, 2)
DEVPKEY_Device_FriendlyName = _devpropkey(
    0xa45c254e, 0xdf1c, 0x4efd, 0x80, 0x20, 0x67, 0xd1, 0x46, 0xa8, 0x50, 0xe0, 14)
DEVPKEY_Device_ClassGuid = _devpropkey(
    0xa45c254e, 0xdf1c, 0x4efd, 0x80, 0x20, 0x67, 0xd1, 0x46, 0xa8, 0x50, 0xe0, 10)
DEVPKEY_Device_BusReportedDeviceDesc = _devpropkey(
    0x540b947e, 0x8b40, 0x45bc, 0xa8, 0xa2, 0x6a, 0x0b, 0x89, 0x4c, 0xbd, 0xa2, 4)

_cfg.CM_Get_Device_ID_List_SizeW.argtypes = [PULONG, wt.LPCWSTR, ULONG]
_cfg.CM_Get_Device_ID_List_SizeW.restype = ULONG
_cfg.CM_Get_Device_ID_ListW.argtypes = [wt.LPCWSTR, wt.LPWSTR, ULONG, ULONG]
_cfg.CM_Get_Device_ID_ListW.restype = ULONG
_cfg.CM_Locate_DevNodeW.argtypes = [PDEVINST, wt.LPCWSTR, ULONG]
_cfg.CM_Locate_DevNodeW.restype = ULONG
_cfg.CM_Get_DevNode_Status.argtypes = [PULONG, PULONG, DEVINST, ULONG]
_cfg.CM_Get_DevNode_Status.restype = ULONG
_cfg.CM_Get_DevNode_PropertyW.argtypes = [
    DEVINST, ctypes.POINTER(DEVPROPKEY), PULONG, ctypes.c_void_p, PULONG, ULONG]
_cfg.CM_Get_DevNode_PropertyW.restype = ULONG
_cfg.CM_Get_Parent.argtypes = [PDEVINST, DEVINST, ULONG]
_cfg.CM_Get_Parent.restype = ULONG
_cfg.CM_Get_Device_ID_Size.argtypes = [PULONG, DEVINST, ULONG]
_cfg.CM_Get_Device_ID_Size.restype = ULONG
_cfg.CM_Get_Device_IDW.argtypes = [DEVINST, wt.LPWSTR, ULONG, ULONG]
_cfg.CM_Get_Device_IDW.restype = ULONG


class DeviceListError(OSError):
    """The device list could not be read; callers treat it as 'unknown'."""


def _get_id_list(class_guid, present):
    """Instance ids of one setup class. Repeats size-then-list while the list
    grows in between (CR_BUFFER_SMALL), as the CM_Get_Device_ID_List docs say;
    raises DeviceListError instead of returning a misleading empty list."""
    flags = CM_GETIDLIST_FILTER_CLASS | (CM_GETIDLIST_FILTER_PRESENT if present else 0)
    for _attempt in range(5):
        ln = ULONG(0)
        cr = _cfg.CM_Get_Device_ID_List_SizeW(ctypes.byref(ln), class_guid, flags)
        if cr != CR_SUCCESS:
            raise DeviceListError("CM_Get_Device_ID_List_Size cr=0x%X" % cr)
        if ln.value < 2:
            return []
        buf = ctypes.create_unicode_buffer(ln.value)
        cr = _cfg.CM_Get_Device_ID_ListW(class_guid, buf, ln.value, flags)
        if cr == CR_SUCCESS:
            return [s for s in buf[:ln.value].split("\x00") if s]
        if cr != CR_BUFFER_SMALL:
            raise DeviceListError("CM_Get_Device_ID_List cr=0x%X" % cr)
    raise DeviceListError("the device list kept changing (CR_BUFFER_SMALL)")


def _locate(instance_id, allow_phantom=False):
    dn = wt.DWORD(0)
    flags = CM_LOCATE_DEVNODE_PHANTOM if allow_phantom else CM_LOCATE_DEVNODE_NORMAL
    if _cfg.CM_Locate_DevNodeW(ctypes.byref(dn), instance_id, flags) == CR_SUCCESS:
        return dn.value
    return None


def _devnode_status(devinst):
    st = ULONG(0)
    pr = ULONG(0)
    if _cfg.CM_Get_DevNode_Status(ctypes.byref(st), ctypes.byref(pr), devinst, 0) == CR_SUCCESS:
        return st.value, pr.value
    return None, None


def _get_string_prop(devinst, key):
    size = ULONG(0)
    ptype = ULONG(0)
    r = _cfg.CM_Get_DevNode_PropertyW(
        devinst, ctypes.byref(key), ctypes.byref(ptype), None, ctypes.byref(size), 0)
    if r != CR_BUFFER_SMALL or size.value == 0:
        return None
    buf = ctypes.create_string_buffer(size.value)
    r = _cfg.CM_Get_DevNode_PropertyW(
        devinst, ctypes.byref(key), ctypes.byref(ptype), buf, ctypes.byref(size), 0)
    if r != CR_SUCCESS or ptype.value != DEVPROP_TYPE_STRING:
        return None
    try:
        return buf.raw[:size.value].decode("utf-16-le").split("\x00", 1)[0] or None
    except Exception:
        return None


def _get_parent(devinst):
    p = wt.DWORD(0)
    if _cfg.CM_Get_Parent(ctypes.byref(p), devinst, 0) == CR_SUCCESS:
        return p.value
    return None


def _get_device_id(devinst):
    ln = ULONG(0)
    if _cfg.CM_Get_Device_ID_Size(ctypes.byref(ln), devinst, 0) != CR_SUCCESS:
        return None
    buf = ctypes.create_unicode_buffer(ln.value + 1)
    if _cfg.CM_Get_Device_IDW(devinst, buf, ln.value + 1, 0) != CR_SUCCESS:
        return None
    return buf.value


def _friendly_name(devinst):
    cur = devinst
    for _ in range(12):
        v = _get_string_prop(cur, DEVPKEY_Device_BusReportedDeviceDesc)
        if v:
            return v
        parent = _get_parent(cur)
        if parent is None:
            break
        cur = parent
    for key in (DEVPKEY_Device_FriendlyName, DEVPKEY_Device_DeviceDesc, DEVPKEY_NAME):
        v = _get_string_prop(devinst, key)
        if v:
            return v
    return None


def _group_info(devinst, iid):
    cur = devinst
    for _ in range(12):
        cid = _get_device_id(cur)
        if cid and is_composite_parent_id(cid):
            nm = (_get_string_prop(cur, DEVPKEY_Device_BusReportedDeviceDesc)
                  or _get_string_prop(cur, DEVPKEY_Device_FriendlyName)
                  or _get_string_prop(cur, DEVPKEY_Device_DeviceDesc))
            return cid, nm
        parent = _get_parent(cur)
        if parent is None:
            break
        cur = parent
    if is_composite_parent_id(iid):
        return iid, None
    return None, None


def _build_device(iid, guid, cname, present):
    d = {"id": iid, "class_guid": guid, "class_name": cname, "present": present,
         "virtual": is_virtual_input_id(iid)}
    d.update(parse_ids_from_devid(iid))
    devinst = _locate(iid, allow_phantom=not present)
    problem = None
    enabled = None
    name = None
    group_key = None
    group_name = None
    if devinst is not None:
        _st, problem = _devnode_status(devinst)
        if present:
            enabled = (problem != CM_PROB_DISABLED)
        name = _friendly_name(devinst)
        group_key, group_name = _group_info(devinst, iid)
    if not name:
        name = cname
    if not group_key:
        if d.get("vid") and d.get("pid"):
            group_key = "VID_%s&PID_%s" % (d["vid"], d["pid"])
        else:
            group_key = iid
    d.update(problem=problem, enabled=enabled, name=name,
             group_key=group_key, group_name=group_name)
    return d


def enumerate_devices(show_disconnected=False):
    """Return a list of groups: {key, name, type, status, devices:[dev,...]}.

    Raises DeviceListError when a class list cannot be read.
    """
    flat = []
    for guid, cname in ((GUID_MOUSE, "Mouse"), (GUID_KEYBOARD, "Keyboard")):
        present_ids = set(_get_id_list(guid, True))
        all_ids = set(present_ids)
        if show_disconnected:
            all_ids |= set(_get_id_list(guid, False))
        for iid in sorted(all_ids):
            flat.append(_build_device(iid, guid, cname, iid in present_ids))
    return group_devices(flat)


def group_devices(flat):
    groups = {}
    order = []
    for d in flat:
        gk = d["group_key"]
        if gk not in groups:
            groups[gk] = {"raw_name": None, "devices": []}
            order.append(gk)
        g = groups[gk]
        g["devices"].append(d)
        if not g["raw_name"] and d.get("group_name"):
            g["raw_name"] = d["group_name"]
    out = []
    for gk in order:
        g = groups[gk]
        devs = g["devices"]
        sample = devs[0]
        vid, pid = sample.get("vid"), sample.get("pid")
        sample_name = sample.get("name") if sample.get("name") != sample.get("class_name") else ""
        raw = g["raw_name"] or sample_name or ""
        if raw or (vid and pid):
            name = display_name(raw or sample.get("class_name") or "", vid, pid)
        else:
            name = sample.get("name") or gk
        for d in devs:
            d["group_display"] = name
        out.append({"key": gk, "name": name, "base_name": name,
                    "type": group_type_text(devs), "status": group_status_text(devs),
                    "present": any(d.get("present") for d in devs),
                    "virtual": all(d.get("virtual") for d in devs),
                    "vidpid": (vid, pid) if (vid and pid) else None,
                    "devices": devs})
    # A receiver plugged into another USB port gets a new instance id, so the
    # same VID:PID shows up again as a disconnected group. Say so, but only
    # when a connected copy exists.
    for g in out:
        if g["present"] or not g["vidpid"]:
            continue
        if any(o is not g and o["present"] and o["vidpid"] == g["vidpid"] for o in out):
            g["name"] = g["base_name"] + " - seen on another USB port"
    return out


def flatten(groups):
    out = []
    for g in groups:
        out.extend(g["devices"])
    return out


def probe_device(iid, want_name=True):
    """Class-agnostic single-device probe used for state and toggle polling.

    Locates the node (present first, then phantom for a removed device) and
    reports present / enabled / problem / name without needing its class.
    """
    d = {"id": iid, "present": False, "enabled": None, "problem": None,
         "name": iid}
    devinst = _locate(iid, allow_phantom=False)
    if devinst is not None:
        d["present"] = True
    else:
        devinst = _locate(iid, allow_phantom=True)
    if devinst is None:
        return d
    _st, problem = _devnode_status(devinst)
    d["problem"] = problem
    if d["present"]:
        d["enabled"] = (problem != CM_PROB_DISABLED)
    if want_name:
        nm = _friendly_name(devinst)
        if nm:
            d["name"] = nm
    return d


def status_of_ids(instance_ids, want_name=True):
    """Return {id: probe_device(id)} for each requested instance id."""
    return {iid: probe_device(iid, want_name) for iid in set(instance_ids)}


def selection_snapshot(ids, _status=None):
    """{'state', 'label', 'present', 'total'} for the selected ids (reads only).

    state: 'nothing' (no selection), 'absent' (selected but not connected),
    'enabled', 'disabled' or 'mixed'.
    """
    ids = [i for i in dict.fromkeys(ids or []) if i]
    if not ids:
        return {"state": "nothing", "label": "", "present": 0, "total": 0}
    st = (_status or status_of_ids)(ids)
    probes = [st[i] for i in ids]
    present = [p for p in probes if p.get("present")]
    label = selection_label_from_probes(probes)
    if not present:
        state = "absent"
    else:
        en = [p.get("enabled") for p in present]
        if all(en):
            state = "enabled"
        elif not any(en):
            state = "disabled"
        else:
            state = "mixed"
    return {"state": state, "label": label, "present": len(present), "total": len(ids)}


# ========================================================================
# Logging (rotating 1 MB x3) and exception hooks
# ========================================================================
_LOG = None


def log():
    global _LOG
    if _LOG is not None:
        return _LOG
    lg = logging.getLogger("inputtoggle")
    lg.setLevel(logging.INFO)
    lg.propagate = False
    if not lg.handlers:
        try:
            h = logging.handlers.RotatingFileHandler(
                log_path(), maxBytes=1 << 20, backupCount=3, encoding="utf-8")
            h.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)s [%(threadName)s] %(message)s"))
            lg.addHandler(h)
        except Exception:
            lg.addHandler(logging.NullHandler())
    _LOG = lg
    return lg


def install_exception_hooks(root=None):
    """Route every uncaught exception to the log (under pythonw sys.stderr is
    None, so Tk's default report_callback_exception would lose them)."""
    def _sys_hook(et, ev, tb):
        try:
            log().error("uncaught exception", exc_info=(et, ev, tb))
        except Exception:
            pass

    def _thread_hook(args):
        if args.exc_type is SystemExit:
            return
        try:
            log().error("uncaught exception in thread %s",
                        getattr(args.thread, "name", "?"),
                        exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        except Exception:
            pass

    sys.excepthook = _sys_hook
    threading.excepthook = _thread_hook
    if root is not None:
        def _tk_hook(et, ev, tb):
            try:
                log().error("Tk callback error", exc_info=(et, ev, tb))
            except Exception:
                pass
        root.report_callback_exception = _tk_hook


# ========================================================================
# Win32 bindings needed early (kernel32)
# ========================================================================
_u32 = ctypes.WinDLL("user32", use_last_error=True)
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_shell = ctypes.WinDLL("shell32", use_last_error=True)

_k32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
_k32.GetModuleHandleW.restype = wt.HMODULE
_k32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
_k32.CreateMutexW.restype = wt.HANDLE
_k32.OpenMutexW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
_k32.OpenMutexW.restype = wt.HANDLE
_k32.CloseHandle.argtypes = [ctypes.c_void_p]
_k32.CloseHandle.restype = wt.BOOL
_k32.GetSystemDirectoryW.argtypes = [wt.LPWSTR, wt.UINT]
_k32.GetSystemDirectoryW.restype = wt.UINT
ERROR_ALREADY_EXISTS = 183
ERROR_NO_MORE_FILES = 18
SYNCHRONIZE = 0x00100000


# ========================================================================
# Launching the elevated helper through the scheduled task
# ========================================================================
CREATE_NO_WINDOW = 0x08000000


def _system32():
    buf = ctypes.create_unicode_buffer(260)
    n = _k32.GetSystemDirectoryW(buf, 260)
    if 0 < n < 260:
        return buf.value
    return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")


def schtasks_exe():
    """Absolute path: a bare 'schtasks.exe' would be searched for first in the
    directory of the running executable (pythonw.exe, or InputToggle.exe),
    which is not necessarily protected the way System32 is."""
    return os.path.join(_system32(), "schtasks.exe")


def _decode(b):
    try:
        return (b or b"").decode("oem", errors="replace")
    except LookupError:
        return (b or b"").decode("latin-1", errors="replace")


def _run_schtasks(args, timeout):
    """(returncode, output text). Only the return code is used for decisions;
    the output is decoded leniently and used for the log only."""
    try:
        p = subprocess.run([schtasks_exe()] + list(args), creationflags=CREATE_NO_WINDOW,
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=timeout)
        return p.returncode, _decode(p.stdout) + _decode(p.stderr)
    except Exception as e:
        return -1, repr(e)


def apply_task_exists(timeout=15):
    rc, _out = _run_schtasks(["/query", "/tn", TASK_NAME], timeout)
    return rc == 0


def run_apply_task(timeout=20):
    """Run \\InputToggle\\Apply with no console flash. Returns (rc, output)."""
    return _run_schtasks(["/run", "/tn", TASK_NAME], timeout)


def _remove_request():
    """Delete request.txt so no later run of the task can replay it."""
    p = request_path()
    for _attempt in range(3):
        try:
            os.remove(p)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            time.sleep(0.1)
    log().warning("could not remove %s (the helper refuses it after %d s anyway)",
                  p, REQUEST_MAX_AGE_S)
    return False


def perform_toggle(selected_ids, forced_action=None, timeout=8.0, _run=None,
                   before_run=None, cleanup=True, run_timeout=20.0,
                   check_missing=True, _status=None, _sleep=None, poll=0.4):
    """Write the request, ask the task to apply it, and poll for the outcome.

    forced_action: None = toggle by current state; 'enable'/'disable' to force.
    before_run(action, targets) runs before the request is handed over (used to
    record intent in state.json); if it raises, nothing is requested.
    cleanup: delete request.txt after the poll (off only at session end, where
    a late helper run must still be able to apply the re-enable).
    Returns a result dict: ok, action, targets, reason, outcome, rc, started,
    task_missing, final ({id: probe} after the operation).
    """
    run = _run or run_apply_task
    status = _status or (lambda ids: status_of_ids(ids, want_name=False))
    sleep = _sleep or time.sleep
    res = {"ok": False, "action": None, "targets": [], "absent": [],
           "task_missing": False, "outcome": "", "reason": "", "changed": False,
           "rc": None, "started": False, "final": {}}
    selected_ids = [s for s in dict.fromkeys(selected_ids or []) if s]
    if not selected_ids:
        res.update(outcome="no devices selected", reason="none_selected")
        return res
    before = status(selected_ids)
    present = [i for i in selected_ids if before[i].get("present")]
    res["absent"] = [i for i in selected_ids if not before[i].get("present")]
    if not present:
        res.update(outcome="none of the selected devices are connected",
                   reason="none_present")
        return res
    action = forced_action or decide_action([before[i].get("enabled") for i in present])
    if action not in ("disable", "enable"):
        raise ValueError("bad action %r" % (action,))
    if action == "disable":
        targets = [i for i in present if before[i].get("enabled") is not False]
    else:
        targets = [i for i in present if before[i].get("enabled") is False]
    res["action"] = action
    res["targets"] = targets
    if not targets:
        res.update(ok=True, outcome="nothing to change", reason="nothing")
        return res
    if before_run is not None:
        try:
            before_run(action, targets)
        except Exception as e:
            log().exception("could not record state before %s", action)
            res.update(outcome="could not record state: %r" % (e,), reason="state_failed")
            return res
    text = format_request(action, targets)
    try:
        atomic_write(request_path(), text)
    except Exception as e:
        log().error("perform_toggle write failed: %r", e)
        res.update(outcome="could not write request: %r" % (e,), reason="write_failed",
                   final=status(targets))
        return res
    rc, out = run(run_timeout)
    res["rc"] = rc
    log().info("apply task rc=%s action=%s targets=%d out=%s",
               rc, action, len(targets), (out or "").strip()[:200])
    # request.txt is user-writable; another same-user process could swap it
    # between our write and the helper's read (accepted residual risk, see
    # README). Detect what we can, and keep polling OUR intended targets.
    seen = _read_text_quiet(request_path())
    if seen is not None and seen != text:
        log().warning("request.txt was changed by another process after it was written")
    if rc != 0:
        res["task_missing"] = (not apply_task_exists()) if check_missing else False
        res["reason"] = "task_missing" if res["task_missing"] else "task_failed"
        res["outcome"] = ("helper task not installed" if res["task_missing"]
                          else "helper task failed rc=%s" % rc)
        if cleanup:
            _remove_request()
        res["final"] = status(targets)
        return res
    res["started"] = True
    want_enabled = (action == "enable")
    deadline = time.monotonic() + timeout
    cur = {}
    while True:
        sleep(poll)
        cur = status(targets)
        if want_enabled:
            ok = all(cur[i].get("present") and cur[i].get("enabled") is True for i in targets)
        else:
            # a disabled devnode usually stays present with problem 22, but some
            # classes drop out of the present set entirely -- both mean "off".
            ok = all((not cur[i].get("present")) or cur[i].get("enabled") is False
                     for i in targets)
        if ok:
            res.update(ok=True, changed=True, reason="",
                       outcome="%sd %d device(s)" % (action, len(targets)))
            break
        if time.monotonic() >= deadline:
            res.update(reason="timeout",
                       outcome="timed out waiting for %s to take effect" % action)
            break
    if cleanup:
        _remove_request()
    res["final"] = cur if res["ok"] else status(targets)
    return res


# ---- state-keeping operations (used by the app under its lock, and the CLI)
def _write_ahead(action, targets):
    if action == "disable":
        save_state(state_mark_pending(load_state(), targets))


def _fold_result(res, auto=False):
    if (res.get("action") and res.get("targets")
            and res.get("reason") not in ("state_failed", "nothing")):
        st = reconcile_state(load_state(), res["action"], res["targets"],
                             res.get("final") or {}, bool(res.get("ok")), auto=auto,
                             may_still_apply=bool(res.get("started")) and not res.get("ok"))
        save_state(st)


def toggle_core(ids, forced=None, auto=False, **kw):
    """Toggle (or force) the selected ids, keeping state.json write-ahead."""
    res = perform_toggle(ids, forced, before_run=_write_ahead, **kw)
    _fold_result(res, auto=auto)
    return res


def enable_core(ids, clear_stale=True, _status=None, **kw):
    """Re-enable those of ids that are connected and switched off; forget ids
    that are already on or gone (a non-persistent disable does not survive
    re-plugging, so a gone device comes back enabled)."""
    status = _status or (lambda x: status_of_ids(x, want_name=False))
    ids = [i for i in dict.fromkeys(ids or []) if i]
    nothing = {"ok": True, "action": "enable", "targets": [],
               "outcome": "nothing to re-enable", "reason": "nothing"}
    if not ids:
        return nothing
    cur = status(ids)
    stale = set(i for i in ids if (not cur[i].get("present")) or cur[i].get("enabled") is True)
    if stale and clear_stale:
        st = normalize_state(load_state())
        for k in STATE_KEYS:
            st[k] = [i for i in st[k] if i not in stale]
        save_state(st)
    targets = [i for i in ids if cur[i].get("present") and cur[i].get("enabled") is False]
    if not targets:
        return nothing
    res = perform_toggle(targets, "enable", _status=_status, **kw)
    _fold_result(res)
    return res


def enable_all_core(selected_ids=(), fallback=True, **kw):
    """Re-enable everything state.json records. If the record is empty (lost
    or corrupted state.json), fall back to the current selection so recovery
    still has a target."""
    want = tracked_ids(load_state())
    if want:
        res = enable_core(want, **kw)
        res["fallback"] = False
        return res
    if fallback and selected_ids:
        log().info("nothing recorded as disabled; checking the current selection instead")
        res = enable_core(list(selected_ids), clear_stale=False, **kw)
        res["fallback"] = True
        return res
    return {"ok": True, "action": "enable", "targets": [], "outcome": "nothing to re-enable",
            "reason": "nothing", "fallback": False}


# ========================================================================
# Tray icons (two states). Order: the icons bundled with the release (made
# at build time), then icons drawn here with Pillow (running from source),
# then a stock icon (see Win32App._load_icons).
# ========================================================================
def ensure_icons():
    """Return (on_path, off_path) or (None, None). 'off' = devices disabled."""
    b_on, b_off = bundled_icon(ICON_ON_NAME), bundled_icon(ICON_OFF_NAME)
    if b_on and b_off:
        return b_on, b_off
    on = os.path.join(data_dir(), ICON_ON_NAME)
    off = os.path.join(data_dir(), ICON_OFF_NAME)
    if os.path.exists(on) and os.path.exists(off):
        return on, off
    try:
        # Not bundled with the release (it ships ready-made icons instead).
        from PIL import Image, ImageDraw

        def draw(disabled):
            base = 64
            img = Image.new("RGBA", (base, base), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            body = (210, 70, 70, 255) if disabled else (70, 170, 95, 255)
            # simple mouse silhouette
            d.rounded_rectangle([20, 10, 44, 54], radius=12, fill=body)
            d.line([32, 14, 32, 28], fill=(255, 255, 255, 230), width=3)
            if disabled:
                d.line([14, 50, 50, 14], fill=(255, 255, 255, 255), width=6)
                d.line([14, 50, 50, 14], fill=(210, 70, 70, 255), width=2)
            sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64)]
            return img, sizes

        im_on, sz = draw(False)
        im_on.save(on, format="ICO", sizes=sz)
        im_off, sz = draw(True)
        im_off.save(off, format="ICO", sizes=sz)
        return on, off
    except Exception as e:
        log().warning("icon generation failed: %r", e)
        return None, None


# ========================================================================
# Win32 layer: hidden window + tray icon + global hotkey + popup menu.
# Correct 64-bit integer types; WNDPROC kept alive on the instance.
# ========================================================================
LRESULT = ctypes.c_ssize_t
LPARAM = ctypes.c_ssize_t
WPARAM = ctypes.c_size_t
HCURSOR = wt.HANDLE
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, WPARAM, LPARAM)
WINEVENTPROC = ctypes.WINFUNCTYPE(None, wt.HANDLE, wt.DWORD, wt.HWND, wt.LONG,
                                  wt.LONG, wt.DWORD, wt.DWORD)

EVENT_SYSTEM_FOREGROUND = 0x0003
OBJID_WINDOW = 0
WINEVENT_OUTOFCONTEXT, WINEVENT_SKIPOWNPROCESS = 0x0000, 0x0002

WM_NULL = 0x0000
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_QUERYENDSESSION = 0x0011
WM_QUIT = 0x0012
WM_ENDSESSION = 0x0016
WM_CONTEXTMENU = 0x007B
WM_COMMAND = 0x0111
WM_TIMER = 0x0113
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_POWERBROADCAST = 0x0218
WM_DEVICECHANGE = 0x0219
WM_HOTKEY = 0x0312
WM_USER = 0x0400
WM_APP = 0x8000

# Tray notifications (shellapi.h, SDK 10.0.26100: all relative to WM_USER).
NIN_SELECT = WM_USER + 0                 # 0x400
NINF_KEY = 0x1
NIN_KEYSELECT = NIN_SELECT | NINF_KEY    # 0x401
NIN_BALLOONSHOW = WM_USER + 2            # 0x402
NIN_BALLOONHIDE = WM_USER + 3            # 0x403
NIN_BALLOONTIMEOUT = WM_USER + 4         # 0x404
NIN_BALLOONUSERCLICK = WM_USER + 5       # 0x405
NIN_POPUPOPEN = WM_USER + 6              # 0x406
NIN_POPUPCLOSE = WM_USER + 7             # 0x407

ENDSESSION_CLOSEAPP = 0x00000001

# our private messages
WM_TRAYCB = WM_APP + 1
MSG_OPEN_SETTINGS = WM_APP + 2
MSG_REREGISTER = WM_APP + 3
MSG_REFRESH = WM_APP + 4
MSG_TOGGLE = WM_APP + 5
MSG_ENABLE_ALL = WM_APP + 6
MSG_TOGGLE_DONE = WM_APP + 7
MSG_QUIT = WM_APP + 8                    # internal: tear the window down
MSG_REQUEST_QUIT = WM_APP + 9            # graceful exit (from --quit)
MSG_HOTKEY_SUSPEND = WM_APP + 10         # wParam 1 = suspend, 0 = resume

PBT_APMRESUMEAUTOMATIC = 0x0012
PBT_APMRESUMESUSPEND = 0x0007
DBT_DEVNODES_CHANGED = 0x0007

NIM_ADD, NIM_MODIFY, NIM_DELETE, NIM_SETVERSION = 0, 1, 2, 4
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_STATE, NIF_INFO, NIF_GUID, NIF_SHOWTIP = (
    0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x80)
NIIF_NONE, NIIF_INFO, NIIF_WARNING, NIIF_NOSOUND = 0x00, 0x01, 0x02, 0x10
NOTIFYICON_VERSION_4 = 4

MF_STRING, MF_SEPARATOR, MF_GRAYED, MF_CHECKED, MF_DISABLED = (
    0x0000, 0x0800, 0x0001, 0x0008, 0x0002)
TPM_RIGHTBUTTON, TPM_RETURNCMD, TPM_NONOTIFY = 0x0002, 0x0100, 0x0080

MB_OK, MB_ICONWARNING = 0x00000000, 0x00000030

IMAGE_ICON = 1
LR_LOADFROMFILE, LR_DEFAULTSIZE, LR_SHARED = 0x0010, 0x0040, 0x8000
IDI_APPLICATION = 32512
SPI_GETWORKAREA = 0x0030

IDM_TOGGLE, IDM_ENABLE_ALL, IDM_SETTINGS, IDM_LOGS, IDM_EXIT = 1, 2, 3, 4, 5
IDM_STATUS, IDM_STATUS2 = 10, 11
HOTKEY_ID = 0xA11
TRAY_RETRY_ID = 0x7A1

# Fixed identity for the tray icon, so the user's "show on taskbar" choice
# belongs to InputToggle and not to every icon of the same executable with
# uID 1. Windows ties a GUID to the path of the binary that first used it, so
# the installed InputToggle.exe and a copy run from source (pythonw.exe) use
# different GUIDs. If Windows refuses one anyway, add_tray falls back to the
# classic hWnd+uID identity.
TRAY_GUID_SOURCE = GUID(0xd534442e, 0x73c6, 0x4068,
                        (ctypes.c_ubyte * 8)(0x9e, 0x3a, 0x58, 0x9e, 0x2b, 0xab, 0x6d, 0x57))
TRAY_GUID_FROZEN = GUID(0xc53e97c1, 0x1899, 0x4a8a,
                        (ctypes.c_ubyte * 8)(0x9b, 0x21, 0x8d, 0xef, 0xec, 0xc6, 0xe8, 0x9c))
TRAY_GUID = TRAY_GUID_FROZEN if FROZEN else TRAY_GUID_SOURCE


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("style", wt.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON), ("hCursor", HCURSOR),
                ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR), ("hIconSm", wt.HICON)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT),
                ("uFlags", wt.UINT), ("uCallbackMessage", wt.UINT),
                ("hIcon", wt.HICON), ("szTip", wt.WCHAR * 128),
                ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD),
                ("szInfo", wt.WCHAR * 256), ("uVersion", wt.UINT),
                ("szInfoTitle", wt.WCHAR * 64), ("dwInfoFlags", wt.DWORD),
                ("guidItem", GUID), ("hBalloonIcon", wt.HICON)]


for _f, _args, _ret in [
    ("DefWindowProcW", [wt.HWND, wt.UINT, WPARAM, LPARAM], LRESULT),
    ("RegisterClassExW", [ctypes.c_void_p], wt.ATOM),
    ("UnregisterClassW", [wt.LPCWSTR, wt.HINSTANCE], wt.BOOL),
    ("CreateWindowExW", [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int,
                         ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU,
                         wt.HINSTANCE, wt.LPVOID], wt.HWND),
    ("DestroyWindow", [wt.HWND], wt.BOOL),
    ("IsWindow", [wt.HWND], wt.BOOL),
    ("GetMessageW", [ctypes.c_void_p, wt.HWND, wt.UINT, wt.UINT], ctypes.c_int),
    ("PeekMessageW", [ctypes.c_void_p, wt.HWND, wt.UINT, wt.UINT, wt.UINT], wt.BOOL),
    ("TranslateMessage", [ctypes.c_void_p], wt.BOOL),
    ("DispatchMessageW", [ctypes.c_void_p], LRESULT),
    ("PostMessageW", [wt.HWND, wt.UINT, WPARAM, LPARAM], wt.BOOL),
    ("PostQuitMessage", [ctypes.c_int], None),
    ("RegisterHotKey", [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT], wt.BOOL),
    ("UnregisterHotKey", [wt.HWND, ctypes.c_int], wt.BOOL),
    ("RegisterWindowMessageW", [wt.LPCWSTR], wt.UINT),
    ("FindWindowW", [wt.LPCWSTR, wt.LPCWSTR], wt.HWND),
    ("GetWindowThreadProcessId", [wt.HWND, ctypes.POINTER(wt.DWORD)], wt.DWORD),
    ("AllowSetForegroundWindow", [wt.DWORD], wt.BOOL),
    ("CreatePopupMenu", [], wt.HMENU),
    ("DestroyMenu", [wt.HMENU], wt.BOOL),
    ("AppendMenuW", [wt.HMENU, wt.UINT, WPARAM, wt.LPCWSTR], wt.BOOL),
    ("TrackPopupMenu", [wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                        wt.HWND, ctypes.c_void_p], wt.BOOL),
    ("SetForegroundWindow", [wt.HWND], wt.BOOL),
    ("GetForegroundWindow", [], wt.HWND),
    ("GetClassNameW", [wt.HWND, wt.LPWSTR, ctypes.c_int], ctypes.c_int),
    ("GetCursorPos", [ctypes.POINTER(wt.POINT)], wt.BOOL),
    ("GetKeyState", [ctypes.c_int], ctypes.c_short),
    ("SetTimer", [wt.HWND, ctypes.c_size_t, wt.UINT, ctypes.c_void_p], ctypes.c_size_t),
    ("KillTimer", [wt.HWND, ctypes.c_size_t], wt.BOOL),
    ("LoadImageW", [wt.HINSTANCE, wt.LPCWSTR, wt.UINT, ctypes.c_int, ctypes.c_int,
                    wt.UINT], wt.HANDLE),
    ("LoadIconW", [wt.HINSTANCE, wt.LPCWSTR], wt.HICON),
    ("DestroyIcon", [wt.HICON], wt.BOOL),
    ("SendInput", [wt.UINT, ctypes.c_void_p, ctypes.c_int], wt.UINT),
    ("ShutdownBlockReasonCreate", [wt.HWND, wt.LPCWSTR], wt.BOOL),
    ("ShutdownBlockReasonDestroy", [wt.HWND], wt.BOOL),
    ("SystemParametersInfoW", [wt.UINT, wt.UINT, ctypes.c_void_p, wt.UINT], wt.BOOL),
    ("SetWinEventHook", [wt.DWORD, wt.DWORD, wt.HMODULE, WINEVENTPROC, wt.DWORD,
                         wt.DWORD, wt.DWORD], wt.HANDLE),
    ("UnhookWinEvent", [wt.HANDLE], wt.BOOL),
    ("MessageBoxW", [wt.HWND, wt.LPCWSTR, wt.LPCWSTR, wt.UINT], ctypes.c_int),
]:
    fn = getattr(_u32, _f)
    fn.argtypes = _args
    fn.restype = _ret

_shell.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
_shell.Shell_NotifyIconW.restype = wt.BOOL

# WNDPROC thunks handed to RegisterClassExW must outlive the window class.
_KEEPALIVE = []


def _def_window_proc(h, m, w, l):
    return _u32.DefWindowProcW(h, m, w, l)


_DPI_DONE = False


def set_dpi_aware():
    """Make the process system-DPI-aware (the mode Tk 8.6 handles correctly).

    Must run before ANY window exists in the process -- in particular before
    tk.Tk(), which fixes its scaling from the DPI it sees at creation time.
    """
    global _DPI_DONE
    if _DPI_DONE:
        return
    _DPI_DONE = True
    try:
        fn = _u32.SetProcessDpiAwarenessContext
        fn.restype = wt.BOOL
        fn.argtypes = [ctypes.c_void_p]
        if fn(ctypes.c_void_p(-2)):          # DPI_AWARENESS_CONTEXT_SYSTEM_AWARE
            return
    except Exception:
        pass
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(1)   # PROCESS_SYSTEM_DPI_AWARE
        return
    except Exception:
        pass
    try:
        _u32.SetProcessDPIAware()
    except Exception:
        pass


def work_area():
    """Primary monitor work area (left, top, right, bottom) or None."""
    r = wt.RECT()
    if _u32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(r), 0):
        return (r.left, r.top, r.right, r.bottom)
    return None


def key_down(vk):
    return (_u32.GetKeyState(vk) & 0x8000) != 0


def foreground_desc():
    """'<window class> pid=<n>' of the foreground window, for the log only
    (spots third-party pop-ups, e.g. a vendor's mouse utility reacting to the
    device coming and going, after a toggle). Window titles are deliberately
    not logged."""
    try:
        h = _u32.GetForegroundWindow()
        if not h:
            return "none"
        buf = ctypes.create_unicode_buffer(256)
        _u32.GetClassNameW(h, buf, 256)
        pid = wt.DWORD(0)
        _u32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        return "%s pid=%d" % (buf.value or "?", pid.value)
    except Exception:
        return "?"


# Shell windows that hold the foreground while the tray is used (taskbar,
# hidden-icons flyout, shell flyouts). The menu never hands focus back to them.
SHELL_FG_CLASSES = frozenset((
    "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "NotifyIconOverflowWindow",
    "TopLevelWindowForOverflowXamlIsland", "XamlExplorerHostIslandWindow",
    "Windows.UI.Core.CoreWindow", "Progman", "WorkerW"))


def window_class(h):
    buf = ctypes.create_unicode_buffer(256)
    try:
        _u32.GetClassNameW(h, buf, 256)
    except Exception:
        return ""
    return buf.value


def is_own_window(h):
    """True if window h belongs to this process (hidden window, Tk root, ...)."""
    pid = wt.DWORD(0)
    try:
        _u32.GetWindowThreadProcessId(h, ctypes.byref(pid))
    except Exception:
        return False
    return pid.value == os.getpid()


def tray_event_action(evt, v4):
    """What one tray callback event does: 'menu', 'settings' or None.

    Under NOTIFYICON_VERSION_4 the shell sends WM_RBUTTONUP AND WM_CONTEXTMENU
    for one right-click, so the menu opens on WM_CONTEXTMENU only (it is also
    what the keyboard sends). Balloon and popup notifications never open
    anything -- NIN_BALLOONHIDE in particular must not be mistaken for a key.
    """
    if evt == WM_CONTEXTMENU:
        return "menu" if v4 else None
    if evt == WM_RBUTTONUP:
        return None if v4 else "menu"
    if evt in (WM_LBUTTONDBLCLK, NIN_KEYSELECT):
        return "settings"
    return None


# ---- F24 input-injection self-test (the only injection the tests allow) ----
class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
VK_F24_CODE = 0x87


def selftest_hotkey_f24(max_attempts=3):
    """Register plain F24, inject F24 down/up, wait <=1 s for WM_HOTKEY.

    F24 is consumed by the registered hotkey, so no application sees it.
    max_attempts is capped at 3. Returns (ok, detail). Creates a hidden
    window, never shown, and unregisters its class afterwards.
    """
    max_attempts = max(1, min(3, int(max_attempts)))
    hinst = _k32.GetModuleHandleW(None)
    cls_name = "InputToggleHKTest"
    proc_ref = WNDPROC(_def_window_proc)
    _KEEPALIVE.append(proc_ref)
    cls = WNDCLASSEXW(cbSize=ctypes.sizeof(WNDCLASSEXW), lpfnWndProc=proc_ref,
                      hInstance=hinst, lpszClassName=cls_name)
    if not _u32.RegisterClassExW(ctypes.byref(cls)):
        return False, "RegisterClassExW failed gle=%d" % ctypes.get_last_error()
    hwnd = None
    try:
        hwnd = _u32.CreateWindowExW(0, cls_name, "hk", 0, 0, 0, 0, 0,
                                    None, None, hinst, None)
        if not hwnd:
            return False, "CreateWindowExW failed gle=%d" % ctypes.get_last_error()
        for attempt in range(1, max_attempts + 1):
            if not _u32.RegisterHotKey(hwnd, HOTKEY_ID, MOD_NOREPEAT, VK_F24_CODE):
                gle = ctypes.get_last_error()
                time.sleep(0.1)
                if attempt == max_attempts:
                    return False, "RegisterHotKey(F24) failed gle=%d" % gle
                continue
            try:
                down = INPUT(type=INPUT_KEYBOARD)
                down.ki = KEYBDINPUT(wVk=VK_F24_CODE)
                up = INPUT(type=INPUT_KEYBOARD)
                up.ki = KEYBDINPUT(wVk=VK_F24_CODE, dwFlags=KEYEVENTF_KEYUP)
                arr = (INPUT * 2)(down, up)
                sent = _u32.SendInput(2, arr, ctypes.sizeof(INPUT))
                if sent != 2:
                    return False, "SendInput sent %d/2 gle=%d" % (sent, ctypes.get_last_error())
                msg = wt.MSG()
                deadline = time.monotonic() + 1.0
                got = False
                while time.monotonic() < deadline:
                    if _u32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):  # PM_REMOVE
                        if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                            got = True
                            break
                        _u32.TranslateMessage(ctypes.byref(msg))
                        _u32.DispatchMessageW(ctypes.byref(msg))
                    else:
                        time.sleep(0.01)
                if got:
                    return True, "WM_HOTKEY received for F24 on attempt %d" % attempt
                if attempt == max_attempts:
                    return False, "no WM_HOTKEY within 1 s after %d attempt(s)" % max_attempts
            finally:
                _u32.UnregisterHotKey(hwnd, HOTKEY_ID)
    finally:
        if hwnd:
            _u32.DestroyWindow(hwnd)
        _u32.UnregisterClassW(cls_name, hinst)
    return False, "exhausted attempts"


class Win32App(object):
    """Hidden window owning the tray icon + global hotkey + popup menu."""

    def __init__(self, ctl):
        self.ctl = ctl
        self.hwnd = None
        self.hinst = None
        self._wndproc = None            # keep the WNDPROC alive
        self.icon_on = None
        self.icon_off = None
        self.hicon_on = None
        self.hicon_off = None
        self.tray_added = False
        self.v4 = False
        self.use_guid = True
        self.taskbar_created_msg = 0
        self.cur_state = "unknown"
        self.cur_label = ""
        self.hotkey_ok = False
        self.hotkey_error = ""
        self.hotkey_suspended = False
        self._in_menu = False
        self._retrying = False
        self._ended = False
        self._app_fg = None          # last application window in the foreground
        self._fg_proc = None
        self._fg_hook = None

    # ---- lifecycle -----------------------------------------------------
    def run(self):
        self.hinst = _k32.GetModuleHandleW(None)
        self._wndproc = WNDPROC(self._wnd_proc)
        self.taskbar_created_msg = _u32.RegisterWindowMessageW("TaskbarCreated")
        cls = WNDCLASSEXW(cbSize=ctypes.sizeof(WNDCLASSEXW), lpfnWndProc=self._wndproc,
                          hInstance=self.hinst, lpszClassName=WND_CLASS)
        if not _u32.RegisterClassExW(ctypes.byref(cls)):
            raise OSError("RegisterClassExW failed gle=%d" % ctypes.get_last_error())
        self.hwnd = _u32.CreateWindowExW(0, WND_CLASS, WND_TITLE, 0, 0, 0, 0, 0,
                                         None, None, self.hinst, None)
        if not self.hwnd:
            raise OSError("CreateWindowExW failed gle=%d" % ctypes.get_last_error())
        self._load_icons()
        # Track the last application window in the foreground (out-of-context,
        # delivered through this thread's message loop) so the tray menu can
        # give the focus back to it instead of to the taskbar.
        self._fg_proc = WINEVENTPROC(self._on_foreground)
        self._fg_hook = _u32.SetWinEventHook(
            EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, None, self._fg_proc,
            0, 0, WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS)
        if not self._fg_hook:
            log().warning("SetWinEventHook failed; the menu will not restore focus")
        self._on_foreground(None, EVENT_SYSTEM_FOREGROUND,
                            _u32.GetForegroundWindow(), OBJID_WINDOW, 0, 0, 0)
        # hotkey first, so the very first tooltip already says whether it works
        self.register_hotkey(self.ctl.get_settings().get("hotkey"))
        self.add_tray()
        log().info("win32 ready hwnd=%s hotkey_ok=%s tray=%s v4=%s guid=%s",
                   self.hwnd, self.hotkey_ok, self.tray_added, self.v4, self.use_guid)
        self.ctl.on_win32_ready()
        self._pump()

    def _pump(self):
        msg = wt.MSG()
        while True:
            r = _u32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r == 0 or r == -1:
                break
            _u32.TranslateMessage(ctypes.byref(msg))
            _u32.DispatchMessageW(ctypes.byref(msg))

    def post(self, msg, wparam=0, lparam=0):
        h = self.hwnd
        if h:
            return bool(_u32.PostMessageW(h, msg, wparam, lparam))
        return False

    def stop(self):
        self.post(MSG_QUIT)

    def _on_foreground(self, _hook, _event, hwnd, id_object, _child, _thread, _time):
        try:
            if (hwnd and id_object == OBJID_WINDOW and hwnd != self.hwnd
                    and not is_own_window(hwnd)
                    and window_class(hwnd) not in SHELL_FG_CLASSES):
                self._app_fg = hwnd
        except Exception:
            pass

    def _teardown(self):
        """MSG_QUIT: remove the icon and hotkey, then end the message loop."""
        self.remove_tray()
        if self._fg_hook:
            _u32.UnhookWinEvent(self._fg_hook)
            self._fg_hook = None
        hwnd = self.hwnd
        if hwnd:
            _u32.UnregisterHotKey(hwnd, HOTKEY_ID)
            self.hwnd = None
            _u32.DestroyWindow(hwnd)          # -> WM_DESTROY -> PostQuitMessage

    # ---- icons ---------------------------------------------------------
    def _load_icons(self):
        self.icon_on, self.icon_off = ensure_icons()

        def load(path):
            if path:
                h = _u32.LoadImageW(None, path, IMAGE_ICON, 0, 0,
                                    LR_LOADFROMFILE | LR_DEFAULTSIZE)
                if h:
                    return h
            return _u32.LoadIconW(None, ctypes.c_wchar_p(IDI_APPLICATION))
        self.hicon_on = load(self.icon_on)
        self.hicon_off = load(self.icon_off)

    def _icon_for_state(self, state):
        return self.hicon_off if state == "disabled" else self.hicon_on

    # ---- tray ----------------------------------------------------------
    def _nid(self, flags=0, hwnd=None):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = hwnd or self.hwnd
        nid.uID = 1
        nid.uFlags = flags
        if self.use_guid:
            nid.uFlags |= NIF_GUID
            nid.guidItem = TRAY_GUID
        return nid

    def _refresh_state(self):
        try:
            snap = self.ctl.selection_snapshot()
            self.cur_state, self.cur_label = snap["state"], snap["label"]
        except Exception:
            log().exception("state refresh failed")
            self.cur_state = "unknown"

    def _full_nid(self):
        nid = self._nid(NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP)
        nid.uCallbackMessage = WM_TRAYCB
        nid.hIcon = self._icon_for_state(self.cur_state)
        nid.szTip = self._tooltip()
        return nid

    def add_tray(self):
        """NIM_ADD with fallbacks: a stale icon with our GUID (after a crash)
        is deleted and re-added; a GUID Windows refuses falls back to uID; an
        add that Explorer reported as failed but performed (timeout at logon)
        is detected with NIM_MODIFY; anything else retries every 3 s."""
        if not self.hwnd:
            return False
        self._refresh_state()
        ok = bool(_shell.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._full_nid())))
        if not ok and self.use_guid:
            _shell.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid()))
            ok = bool(_shell.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._full_nid())))
            if not ok:
                log().warning("tray: NIM_ADD with GUID failed; using hWnd+uID")
                self.use_guid = False
                ok = bool(_shell.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._full_nid())))
        if not ok:
            ok = bool(_shell.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._full_nid())))
        if not ok:
            log().warning("tray: Shell_NotifyIcon ADD failed; retrying every 3 s")
            self._start_retry()
            return False
        self.tray_added = True
        self._stop_retry()
        nid = self._nid()
        nid.uVersion = NOTIFYICON_VERSION_4
        self.v4 = bool(_shell.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(nid)))
        if not self.v4:
            log().warning("tray: NIM_SETVERSION 4 failed; using classic mouse messages")
        return True

    def _start_retry(self):
        if not self._retrying and self.hwnd:
            self._retrying = bool(_u32.SetTimer(self.hwnd, TRAY_RETRY_ID, 3000, None))

    def _stop_retry(self):
        if self._retrying and self.hwnd:
            _u32.KillTimer(self.hwnd, TRAY_RETRY_ID)
        self._retrying = False

    def remove_tray(self):
        self._stop_retry()
        if self.tray_added and self.hwnd:
            _shell.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid()))
        self.tray_added = False

    def _tooltip(self):
        hk = format_hotkey(self.ctl.get_settings().get("hotkey"))
        return build_tooltip(self.cur_state, self.cur_label, hk, self.hotkey_ok,
                             self.ctl.helper_ok, self.ctl.is_busy(), self.hotkey_error)

    def update_tray(self, balloon=None, warning=False):
        if not self.hwnd:
            return
        if not self.tray_added:
            if not self.add_tray() or not balloon:
                return
        else:
            self._refresh_state()
        nid = self._nid(NIF_ICON | NIF_TIP | NIF_SHOWTIP)
        nid.hIcon = self._icon_for_state(self.cur_state)
        nid.szTip = self._tooltip()
        s = self.ctl.get_settings()
        if balloon and s.get("show_notifications"):
            # balloons (toasts on Windows 11) never take focus
            nid.uFlags |= NIF_INFO
            nid.szInfo = balloon[:255]
            nid.szInfoTitle = "InputToggle"
            flags = NIIF_WARNING if warning else NIIF_INFO
            if not s.get("play_sound"):
                flags |= NIIF_NOSOUND
            nid.dwInfoFlags = flags
        if not _shell.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid)):
            log().warning("tray: NIM_MODIFY failed; re-adding the icon")
            self.tray_added = False
            self.add_tray()

    # ---- hotkey --------------------------------------------------------
    def register_hotkey(self, hk):
        _u32.UnregisterHotKey(self.hwnd, HOTKEY_ID)
        ok, reason = validate_hotkey(hk)
        if not ok:
            log().warning("hotkey invalid: %s", reason)
            self.hotkey_ok = False
            self.hotkey_error = "invalid combination"
            return False, reason
        mods, vk = hotkey_win32(hk)
        if _u32.RegisterHotKey(self.hwnd, HOTKEY_ID, mods, vk):
            self.hotkey_ok = True
            self.hotkey_error = ""
            return True, ""
        gle = ctypes.get_last_error()
        log().warning("RegisterHotKey(%s) failed gle=%d", format_hotkey(hk), gle)
        self.hotkey_ok = False
        self.hotkey_error = "used by another program"
        return False, "in use by another program (gle=%d)" % gle

    def _on_reregister(self, seq):
        if seq:
            req = self.ctl.take_rereg(seq)
            if req is None:
                log().info("hotkey re-register #%d was abandoned; ignored", seq)
                return
            ok, reason = self.register_hotkey(req["new"])
            if not ok:
                self.register_hotkey(req["old"])   # keep the previous one
            req["ok"] = ok
            req["reason"] = reason
            req["event"].set()
        else:
            self.register_hotkey(self.ctl.get_settings().get("hotkey"))
        self.update_tray()

    # ---- menu ----------------------------------------------------------
    def show_menu(self, x=None, y=None):
        if self._in_menu:                       # no re-entrant TrackPopupMenu
            return
        self._in_menu = True
        try:
            self._show_menu(x, y)
        finally:
            self._in_menu = False

    def _show_menu(self, x, y):
        hwnd = self.hwnd
        if not hwnd:
            return
        self._refresh_state()
        busy = self.ctl.is_busy()
        label, action, grayed = menu_toggle_spec(self.cur_state, busy)
        hmenu = _u32.CreatePopupMenu()
        if not hmenu:
            return
        cmd = 0
        try:
            _u32.AppendMenuW(hmenu, MF_STRING | MF_GRAYED, IDM_STATUS,
                             status_text(self.cur_state, self.cur_label, busy,
                                         self.ctl.helper_ok))
            if not self.hotkey_ok:
                hk = format_hotkey(self.ctl.get_settings().get("hotkey"))
                _u32.AppendMenuW(hmenu, MF_STRING | MF_GRAYED, IDM_STATUS2,
                                 "Hotkey %s not active (%s)" % (
                                     hk, self.hotkey_error or "used by another program"))
            _u32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
            _u32.AppendMenuW(hmenu, MF_STRING | (MF_GRAYED if grayed else 0),
                             IDM_TOGGLE, label)
            _u32.AppendMenuW(hmenu, MF_STRING | (MF_GRAYED if busy else 0),
                             IDM_ENABLE_ALL, "Re-enable everything InputToggle disabled")
            _u32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
            _u32.AppendMenuW(hmenu, MF_STRING, IDM_SETTINGS, "Settings...")
            _u32.AppendMenuW(hmenu, MF_STRING, IDM_LOGS, "Open log folder")
            _u32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
            _u32.AppendMenuW(hmenu, MF_STRING, IDM_EXIT, "Exit")
            if x is None or y is None:
                pt = wt.POINT()
                _u32.GetCursorPos(ctypes.byref(pt))
                x, y = pt.x, pt.y
            # the foreground right now is the taskbar or the hidden-icons
            # flyout (the click went there); hand focus back to the app instead
            prev = self._app_fg
            _u32.SetForegroundWindow(hwnd)      # per TrackPopupMenu remarks
            cmd = _u32.TrackPopupMenu(hmenu, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
                                      x, y, 0, hwnd, None)
            _u32.PostMessageW(hwnd, WM_NULL, 0, 0)  # per remarks
            # give the foreground back unless the choice opens a window itself
            if (cmd in (0, IDM_TOGGLE, IDM_ENABLE_ALL, IDM_EXIT) and prev
                    and prev != hwnd and _u32.IsWindow(prev)):
                _u32.SetForegroundWindow(prev)
        finally:
            _u32.DestroyMenu(hmenu)
        if cmd == IDM_TOGGLE:
            self.ctl.trigger_toggle(action)     # exactly what the label said
        elif cmd == IDM_ENABLE_ALL:
            self.ctl.trigger_enable_all()
        elif cmd == IDM_SETTINGS:
            self.ctl.request_open_settings()
        elif cmd == IDM_LOGS:
            self.ctl.open_log_folder()
        elif cmd == IDM_EXIT:
            self.ctl.request_quit()

    # ---- results -------------------------------------------------------
    def _on_results(self):
        last = None
        for msg, warning in self.ctl.take_results():
            if msg:
                last = (msg, warning)
        if last:
            self.update_tray(balloon=last[0], warning=last[1])
        else:
            self.update_tray()

    # ---- session end ---------------------------------------------------
    def _on_end_session(self, lparam):
        if self._ended:
            return
        self._ended = True
        log().info("WM_ENDSESSION lParam=0x%X", lparam & 0xFFFFFFFF)
        blocked = False
        try:
            if self.ctl.get_settings().get("reenable_on_exit") and self.hwnd:
                # only labels us in the shutdown screen if we are slow
                blocked = bool(_u32.ShutdownBlockReasonCreate(
                    self.hwnd, "Re-enabling input devices"))
            self.ctl.on_end_session_sync()
        finally:
            if blocked and self.hwnd:
                _u32.ShutdownBlockReasonDestroy(self.hwnd)
        if lparam & ENDSESSION_CLOSEAPP:
            # Restart Manager closes us but the session goes on: really exit
            self.ctl.request_quit()
        else:
            self.remove_tray()

    # ---- window procedure ---------------------------------------------
    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAYCB:
                evt = lparam & 0xFFFF
                act = tray_event_action(evt, self.v4)
                if act == "menu":
                    if self.v4:
                        # v4: wParam carries the anchor (icon for keyboard,
                        # cursor for mouse) in screen coordinates
                        self.show_menu(signed16(wparam & 0xFFFF),
                                       signed16((wparam >> 16) & 0xFFFF))
                    else:
                        self.show_menu()
                elif act == "settings":
                    self.ctl.request_open_settings()
                return 0
            if msg == WM_HOTKEY:
                if wparam == HOTKEY_ID:
                    self.ctl.trigger_toggle(None)
                return 0
            if msg == MSG_OPEN_SETTINGS:
                self.ctl.request_open_settings()
                return 0
            if msg == MSG_REREGISTER:
                self._on_reregister(int(wparam))
                return 0
            if msg == MSG_HOTKEY_SUSPEND:
                if wparam:
                    _u32.UnregisterHotKey(hwnd, HOTKEY_ID)
                    self.hotkey_suspended = True
                elif self.hotkey_suspended:
                    self.hotkey_suspended = False
                    self.register_hotkey(self.ctl.get_settings().get("hotkey"))
                    self.update_tray()
                return 0
            if msg == MSG_REFRESH:
                self.update_tray()
                return 0
            if msg == MSG_TOGGLE:
                self.ctl.trigger_toggle(None)
                return 0
            if msg == MSG_ENABLE_ALL:
                self.ctl.trigger_enable_all()
                return 0
            if msg == MSG_TOGGLE_DONE:
                self._on_results()
                return 0
            if msg == MSG_REQUEST_QUIT:
                self.ctl.request_quit()
                return 0
            if msg == MSG_QUIT:
                self._teardown()
                return 0
            if msg == WM_TIMER:
                if wparam == TRAY_RETRY_ID:
                    if self.tray_added:
                        self._stop_retry()
                    else:
                        self.add_tray()
                return 0
            if msg == WM_DEVICECHANGE:
                if wparam == DBT_DEVNODES_CHANGED:
                    self.update_tray()
                    self.ctl.notify_devices_changed()
                return 1
            if msg == WM_POWERBROADCAST:
                if wparam in (PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND):
                    self.update_tray()
                return 1
            if msg == WM_QUERYENDSESSION:
                return 1
            if msg == WM_ENDSESSION:
                if wparam:
                    self._on_end_session(lparam)
                return 0
            if msg == WM_CLOSE:
                # e.g. taskkill without /f: exit cleanly instead of letting
                # DefWindowProc destroy the window and orphan the tray icon
                self.ctl.request_quit()
                return 0
            if msg == WM_DESTROY:
                _u32.PostQuitMessage(0)
                return 0
            if self.taskbar_created_msg and msg == self.taskbar_created_msg:
                # Explorer restarted: our icon is gone; delete any leftover first
                self.tray_added = False
                _shell.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid(hwnd=hwnd)))
                self.add_tray()
                return 0
        except Exception:
            log().exception("wndproc error msg=0x%X", msg)
        return _u32.DefWindowProcW(hwnd, msg, wparam, lparam)


# ========================================================================
# CRD process detection (CreateToolhelp32Snapshot)
# ========================================================================
TH32CS_SNAPPROCESS = 0x00000002


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                ("th32ProcessID", wt.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wt.DWORD), ("szExeFile", wt.WCHAR * 260)]


_k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
_k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
_k32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
_k32.Process32FirstW.restype = wt.BOOL
_k32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
_k32.Process32NextW.restype = wt.BOOL
_INVALID_HANDLE = 0xFFFFFFFFFFFFFFFF


def find_process_pids(name):
    """frozenset of PIDs whose exe name matches, or None if the snapshot
    failed (an API failure must not look like 'not running')."""
    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == _INVALID_HANDLE:
        return None
    pids = set()
    try:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        low = name.lower()
        if not _k32.Process32FirstW(snap, ctypes.byref(pe)):
            return frozenset() if ctypes.get_last_error() == ERROR_NO_MORE_FILES else None
        while True:
            if pe.szExeFile.lower() == low:
                pids.add(int(pe.th32ProcessID))
            if not _k32.Process32NextW(snap, ctypes.byref(pe)):
                if ctypes.get_last_error() != ERROR_NO_MORE_FILES:
                    return None
                break
    finally:
        _k32.CloseHandle(snap)
    return frozenset(pids)


def process_running(name):
    """True / False, or None when the process list could not be read."""
    pids = find_process_pids(name)
    return None if pids is None else bool(pids)


# ========================================================================
# Start-with-Windows (HKCU Run). The tests never touch the registry: they use
# the pure helpers and inject fakes into sync_run_on_logon.
# ========================================================================
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def pythonw_exe():
    d = os.path.dirname(sys.executable)
    cand = os.path.join(d, "pythonw.exe")
    return cand if os.path.exists(cand) else sys.executable


def run_on_logon_command(script=None, frozen=None, executable=None):
    """The HKCU Run value that starts this copy at logon:
    '"<...>\\InputToggle.exe"' for the release build, '"<pythonw.exe>"
    "<script>"' when running from source."""
    if FROZEN if frozen is None else frozen:
        return '"%s"' % os.path.abspath(executable or sys.executable)
    script = os.path.abspath(script or __file__)
    return '"%s" "%s"' % (pythonw_exe(), script)


def same_run_command(a, b):
    """Run values name Windows paths: compare them case-insensitively."""
    if a is None or b is None:
        return a is None and b is None
    return os.path.normcase(a.strip()) == os.path.normcase(b.strip())


def get_run_on_logon():
    """The current HKCU Run value named InputToggle, or None."""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_QUERY_VALUE) as k:
            val, _typ = winreg.QueryValueEx(k, APP_NAME)
    except OSError:
        return None
    return val if isinstance(val, str) else None


def set_run_on_logon(enable, script=None):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enable:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, run_on_logon_command(script))
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass


def sync_run_on_logon(want, _get=None, _set=None):
    """Release build, at startup: make the logon entry match the 'Start with
    Windows' setting. The installer always writes the entry for the installed
    exe (replacing one that started an older copy); this removes it again
    when the user had switched the setting off, and repairs an entry that
    points somewhere else. Returns 'set', 'removed' or '' (nothing to do)."""
    get = _get or get_run_on_logon
    put = _set or set_run_on_logon
    cur = get()
    if want:
        if not same_run_command(cur, run_on_logon_command()):
            put(True)
            return "set"
    elif cur is not None:
        put(False)
        return "removed"
    return ""


# ========================================================================
# Settings window (ttk). Built on demand on the withdrawn root.
# ========================================================================
GLYPH_ON = "\u2611"      # checked
GLYPH_OFF = "\u2610"     # unchecked
GLYPH_PART = "\u25a3"    # partially checked group

CRD_HINT = ("Turns the selected devices off within about 2 s of a Chrome Remote "
            "Desktop session starting - even when you are at the desk - and back on "
            "when it ends, unless you switched them yourself during the session. "
            "Works while the PC is locked.")


class SettingsWindow(object):
    def __init__(self, ctl):
        import tkinter as tk
        from tkinter import ttk
        import tkinter.font as tkfont
        self.tk = tk
        self.ttk = ttk
        self.tkfont = tkfont
        self.ctl = ctl
        self.recording = False
        self._rec_bind = None
        self._rec_after = None
        s = ctl.get_settings()
        self.hk = normalize_hotkey(s.get("hotkey"))
        self.checked = set(s.get("devices", []))
        self.item_kind = {}
        self.item_id = {}
        self.group_children = {}
        self.flat = None
        self.layout = {}
        self._confirmed_sig = None

        self.top = tk.Toplevel(ctl.root)
        self.top.withdraw()                       # never shown until show()
        self.top.title("InputToggle Settings")
        self.top.protocol("WM_DELETE_WINDOW", self.on_cancel)
        icon = bundled_icon(APP_ICON_NAME)
        if icon:
            try:
                self.top.iconbitmap(icon)
            except Exception:
                pass
        try:
            self.tk_scaling = float(self.top.tk.call("tk", "scaling"))
        except Exception:
            self.tk_scaling = 96.0 / 72.0
        self.scale = max(1.0, self.tk_scaling / (96.0 / 72.0))

        self.var_ctrl = tk.BooleanVar(value=self.hk["ctrl"])
        self.var_alt = tk.BooleanVar(value=self.hk["alt"])
        self.var_shift = tk.BooleanVar(value=self.hk["shift"])
        self.var_win = tk.BooleanVar(value=self.hk["win"])
        self.var_key = tk.StringVar(value=self.hk["key"] or "M")
        self.var_showdisc = tk.BooleanVar(value=bool(s.get("show_disconnected")))
        self.var_showvirt = tk.BooleanVar(value=False)
        self.var_notify = tk.BooleanVar(value=bool(s.get("show_notifications")))
        self.var_sound = tk.BooleanVar(value=bool(s.get("play_sound")))
        self.var_reexit = tk.BooleanVar(value=bool(s.get("reenable_on_exit")))
        self.var_restart = tk.BooleanVar(value=bool(s.get("reenable_at_startup")))
        self.var_autocrd = tk.BooleanVar(value=bool(s.get("auto_disable_crd")))
        self.var_startup = tk.BooleanVar(value=bool(s.get("start_with_windows")))
        self.var_status = tk.StringVar(value="")

        self._style()
        self._build()
        self.reload_tree()
        self._fit_window()

    def _px(self, n):
        """A 96-DPI pixel size scaled to the current DPI."""
        return int(round(n * self.scale))

    def _style(self):
        try:
            line = int(self.tkfont.nametofont("TkDefaultFont").metrics("linespace"))
        except Exception:
            line = self._px(16)
        self.rowheight = max(self._px(20), int(line * 1.35))
        self.ttk.Style(self.top).configure("IT.Treeview", rowheight=self.rowheight)

    # ---- UI construction ----------------------------------------------
    def _build(self):
        tk, ttk = self.tk, self.ttk
        pad = {"padx": self._px(6), "pady": self._px(3)}
        frm = ttk.Frame(self.top, padding=self._px(10))
        frm.pack(fill="both", expand=True)
        self.frm = frm

        # -- hotkey (top)
        hk = ttk.LabelFrame(frm, text="Global hotkey", padding=self._px(8))
        hk.pack(side="top", fill="x")
        row = ttk.Frame(hk)
        row.pack(fill="x")
        for text, var in (("Ctrl", self.var_ctrl), ("Alt", self.var_alt),
                          ("Shift", self.var_shift), ("Win", self.var_win)):
            ttk.Checkbutton(row, text=text, variable=var,
                            command=self._update_preview).pack(side="left", **pad)
        ttk.Label(row, text="Key:").pack(side="left", padx=(self._px(14), self._px(2)))
        self.cmb = ttk.Combobox(row, textvariable=self.var_key, values=KEY_CHOICES,
                                width=10, state="readonly")
        self.cmb.pack(side="left", **pad)
        self.cmb.bind("<<ComboboxSelected>>", lambda e: self._update_preview())
        self.btn_rec = ttk.Button(row, text="Record...", command=self.on_record)
        self.btn_rec.pack(side="left", **pad)
        self.lbl_preview = ttk.Label(hk, text="", justify="left",
                                     wraplength=self._px(600))
        self.lbl_preview.pack(anchor="w", fill="x", pady=(self._px(6), 0))

        # -- bottom stack, packed BEFORE the device tree so pack can never
        #    clip the Save/Cancel row or the status line (the tree shrinks)
        btns = ttk.Frame(frm)
        btns.pack(side="bottom", fill="x", pady=(self._px(6), 0))
        ttk.Button(btns, text="Cancel", command=self.on_cancel).pack(side="right")
        ttk.Button(btns, text="Save", command=self.on_save).pack(side="right",
                                                                 padx=self._px(6))
        ttk.Label(btns, text="InputToggle %s" % VERSION,
                  foreground="#777777").pack(side="left")
        self.lbl_status = ttk.Label(frm, textvariable=self.var_status,
                                    foreground="#b03030", justify="left",
                                    wraplength=self._px(600))
        self.lbl_status.pack(side="bottom", fill="x", pady=(self._px(6), 0))

        op = ttk.LabelFrame(frm, text="Options", padding=self._px(8))
        op.pack(side="bottom", fill="x", pady=(self._px(8), 0))
        ttk.Checkbutton(op, text="Show notifications",
                        variable=self.var_notify).pack(anchor="w")
        ttk.Checkbutton(op, text="Play a sound with notifications",
                        variable=self.var_sound).pack(anchor="w")
        ttk.Checkbutton(op, text="Re-enable disabled devices when InputToggle exits "
                        "(also at sign-out)", variable=self.var_reexit).pack(anchor="w")
        ttk.Checkbutton(op, text="Re-enable disabled devices when InputToggle starts",
                        variable=self.var_restart).pack(anchor="w")
        ttk.Checkbutton(op, text="Auto-disable selected devices while Chrome Remote "
                        "Desktop is connected", variable=self.var_autocrd).pack(anchor="w")
        self.lbl_hint = ttk.Label(op, text=CRD_HINT, foreground="#555555",
                                  justify="left", wraplength=self._px(560))
        self.lbl_hint.pack(anchor="w", padx=(self._px(22), 0), pady=(0, self._px(4)))
        ttk.Checkbutton(op, text="Start with Windows",
                        variable=self.var_startup).pack(anchor="w")

        # -- device tree: takes whatever space is left
        dv = ttk.LabelFrame(frm, text="Devices to switch", padding=self._px(8))
        dv.pack(side="top", fill="both", expand=True, pady=(self._px(8), 0))
        bar = ttk.Frame(dv)
        bar.pack(side="top", fill="x")
        ttk.Button(bar, text="Refresh", command=self.reload_tree).pack(side="left")
        ttk.Checkbutton(bar, text="Show disconnected", variable=self.var_showdisc,
                        command=self.reload_tree).pack(side="left", padx=self._px(10))
        ttk.Checkbutton(bar, text="Show virtual/remote", variable=self.var_showvirt,
                        command=self.reload_tree).pack(side="left")
        cols = ("type", "status", "id")
        vsb = ttk.Scrollbar(dv, orient="vertical")
        vsb.pack(side="right", fill="y")            # packed first: never clipped
        self.tree = ttk.Treeview(dv, columns=cols, show="tree headings", height=10,
                                 style="IT.Treeview", yscrollcommand=vsb.set)
        vsb.configure(command=self.tree.yview)
        self.tree.heading("#0", text="Device")
        self.tree.heading("type", text="Type")
        self.tree.heading("status", text="Status")
        self.tree.heading("id", text="Instance ID")
        self.tree.column("#0", width=self._px(300))
        self.tree.column("type", width=self._px(110), anchor="center")
        self.tree.column("status", width=self._px(100), anchor="center")
        self.tree.column("id", width=self._px(170))
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<ButtonRelease-1>", self._on_tree_click)
        self.tree.bind("<space>", self._on_tree_space)

        frm.bind("<Configure>", self._on_resize)
        self._update_preview()

    def _on_resize(self, event):
        wl = max(self._px(200), int(event.width) - self._px(30))
        try:
            self.lbl_status.configure(wraplength=wl)
            self.lbl_preview.configure(wraplength=wl)
            self.lbl_hint.configure(wraplength=max(self._px(160), wl - self._px(40)))
        except Exception:
            pass

    def _fit_window(self):
        """Size the window from what its content requests (no fixed geometry),
        clamped to the work area; only the device tree shrinks."""
        top = self.top
        top.update_idletasks()                     # never maps a withdrawn window
        req_w = top.winfo_reqwidth()
        req_h = top.winfo_reqheight()
        tree_req = self.tree.winfo_reqheight()
        min_tree = self.rowheight * 4 + self._px(28)
        wa = work_area()
        if wa:
            max_w = (wa[2] - wa[0]) - self._px(32)
            max_h = (wa[3] - wa[1]) - self._px(64)    # leave room for the title bar
        else:
            max_w = top.winfo_screenwidth() - self._px(32)
            max_h = top.winfo_screenheight() - self._px(96)
        win_w = min(req_w, max_w)
        win_h = min(req_h, max_h)
        fixed_h = max(0, req_h - tree_req)
        need_h = fixed_h + min(min_tree, tree_req)
        top.minsize(win_w, min(need_h, win_h))
        top.geometry("%dx%d" % (win_w, win_h))
        self.layout = {"req_w": req_w, "req_h": req_h, "win_w": win_w, "win_h": win_h,
                       "fixed_h": fixed_h, "need_h": need_h, "max_w": max_w,
                       "max_h": max_h, "tk_scaling": self.tk_scaling,
                       "fits": bool(req_w <= win_w and need_h <= win_h)}

    def _current_hotkey(self):
        return normalize_hotkey({
            "ctrl": self.var_ctrl.get(), "alt": self.var_alt.get(),
            "shift": self.var_shift.get(), "win": self.var_win.get(),
            "key": self.var_key.get()})

    def _invalidate_confirm(self):
        """Any change to hotkey or selection withdraws a pending confirmation."""
        if self._confirmed_sig is not None:
            self._confirmed_sig = None
            self.var_status.set("")

    def _update_preview(self):
        self._invalidate_confirm()
        hk = self._current_hotkey()
        ok, reason = validate_hotkey(hk)
        txt = "Hotkey: %s" % format_hotkey(hk)
        if not ok:
            txt += "   - " + reason
        else:
            warn = hotkey_warning(hk)
            if warn:
                txt += "   - note: " + warn
        self.lbl_preview.configure(text=txt)

    # ---- record capture ------------------------------------------------
    def on_record(self):
        if self.recording:
            return
        self.recording = True
        # the registered hotkey would swallow its own combination: suspend it
        self.ctl.suspend_hotkey(True)
        self.btn_rec.configure(text="Press keys...")
        self.lbl_preview.configure(
            text="Press the new combination (Esc cancels). If nothing happens, "
                 "another program owns that combination.")
        self._rec_bind = self.top.bind("<KeyPress>", self._on_rec_key)
        self._rec_after = self.top.after(15000, self._rec_timeout)
        self.top.focus_set()

    def _on_rec_key(self, event):
        vk = event.keycode          # on Windows Tk this is the VK code
        # Read the modifiers from user32: this runs on the Tk thread while the
        # key message is being handled, so GetKeyState matches this press.
        status, hk = hotkey_from_record(
            vk, key_down(VK_CONTROL), key_down(VK_MENU), key_down(VK_SHIFT),
            key_down(VK_LWIN) or key_down(VK_RWIN))
        if status == "wait":
            return "break"
        if status == "unsupported":
            self.lbl_preview.configure(
                text="That key is not supported - press another one (Esc cancels).")
            return "break"
        if status == "ok":
            self.var_ctrl.set(hk["ctrl"])
            self.var_alt.set(hk["alt"])
            self.var_shift.set(hk["shift"])
            self.var_win.set(hk["win"])
            self.var_key.set(hk["key"])
        self._stop_record()
        self._update_preview()
        return "break"

    def _rec_timeout(self):
        self._rec_after = None
        if self.recording:
            self._stop_record()
            self._update_preview()

    def _stop_record(self, resume=True):
        if self._rec_after is not None:
            try:
                self.top.after_cancel(self._rec_after)
            except Exception:
                pass
            self._rec_after = None
        if self._rec_bind is not None:
            try:
                self.top.unbind("<KeyPress>", self._rec_bind)
            except Exception:
                pass
            self._rec_bind = None
        was = self.recording
        self.recording = False
        try:
            self.btn_rec.configure(text="Record...")
        except Exception:
            pass
        if was and resume:
            self.ctl.suspend_hotkey(False)

    # ---- device tree ---------------------------------------------------
    def reload_tree(self):
        for i in self.tree.get_children(""):
            self.tree.delete(i)
        self.item_kind.clear()
        self.item_id.clear()
        self.group_children.clear()
        self._invalidate_confirm()                 # the device list may have changed
        try:
            groups = self.ctl.enumerate(self.var_showdisc.get())
            self.flat = flatten(groups)
            if self.var_status.get().startswith("Could not read the device list"):
                self.var_status.set("")
        except Exception as e:
            log().warning("device enumeration failed: %r", e)
            groups = []
            self.flat = None
            self.var_status.set("Could not read the device list (%s). Click Refresh "
                                "to try again." % (e,))
        show_virt = self.var_showvirt.get()
        for g in groups:
            devs = [d for d in g["devices"] if show_virt or not d.get("virtual")]
            if not devs:
                continue
            gi = self.tree.insert("", "end", text="%s  %s" % (GLYPH_OFF, g["name"]),
                                  values=(g["type"], g["status"], g["key"]), open=True)
            self.item_kind[gi] = "group"
            kids = []
            for d in devs:
                if d.get("present"):
                    status = "ON" if d.get("enabled") else "OFF"
                else:
                    status = "not connected"
                ci = self.tree.insert(gi, "end",
                                      text="%s  %s" % (GLYPH_OFF, child_label(d)),
                                      values=(d.get("class_name", "?"), status, d["id"]))
                self.item_kind[ci] = "device"
                self.item_id[ci] = d["id"]
                kids.append(ci)
            self.group_children[gi] = kids
        self._refresh_glyphs()

    def _set_text_glyph(self, item, glyph):
        txt = self.tree.item(item, "text")
        rest = txt.split("  ", 1)[1] if "  " in txt else txt
        self.tree.item(item, text="%s  %s" % (glyph, rest))

    def _refresh_glyphs(self):
        for gi, kids in self.group_children.items():
            for ci in kids:
                on = self.item_id[ci] in self.checked
                self._set_text_glyph(ci, GLYPH_ON if on else GLYPH_OFF)
            states = [self.item_id[ci] in self.checked for ci in kids]
            if kids and all(states):
                self._set_text_glyph(gi, GLYPH_ON)
            elif any(states):
                self._set_text_glyph(gi, GLYPH_PART)
            else:
                self._set_text_glyph(gi, GLYPH_OFF)

    def _toggle_item(self, item):
        kind = self.item_kind.get(item)
        if kind == "device":
            iid = self.item_id[item]
            if iid in self.checked:
                self.checked.discard(iid)
            else:
                self.checked.add(iid)
        elif kind == "group":
            kids = self.group_children.get(item, [])
            ids = [self.item_id[c] for c in kids]
            if all(i in self.checked for i in ids):
                for i in ids:
                    self.checked.discard(i)
            else:
                for i in ids:
                    self.checked.add(i)
        self._refresh_glyphs()
        self._invalidate_confirm()

    def _on_tree_click(self, event):
        if self.tree.identify_region(event.x, event.y) == "tree":
            # clicking the expand/collapse triangle should not toggle
            if self.tree.identify_element(event.x, event.y) == "Treeitem.indicator":
                return
        row = self.tree.identify_row(event.y)
        if row:
            self._toggle_item(row)

    def _on_tree_space(self, event):
        sel = self.tree.selection()
        if sel:
            self._toggle_item(sel[0])
        return "break"

    # ---- save / cancel -------------------------------------------------
    def on_cancel(self):
        self.close()

    def close(self, resume_hotkey=True):
        self._stop_record(resume=resume_hotkey)
        self.ctl.on_settings_closed(self)
        try:
            self.top.destroy()
        except Exception:
            pass

    def on_save(self):
        self._stop_record()
        hk = self._current_hotkey()
        ok, reason = validate_hotkey(hk)
        if not ok:
            self.var_status.set(reason)
            return
        # inline confirmations (no OS dialog): the same warnings, unchanged,
        # on a second Save mean "yes"
        try:
            flat_now = flatten(self.ctl.enumerate(False))
        except Exception:
            flat_now = None
        warnings = []
        hw = hotkey_warning(hk)
        if hw:
            warnings.append(("hotkey", hw + " Click Save again to confirm."))
        warnings += selection_warnings(flat_now, self.checked, enum_ok=flat_now is not None)
        if warnings:
            sig = (format_hotkey(hk), tuple(sorted(self.checked)),
                   tuple(code for code, _ in warnings))
            if self._confirmed_sig != sig:
                self._confirmed_sig = sig
                self.var_status.set("\n\n".join(text for _, text in warnings))
                return
        s = self.ctl.get_settings()
        prev_startup = bool(s.get("start_with_windows"))
        new = dict(s)
        new["hotkey"] = hk
        new["devices"] = sorted(self.checked)
        new["show_disconnected"] = bool(self.var_showdisc.get())
        new["show_notifications"] = bool(self.var_notify.get())
        new["play_sound"] = bool(self.var_sound.get())
        new["reenable_on_exit"] = bool(self.var_reexit.get())
        new["reenable_at_startup"] = bool(self.var_restart.get())
        new["auto_disable_crd"] = bool(self.var_autocrd.get())
        new["start_with_windows"] = bool(self.var_startup.get())
        new["first_run_done"] = True
        applied = self.ctl.apply_settings(new, prev_startup)
        if applied is not True:
            self.var_status.set(str(applied))     # e.g. hotkey in use
            return
        self.ctl.on_settings_closed(self)
        try:
            self.top.destroy()
        except Exception:
            pass

    def show(self):
        """Only ever called because the user asked for Settings."""
        self.top.deiconify()
        self.top.lift()
        self.top.focus_force()


# ========================================================================
# Controller: main-thread Tk orchestration + worker dispatch + CRD poll.
# ========================================================================
class Controller(object):
    def __init__(self, open_settings_on_start=False):
        import tkinter as tk
        self.tk = tk
        self._init_runtime(open_settings_on_start)
        self._startup_fg = _u32.GetForegroundWindow()   # given back below
        self.root = tk.Tk()
        self.root.withdraw()                       # never shown
        self._give_back_startup_focus()
        try:
            self.root.title("InputToggle")
        except Exception:
            pass
        install_exception_hooks(self.root)
        # WM_CLOSE to the hidden root (e.g. taskkill without /f): clean exit
        self.root.protocol("WM_DELETE_WINDOW", self._shutdown)
        self.app = Win32App(self)
        self._first_run_init()

    def _init_runtime(self, open_settings_on_start=False):
        """All state except Tk and the Win32 window (unit tests build a
        Controller with only this and a fake app)."""
        self.settings = load_settings()
        self.first_run = not self.settings.get("first_run_done")
        self.open_settings_on_start = open_settings_on_start
        self.q = _queue.Queue()
        self.app = None
        self._win_thread = None
        self._crd_thread = None
        self._stop = threading.Event()
        self._op_lock = threading.Lock()
        self._busy_lock = threading.Lock()
        self._inflight = 0
        self._results = collections.deque()
        self._results_lock = threading.Lock()
        self._rereg_lock = threading.Lock()
        self._rereg_seq = 0
        self._rereg_reqs = {}
        self.settings_win = None
        self.helper_ok = None                      # None = not checked yet
        self._crd = CrdTracker(end_after=3)
        self._crd_active = False
        self._crd_manual_override = False
        self._startup_done = threading.Event()    # set when the startup work ended
        self._shutting_down = False
        self._finished = False
        self._ended_session = False
        self._exit_thread = None

    def _first_run_init(self):
        """First start of a fresh install: nothing is pre-selected; Settings
        opens once so the user can pick the devices (see first_run_plan)."""
        if self.settings.get("first_run_done"):
            return
        new, open_settings = first_run_plan(self.settings)
        self.settings = migrate_settings(new)
        if open_settings:
            self.open_settings_on_start = True
        try:
            save_settings(self.settings)
        except Exception:
            log().exception("could not save settings")

    # ---- run / shutdown ------------------------------------------------
    def run(self):
        self._win_thread = threading.Thread(target=self._win_main, name="win32",
                                            daemon=True)
        self._win_thread.start()
        self._crd_thread = threading.Thread(target=self._crd_loop, name="crd",
                                            daemon=True)
        self._crd_thread.start()
        if self.open_settings_on_start:
            self.root.after(500, self.request_open_settings)
        self.root.after(0, self._give_back_startup_focus)
        self.root.after(800, self._give_back_startup_focus)
        self.root.after(100, self._drain)
        try:
            self.root.mainloop()
        finally:
            self._stop.set()
            # Wait for the (non-daemon) exit worker here and tear Tk down on
            # the main thread, so the garbage collector never frees Tk objects
            # on a worker thread (Tcl aborts if that happens).
            t = self._exit_thread
            if t is not None and t is not threading.current_thread():
                t.join(20.0)
            try:
                self.root.destroy()
            except Exception:
                pass

    def _give_back_startup_focus(self):
        """On Windows, Tk activates its withdrawn (invisible) root window when it
        is created, so it can take the foreground at every start. Hand the
        foreground straight back to the window the user was in, so starting
        InputToggle never swallows keystrokes."""
        try:
            if self.settings_win is not None:
                return
            fg = _u32.GetForegroundWindow()
            if not fg or not is_own_window(fg):
                return
            prev = getattr(self, "_startup_fg", None)
            if prev and _u32.IsWindow(prev) and not is_own_window(prev):
                _u32.SetForegroundWindow(prev)
                log().info("startup: gave the foreground back to %s", window_class(prev))
        except Exception:
            log().exception("startup focus restore failed")

    def _win_main(self):
        try:
            self.app.run()
        except Exception:
            log().exception("win32 thread crashed")
        finally:
            # the window is gone: end the app through the queue (never touch Tk
            # from this thread)
            self.q.put(("quit",))

    def _drain(self):
        try:
            while True:
                item = self.q.get_nowait()
                try:
                    self._handle(item)
                except Exception:
                    log().exception("queue item %r failed", item[:1])
        except _queue.Empty:
            pass
        if not self._finished:
            self.root.after(120, self._drain)

    def _handle(self, item):
        kind = item[0]
        if kind == "open_settings":
            self._open_settings()
        elif kind == "refresh_tree":
            if self.settings_win is not None:
                try:
                    self.settings_win.reload_tree()
                except Exception:
                    log().exception("tree refresh failed")
        elif kind == "quit":
            self._shutdown()
        elif kind == "exit_ready":
            self._finish_shutdown()

    def _open_settings(self):
        if self._shutting_down:
            return
        if self.settings_win is not None:
            try:
                self.settings_win.show()
                return
            except Exception:
                self.settings_win = None
        try:
            self.settings_win = SettingsWindow(self)
            self.settings_win.show()
        except Exception:
            log().exception("open settings failed")
            self.settings_win = None

    def _shutdown(self):
        """Exit (tray menu, --quit, WM_CLOSE): honour 'Re-enable on exit' and
        wait for an operation in flight before the tray icon goes away."""
        if self._shutting_down:
            return
        self._shutting_down = True
        log().info("shutting down")
        self._stop.set()
        if self.settings_win is not None:
            try:
                self.settings_win.close(resume_hotkey=False)
            except Exception:
                pass
            self.settings_win = None
        t = self.start_worker(self._exit_worker, "exit", daemon=False)
        self._exit_thread = t
        if t is None:
            self._finish_shutdown()
            return
        self.root.after(12000, self._finish_shutdown)     # safety net

    def _exit_worker(self):
        try:
            if self.settings.get("reenable_on_exit") and not self._ended_session:
                res = self._auto_op(lambda: self.do_enable_all(fallback=True))
                log().info("exit re-enable: %s", res.get("outcome"))
            else:
                with self._op_lock:               # let an operation in flight finish
                    pass
        except Exception:
            log().exception("exit re-enable failed")
        finally:
            self.q.put(("exit_ready",))

    def _finish_shutdown(self):
        if self._finished:
            return
        self._finished = True
        try:
            self.app.stop()                       # tray icon + hotkey removed
        except Exception:
            pass
        wt_ = self._win_thread
        if wt_ is not None and wt_ is not threading.current_thread():
            wt_.join(2.0)
        try:
            self.root.quit()
        except Exception:
            pass

    # ---- startup -------------------------------------------------------
    def on_win32_ready(self):
        """Called on the win32 thread once the window, tray and hotkey exist."""
        if self.start_worker(self._startup_worker, "startup") is None:
            log().error("could not start the startup worker")
            self._startup_done.set()

    def _startup_worker(self):
        try:
            self._startup_work()
        finally:
            self._startup_done.set()

    def _startup_work(self):
        if self._stop.wait(1.0):
            return
        notes = []
        warn = False
        try:
            self.helper_ok = apply_task_exists()
        except Exception:
            self.helper_ok = None
        hk_text = format_hotkey(self.settings.get("hotkey"))
        if FROZEN and not is_installed_copy():
            log().info("not the installed copy (%s); logon entry left alone",
                       redact_text(sys.executable))
        elif FROZEN:
            try:
                done = sync_run_on_logon(bool(self.settings.get("start_with_windows")))
                if done:
                    log().info("logon entry %s (Start with Windows = %s)", done,
                               bool(self.settings.get("start_with_windows")))
            except Exception:
                log().exception("could not update the logon entry")
        if self.helper_ok is False:
            notes.append("Helper not installed - %s." % helper_fix_hint())
            warn = True
        if not self.app.hotkey_ok:
            notes.append("Hotkey %s is not active (%s); pick another in Settings."
                         % (hk_text, self.app.hotkey_error or "used by another program"))
            warn = True
        if self.settings.get("reenable_at_startup") and self.helper_ok is not False:
            try:
                res = self._auto_op(lambda: self.do_enable_all(fallback=True))
                msg = outcome_message(res, self.selection_label(), None, "startup")
                if msg:
                    notes.append(msg)
                    warn = warn or not res.get("ok", False)
            except Exception:
                log().exception("startup re-enable failed")
        if self.first_run:
            label = self.selection_label()
            if not self.selected_ids():
                notes.append("InputToggle is running. Tick the devices to switch in "
                             "Settings and click Save; then %s switches them off and on. "
                             "The tray icon may be hidden under ^." % hk_text)
            elif self.app.hotkey_ok:
                notes.append("InputToggle is running. %s switches %s off/on. The icon "
                             "may be hidden under ^ in the taskbar." % (hk_text, label))
            else:
                notes.append("InputToggle is running. Use its tray icon (it may be "
                             "under ^) to switch %s off/on." % label)
        if notes:
            self.post_message(" ".join(notes), warn)
        else:
            self.app.post(MSG_REFRESH)

    # ---- interface used by Win32App / Settings (any thread) ------------
    def get_settings(self):
        return self.settings

    def selected_ids(self):
        return list(self.settings.get("devices", []))

    def selection_snapshot(self):
        return selection_snapshot(self.selected_ids())

    def selection_label(self):
        try:
            return self.selection_snapshot()["label"]
        except Exception:
            return ""

    def is_busy(self):
        with self._busy_lock:
            return self._inflight > 0

    def _op_begin(self, exclusive):
        with self._busy_lock:
            if exclusive and self._inflight > 0:
                return False
            self._inflight += 1
        self.app.post(MSG_REFRESH)                # tooltip: "switching..."
        return True

    def _op_end(self):
        with self._busy_lock:
            self._inflight = max(0, self._inflight - 1)
            idle = self._inflight == 0
        if idle:
            self.app.post(MSG_REFRESH)            # drop the "switching..." tooltip

    def _auto_op(self, fn):
        """Run an automatic operation (startup, exit, CRD, Save) on the
        calling thread, marked in flight so hotkey and menu ignore input."""
        self._op_begin(exclusive=False)
        try:
            return fn()
        finally:
            self._op_end()

    def start_worker(self, fn, name="worker", daemon=True):
        try:
            t = threading.Thread(target=fn, name=name, daemon=daemon)
            t.start()
            return t
        except Exception:                         # e.g. "can't start new thread"
            log().exception("could not start thread %s", name)
            return None

    def trigger_toggle(self, forced):
        """Hotkey (forced=None: decide at run time) or menu (forced action).
        Ignored while ANY operation is in flight."""
        self._trigger_manual(lambda: self.do_toggle(forced), "toggle")

    def trigger_enable_all(self):
        self._trigger_manual(lambda: self.do_enable_all(fallback=True, manual=True),
                             "enable-all")

    def _trigger_manual(self, fn, name):
        if self._shutting_down:
            return
        if not self._op_begin(exclusive=True):
            log().info("%s ignored: an operation is in flight", name)
            return

        def work():
            fg_before = foreground_desc()
            try:
                res = fn()
            except Exception as e:
                log().exception("%s crashed", name)
                res = {"ok": False, "outcome": "error: %r" % (e,), "reason": "error"}
            finally:
                self._op_end()
            self.post_result(res, "manual")
            self._log_foreground(fg_before, res)

        if self.start_worker(work, name) is None:
            self._op_end()
            self.post_message("Could not start a worker thread (low memory?). "
                              "Try again.", True)

    def _log_foreground(self, before, res):
        """After a device actually changed, log the foreground window right
        away and 1.5 s later (vendor software may react when a device it
        watches disappears and comes back)."""
        if not res.get("targets"):
            return
        log().info("foreground before=%s after=%s", before, foreground_desc())
        if not self._stop.wait(1.5):
            log().info("foreground 1.5 s later=%s", foreground_desc())

    def post_result(self, res, source):
        if res.get("task_missing"):
            self.helper_ok = False
        elif res.get("rc") == 0:
            self.helper_ok = True
        hk = format_hotkey(self.settings.get("hotkey")) if self.app.hotkey_ok else None
        msg = outcome_message(res, self.selection_label(), hk, source)
        if msg:
            self.post_message(msg, not res.get("ok", False))
        else:
            self.app.post(MSG_REFRESH)

    def post_message(self, msg, warning=False):
        with self._results_lock:
            self._results.append((msg, bool(warning)))
        self.app.post(MSG_TOGGLE_DONE)

    def take_results(self):
        with self._results_lock:
            out = list(self._results)
            self._results.clear()
        return out

    def request_open_settings(self):
        self.q.put(("open_settings",))

    def request_quit(self):
        self.q.put(("quit",))

    def notify_devices_changed(self):
        self.q.put(("refresh_tree",))

    def open_log_folder(self):
        try:
            os.startfile(data_dir())             # user-initiated from the menu
        except Exception:
            log().exception("open log folder failed")

    def on_settings_closed(self, win):
        if self.settings_win is win:
            self.settings_win = None

    def enumerate(self, show_disconnected):
        return enumerate_devices(show_disconnected)

    def suspend_hotkey(self, suspend):
        self.app.post(MSG_HOTKEY_SUSPEND, 1 if suspend else 0)

    # ---- device operations (serialized by _op_lock) --------------------
    def do_toggle(self, forced):
        with self._op_lock:
            res = toggle_core(self.selected_ids(), forced, auto=False)
            if res.get("targets") and self._crd_active:
                self._crd_manual_override = True
            return res

    def do_enable_all(self, fallback=True, manual=False, **kw):
        with self._op_lock:
            res = enable_all_core(self.selected_ids(), fallback=fallback, **kw)
            if manual and res.get("targets") and self._crd_active:
                self._crd_manual_override = True
            return res

    def _enable_dropped(self, ids):
        """After Save: devices InputToggle switched off that are no longer
        selected would be out of the toggle's reach -- turn them back on."""
        try:
            def go():
                with self._op_lock:
                    return enable_core(ids)
            res = self._auto_op(go)
            self.post_result(res, "save")
        except Exception:
            log().exception("re-enable of deselected devices failed")

    def apply_settings(self, new, prev_startup):
        """Called on the main thread from Settings Save. Returns True or msg."""
        old_hk = self.settings.get("hotkey")
        ok, reason = self._reregister_sync(new.get("hotkey"), old_hk)
        if not ok:
            return "Hotkey not applied: %s" % reason
        self.settings = migrate_settings(new)
        try:
            save_settings(self.settings)
        except Exception as e:
            log().exception("save settings failed")
            return "Could not save settings: %r" % (e,)
        if bool(new.get("start_with_windows")) != bool(prev_startup):
            try:
                set_run_on_logon(bool(new.get("start_with_windows")))
            except Exception:
                log().exception("start-with-windows update failed")
        selected = set(self.settings.get("devices", []))
        dropped = [i for i in tracked_ids(load_state()) if i not in selected]
        if dropped:
            if self.start_worker(lambda: self._enable_dropped(dropped), "save-reenable") is None:
                log().error("could not start the re-enable worker after Save")
        self.app.post(MSG_REFRESH)
        return True

    def _reregister_sync(self, new_hk, old_hk, timeout=3.0):
        if not self.app.hwnd:
            return True, ""      # win32 not up yet (tests); nothing to do
        with self._rereg_lock:
            self._rereg_seq += 1
            seq = self._rereg_seq
            req = {"new": new_hk, "old": old_hk, "event": threading.Event(),
                   "ok": None, "reason": ""}
            self._rereg_reqs[seq] = req
        self.app.post(MSG_REREGISTER, seq)
        req["event"].wait(timeout)
        with self._rereg_lock:
            self._rereg_reqs.pop(seq, None)
        if req["ok"] is None:
            # abandoned: the win32 thread ignores the stale sequence number,
            # and this re-registers the saved (unchanged) hotkey
            self.app.post(MSG_REREGISTER, 0)
            return False, "timeout re-registering hotkey"
        return req["ok"], req["reason"]

    def take_rereg(self, seq):
        with self._rereg_lock:
            return self._rereg_reqs.pop(seq, None)

    def on_end_session_sync(self):
        """WM_ENDSESSION(TRUE), on the win32 thread: the process is terminated
        soon after this returns, so everything here is bounded (~6 s max):
        lock wait <= 1 s, schtasks <= 3 s, poll <= 2 s, no task query, and
        the request is left in place for a late helper run."""
        self._ended_session = True
        self._stop.set()
        if not self.settings.get("reenable_on_exit"):
            return
        got = self._op_lock.acquire(timeout=1.0)
        try:
            res = enable_all_core(self.selected_ids(), fallback=True, timeout=2.0,
                                  run_timeout=3.0, check_missing=False, cleanup=False)
            log().info("end-session re-enable: %s", res.get("outcome"))
        except Exception:
            log().exception("end-session re-enable failed")
        finally:
            if got:
                self._op_lock.release()

    # ---- CRD auto-disable ---------------------------------------------
    def _crd_loop(self):
        # Let "Re-enable at startup" finish first: otherwise it re-enables, a
        # moment later, what auto mode switched off for a session that was
        # already open when InputToggle started (e.g. signing in through CRD).
        deadline = time.monotonic() + STARTUP_GATE_S
        while (not self._startup_done.is_set() and not self._stop.is_set()
               and time.monotonic() < deadline):
            self._startup_done.wait(0.5)
        while not self._stop.is_set():
            try:
                if self.settings.get("auto_disable_crd"):
                    ev = self._crd.step(find_process_pids(CRD_PROCESS))
                    if ev and not self._stop.is_set():
                        log().info("CRD session event: %s", ev)
                        fg_before = foreground_desc()
                        if ev in ("start", "restart"):
                            res = self._auto_op(self._crd_start)
                            source = "crd_start"
                        else:
                            res = self._auto_op(self._crd_end)
                            source = "crd_end"
                        # report after the operation ended (tooltip not "busy")
                        if res is not None:
                            self.post_result(res, source)
                            self._log_foreground(fg_before, res)
                elif self._crd.active:
                    self._crd.reset()
                    self._crd_active = False
            except Exception:
                log().exception("crd loop error")
            self._stop.wait(2.0)

    def _crd_start(self):
        """Session started (or a new one replaced it): switch the selection off.
        Returns the result, or None when nothing was attempted."""
        with self._op_lock:
            if self._stop.is_set():
                return None
            self._crd_active = True
            self._crd_manual_override = False
            return toggle_core(self.selected_ids(), "disable", auto=True)

    def _crd_end(self):
        """Session ended: re-enable what auto mode switched off, unless the
        user switched devices manually during the session."""
        with self._op_lock:
            self._crd_active = False
            if self._crd_manual_override:
                log().info("CRD session ended; devices were switched manually, leaving them")
                return None
            if self._stop.is_set():
                return None
            auto = load_state()["auto_disabled"]
            return enable_core(auto) if auto else None


# ========================================================================
# Self-tests (used by tests/test_smoke.py; none of them changes a device)
# ========================================================================
def selftest_tray():
    """Add a tray icon, pump briefly, remove it. No balloons, no GUID (a GUID
    would be tied to python.exe). Returns (ok, msg)."""
    hinst = _k32.GetModuleHandleW(None)
    cls_name = "InputToggleTrayTest"
    proc_ref = WNDPROC(_def_window_proc)
    _KEEPALIVE.append(proc_ref)
    cls = WNDCLASSEXW(cbSize=ctypes.sizeof(WNDCLASSEXW), lpfnWndProc=proc_ref,
                      hInstance=hinst, lpszClassName=cls_name)
    if not _u32.RegisterClassExW(ctypes.byref(cls)):
        return False, "RegisterClassExW failed gle=%d" % ctypes.get_last_error()
    hwnd = None
    added = False
    nid = NOTIFYICONDATAW()
    try:
        hwnd = _u32.CreateWindowExW(0, cls_name, "tray", 0, 0, 0, 0, 0,
                                    None, None, hinst, None)
        if not hwnd:
            return False, "CreateWindowExW failed gle=%d" % ctypes.get_last_error()
        on, _off = ensure_icons()
        hicon = None
        if on:
            hicon = _u32.LoadImageW(None, on, IMAGE_ICON, 0, 0,
                                    LR_LOADFROMFILE | LR_DEFAULTSIZE)
        if not hicon:
            hicon = _u32.LoadIconW(None, ctypes.c_wchar_p(IDI_APPLICATION))
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP
        nid.uCallbackMessage = WM_TRAYCB
        nid.hIcon = hicon
        nid.szTip = "InputToggle self-test"
        added = bool(_shell.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)))
        if added:
            nid2 = NOTIFYICONDATAW()
            nid2.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid2.hWnd = hwnd
            nid2.uID = 1
            nid2.uVersion = NOTIFYICON_VERSION_4
            _shell.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(nid2))
            msg = wt.MSG()
            deadline = time.monotonic() + 0.3
            while time.monotonic() < deadline:
                if _u32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                    _u32.TranslateMessage(ctypes.byref(msg))
                    _u32.DispatchMessageW(ctypes.byref(msg))
                else:
                    time.sleep(0.01)
    finally:
        if hwnd:
            if added:
                nid.uFlags = 0
                _shell.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
            _u32.DestroyWindow(hwnd)
        _u32.UnregisterClassW(cls_name, hinst)
    return (added, "tray icon added+removed" if added else "Shell_NotifyIcon ADD failed")


def selftest_settings():
    """Construct the Settings window with a withdrawn root; never shown.

    Checks that the window stays unmapped, that its content fits (the window
    size comes from the content, clamped to the work area, and only the device
    tree may shrink), and that the lockout warnings behave on the live list.
    """
    set_dpi_aware()
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    suspend_calls = []

    class _Ctl(object):
        def __init__(self, r):
            self.root = r

        def get_settings(self):
            return default_settings()

        def enumerate(self, show):
            return enumerate_devices(show)

        def on_settings_closed(self, w):
            pass

        def apply_settings(self, new, prev):
            return True

        def suspend_hotkey(self, flag):
            suspend_calls.append(flag)

    try:
        win = SettingsWindow(_Ctl(root))
        n_groups = len(win.tree.get_children(""))
        lay = dict(win.layout)
        mapped = bool(win.top.winfo_ismapped()) or bool(root.winfo_ismapped())
        state = str(win.top.state())
        flat = list(win.flat or [])
        win.close(resume_hotkey=False)
        root.update_idletasks()
        root.destroy()
    except Exception as e:
        try:
            root.destroy()
        except Exception:
            pass
        return False, "Settings construction failed: %r" % (e,)
    phys = [d for d in flat if d.get("present") and not d.get("virtual")]
    kb = [d["id"] for d in phys if d.get("class_name") == "Keyboard"]
    mice = [d["id"] for d in phys if d.get("class_name") == "Mouse"]
    w_total = [c for c, _ in selection_warnings(flat, kb + mice)]
    w_mice = [c for c, _ in selection_warnings(flat, mice)]
    w_kb = [c for c, _ in selection_warnings(flat, kb[:1])]
    problems = []
    if mapped or state != "withdrawn":
        problems.append("window was mapped (state=%s)" % state)
    if n_groups < 1:
        problems.append("no device groups")
    if not lay.get("fits"):
        problems.append("content does not fit the window: %r" % (lay,))
    if kb and mice and "total" not in w_total:
        problems.append("total-input warning missing")
    if kb and "keyboard" not in w_kb:
        problems.append("keyboard warning missing")
    if mice and w_mice:
        problems.append("unexpected warning for a mouse-only selection: %r" % (w_mice,))
    if suspend_calls:
        problems.append("hotkey suspend was called")
    detail = ("Settings built withdrawn; %d device groups; content %dx%d px in a "
              "%dx%d px window (max %dx%d, tk scaling %.2f)"
              % (n_groups, lay.get("req_w", 0), lay.get("req_h", 0), lay.get("win_w", 0),
                 lay.get("win_h", 0), lay.get("max_w", 0), lay.get("max_h", 0),
                 lay.get("tk_scaling", 0.0)))
    if problems:
        return False, detail + "; " + "; ".join(problems)
    return True, detail + "; layout fits; warnings ok"


def selftest_controller():
    """Build the real Controller (withdrawn Tk root + a Win32App object that is
    never run: no window, no tray icon, no hotkey) and check its wiring, the
    first-run plan and the tooltip/menu text on the live device list. Writes
    settings.json into the data dir. Returns (ok, detail)."""
    set_dpi_aware()
    saved_hooks = (sys.excepthook, threading.excepthook)
    ctl = None
    try:
        fresh = not load_settings().get("first_run_done")
        ctl = Controller()
        problems = []
        if str(ctl.root.state()) != "withdrawn" or ctl.root.winfo_ismapped():
            problems.append("root window was mapped")
        if ctl.app is None or ctl.app.hwnd is not None:
            problems.append("Win32App unexpectedly created a window")
        sel = ctl.selected_ids()
        if fresh and sel:
            problems.append("first run pre-selected devices: %r" % (sel,))
        if fresh and not ctl.open_settings_on_start:
            problems.append("first run with nothing selected would not open Settings")
        if not load_settings().get("first_run_done"):
            problems.append("first_run_done was not saved")
        snap = ctl.selection_snapshot()
        ctl.app._refresh_state()
        tip = ctl.app._tooltip()
        label, _action, _grayed = menu_toggle_spec(ctl.app.cur_state, ctl.is_busy())
        if len(tip) > 127:
            problems.append("tooltip longer than 127 chars")
        detail = ("controller built withdrawn; %d device(s) selected; open_settings=%s; "
                  "state=%s; label=%r; menu=%r; tooltip=%r"
                  % (len(sel), ctl.open_settings_on_start, snap["state"], snap["label"],
                     label, tip))
        if problems:
            return False, detail + "; " + "; ".join(problems)
        return True, detail
    except Exception as e:
        return False, "controller construction failed: %r" % (e,)
    finally:
        if ctl is not None:
            try:
                ctl.root.destroy()
            except Exception:
                pass
        sys.excepthook, threading.excepthook = saved_hooks


def _icon_loads(path):
    """True when Windows can load the .ico file (no window needed)."""
    h = _u32.LoadImageW(None, path, IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE)
    if not h:
        return False
    _u32.DestroyIcon(h)
    return True


def installed_helper_path():
    """Where the installer puts the helper: next to the app\\ folder that holds
    the frozen InputToggle.exe. None when running from source."""
    if not FROZEN:
        return None
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(sys.executable))),
                        "InputToggleHelper.exe")


def redact_text(text):
    """Replace the profile folders anywhere in a line, case-insensitively
    (quoted command lines included), so self-test output can be shared."""
    out = str(text)
    for var in ("LOCALAPPDATA", "USERPROFILE"):     # the longer one first
        base = os.environ.get(var, "").rstrip("\\/")
        if not base:
            continue
        low, low_base = out.lower(), base.lower()
        pos = low.find(low_base)
        while pos != -1:
            out = out[:pos] + "%" + var + "%" + out[pos + len(base):]
            low = out.lower()
            pos = low.find(low_base, pos + len(var) + 2)
    return out


def installed_app_exe():
    """Where the installer puts the release exe (64-bit Program Files)."""
    base = (os.environ.get("ProgramW6432") or os.environ.get("ProgramFiles")
            or "C:\\Program Files")
    return os.path.join(base, "InputToggle", "app", "InputToggle.exe")


def is_installed_copy(executable=None):
    """True when this release exe is the installed one (not a build folder)."""
    exe = executable or sys.executable
    return (os.path.normcase(os.path.abspath(exe))
            == os.path.normcase(os.path.abspath(installed_app_exe())))


def redact_user_path(path):
    """Self-test output ends up in public issues: show the profile folders as
    %LOCALAPPDATA% / %USERPROFILE% instead of a path with the user name."""
    p = str(path)
    for var in ("LOCALAPPDATA", "USERPROFILE"):
        base = os.environ.get(var, "").rstrip("\\/")
        if base and p.lower().startswith(base.lower()) and (
                len(p) == len(base) or p[len(base)] in "\\/"):
            return "%" + var + "%" + p[len(base):]
    return p


def selftest_readonly():
    """Read-only checks: no device change, no window, no instance mutex. They
    pass on any PC, including a build server without a mouse or keyboard
    (the device count is information, not a requirement). Returns
    (ok, lines)."""
    lines = []
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        lines.append("[%s] %s%s" % ("PASS" if cond else "FAIL", name,
                                    (" - " + detail) if detail else ""))
        if not cond:
            ok = False

    def info(text):
        lines.append("[INFO] " + text)

    if FROZEN:
        info("InputToggle %s, release build: %s" % (VERSION, redact_user_path(sys.executable)))
    else:
        info("InputToggle %s, from source: %s"
             % (VERSION, redact_user_path(os.path.abspath(__file__))))
    flat = []
    try:
        groups = enumerate_devices(True)
        flat = flatten(groups)
        check("enumerate devices", True, "%d devnodes in %d groups" % (len(flat), len(groups)))
    except Exception as e:
        check("enumerate devices", False, repr(e))
    present = [d for d in flat if d.get("present")]
    info("present devnodes: %d Mouse, %d Keyboard; virtual/remote devnodes: %d"
         % (len([d for d in present if d.get("class_name") == "Mouse"]),
            len([d for d in present if d.get("class_name") == "Keyboard"]),
            len([d for d in flat if d.get("virtual")])))
    hk_ok, _ = validate_hotkey(DEFAULT_HOTKEY)
    check("default hotkey valid", hk_ok, format_hotkey(DEFAULT_HOTKEY))
    try:
        sample = "HID\\VID_1234&PID_5678&MI_00\\7&1a2b3c4d&0&0000"
        act, ids, skipped, issued = parse_request(format_request("disable", [sample]))
        check("request format/parse round trip",
              act == "disable" and ids == [sample] and not skipped
              and request_is_fresh(issued))
    except Exception as e:
        check("request format/parse round trip", False, repr(e))
    probe = os.path.join(data_dir(), ".selftest-probe.txt")
    try:
        atomic_write(probe, "ok\n")
        os.remove(probe)
        check("data dir writable", True, redact_user_path(data_dir()))
    except Exception as e:
        check("data dir writable", False, "%s (%r)" % (redact_user_path(data_dir()), e))
    # Tcl/Tk (Settings window, hidden root): a Tcl interpreter without Tk, so
    # no window can appear; in the release build also the bundled Tk scripts
    try:
        import tkinter
        patch = tkinter.Tcl().eval("info patchlevel")
        tk_ok = (not FROZEN) or os.path.isfile(os.path.join(resource_dir(), "_tk_data", "tk.tcl"))
        check("Tcl/Tk runtime", tk_ok, "Tcl " + patch)
    except Exception as e:
        check("Tcl/Tk runtime", False, repr(e))
    if FROZEN:
        names = (ICON_ON_NAME, ICON_OFF_NAME, APP_ICON_NAME)
        found = [(n, bundled_icon(n)) for n in names]
        good = [n for n, p in found if p and _icon_loads(p)]
        check("bundled icons", len(good) == len(names),
              ", ".join(good) if good else "none found")
        helper = installed_helper_path()
        info("helper exe %s: %s" % (helper, "found" if os.path.isfile(helper) else "missing"))
    else:
        info("bundled icons: not used when running from source")
    # informational only: a build server or a fresh PC has no task
    info("helper task %s installed: %s" % (TASK_NAME, apply_task_exists()))
    try:
        run = get_run_on_logon()
    except Exception:
        run = None
    info("logon entry: %s" % (run if run else "none"))
    return ok, [redact_text(ln) for ln in lines]


# ========================================================================
# CLI output. The release build is a windowed exe: it has no console and
# sys.stdout may be None. Every CLI message goes through say(), which prints
# when there is somewhere to print to and collects the lines for --out.
# ========================================================================
class CliOutput(object):
    def __init__(self):
        self.lines = None                  # None = not collecting

    def start(self):
        self.lines = []

    def say(self, text=""):
        text = str(text)
        if self.lines is not None:
            self.lines.append(text)
        stream = sys.stdout
        if stream is None:
            return
        try:
            stream.write(text + "\n")
            stream.flush()
        except UnicodeEncodeError:
            try:
                enc = getattr(stream, "encoding", None) or "ascii"
                stream.write((text + "\n").encode(enc, "replace").decode(enc, "replace"))
                stream.flush()
            except Exception:
                pass
        except Exception:
            pass

    def write_to(self, path):
        """Write the collected lines to path (UTF-8). True on success."""
        try:
            atomic_write(os.path.abspath(path), "\n".join(self.lines or []) + "\n")
            return True
        except Exception:
            return False


OUTPUT = CliOutput()


def say(text=""):
    OUTPUT.say(text)


# ========================================================================
# CLI
# ========================================================================
def find_instance_window():
    return _u32.FindWindowW(WND_CLASS, None)


def instance_mutex_exists():
    h = _k32.OpenMutexW(SYNCHRONIZE, False, MUTEX_NAME)
    if h:
        _k32.CloseHandle(h)
        return True
    return False


def cli_status():
    try:
        groups = enumerate_devices(True)
    except DeviceListError as e:
        say("ERROR: could not read the device list: %s" % e)
        return 1
    say("InputToggle device inventory (Mouse + Keyboard classes)")
    say("=" * 64)
    present_total = 0
    for g in groups:
        say("")
        say("GROUP: %s   [%s]   %s" % (g["name"], g["type"], g["status"]))
        say("  key: %s" % g["key"])
        for d in g["devices"]:
            if d.get("present"):
                st = "ON " if d.get("enabled") else "OFF"
                present_total += 1
            else:
                st = "---"
            say("    [%s] %-8s vid=%s pid=%s %-10s %s%s"
                % (st, d.get("class_name", "?"), d.get("vid"), d.get("pid"),
                   iface_text(d) or "-", d["id"],
                   "  [virtual/remote]" if d.get("virtual") else ""))
            if d.get("name") and d["name"] != d.get("class_name"):
                say("          name: %s" % display_name(d["name"], d.get("vid"),
                                                       d.get("pid"), with_ids=False))
    say("")
    say("=" * 64)
    say("%d groups, %d present devnode(s). Legend: ON=enabled OFF=disabled "
        "---=disconnected" % (len(groups), present_total))
    sel = load_settings().get("devices", [])
    if sel:
        say("Currently selected to switch: %d device(s)" % len(sel))
    return 0


def cli_enable_all():
    hwnd = find_instance_window()
    if hwnd:
        _u32.PostMessageW(hwnd, MSG_ENABLE_ALL, 0, 0)
        say("Asked the running InputToggle to re-enable everything it disabled.")
        return 0
    res = enable_all_core(load_settings().get("devices", []), fallback=True)
    say(res.get("outcome", ""))
    return 0 if res.get("ok") else 1


def cli_toggle():
    hwnd = find_instance_window()
    if hwnd:
        _u32.PostMessageW(hwnd, MSG_TOGGLE, 0, 0)
        say("Asked the running InputToggle to toggle.")
        return 0
    res = toggle_core(load_settings().get("devices", []))
    say(res.get("outcome", ""))
    return 0 if res.get("ok") else 1


def cli_quit(timeout=30.0, _find=None, _post=None, _alive=None, _sleep=None):
    """Ask the running instance to exit cleanly (it re-enables what it switched
    off if 'Re-enable on exit' is set) and wait until the process is gone."""
    find = _find or find_instance_window
    post = _post or (lambda h: _u32.PostMessageW(h, MSG_REQUEST_QUIT, 0, 0))
    alive = _alive or instance_mutex_exists
    sleep = _sleep or time.sleep
    hwnd = find()
    if not hwnd:
        if alive():
            say("InputToggle is starting or stopping; try again in a few seconds.")
            return 1
        say("InputToggle is not running.")
        return 0
    post(hwnd)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not find() and not alive():
            say("InputToggle stopped.")
            return 0
        sleep(0.25)
    say("InputToggle did not stop within %d s." % int(timeout))
    return 1


# Token elevation types (winnt.h TOKEN_ELEVATION_TYPE).
TOKEN_ELEVATION_TYPE_DEFAULT = 1   # no split token: UAC off, or a standard user
TOKEN_ELEVATION_TYPE_FULL = 2      # the elevated half of a UAC split admin token
TOKEN_ELEVATION_TYPE_LIMITED = 3   # the normal, filtered half
TOKEN_QUERY = 0x0008
_TOKEN_ELEVATION_TYPE_CLASS = 18   # TOKEN_INFORMATION_CLASS TokenElevationType


def token_elevation_type():
    """TOKEN_ELEVATION_TYPE of this process, or None when it cannot be read."""
    try:
        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        adv.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
        adv.OpenProcessToken.restype = wt.BOOL
        adv.GetTokenInformation.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                            wt.DWORD, ctypes.POINTER(wt.DWORD)]
        adv.GetTokenInformation.restype = wt.BOOL
        _k32.GetCurrentProcess.argtypes = []
        _k32.GetCurrentProcess.restype = wt.HANDLE
        tok = wt.HANDLE()
        if not adv.OpenProcessToken(_k32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(tok)):
            return None
        try:
            val = wt.DWORD(0)
            got = wt.DWORD(0)
            if not adv.GetTokenInformation(tok, _TOKEN_ELEVATION_TYPE_CLASS, ctypes.byref(val),
                                           ctypes.sizeof(val), ctypes.byref(got)):
                return None
            return int(val.value)
        finally:
            _k32.CloseHandle(tok)
    except Exception:
        return None


def refuse_elevated(elevation_type):
    """The tray application never runs elevated. Only the elevated half of a
    split token is refused: with UAC off (or for a standard user) there is no
    non-elevated alternative, and nothing is gained by refusing."""
    return elevation_type == TOKEN_ELEVATION_TYPE_FULL


ELEVATED_TEXT = ("InputToggle does not run as administrator. Start it normally: "
                 "from the Start menu, or sign out and in again (it starts with "
                 "Windows). Only its small helper runs elevated, through its "
                 "scheduled task.")


def run_app(open_settings_on_start=False):
    if refuse_elevated(token_elevation_type()):
        # e.g. started from an elevated prompt, or by a setup program that was
        # itself started elevated: this one message box is the whole UI
        log().warning("refusing to run elevated")
        try:
            _u32.MessageBoxW(None, ELEVATED_TEXT, "InputToggle", MB_OK | MB_ICONWARNING)
        except Exception:
            pass
        return 1
    # single instance
    mutex = _k32.CreateMutexW(None, False, MUTEX_NAME)
    if mutex and ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        hwnd = None
        for _attempt in range(10):          # the first instance may still be starting
            hwnd = find_instance_window()
            if hwnd:
                break
            time.sleep(0.3)
        if hwnd:
            pid = wt.DWORD(0)
            _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value:
                # let the running instance bring Settings to the front
                _u32.AllowSetForegroundWindow(pid.value)
            _u32.PostMessageW(hwnd, MSG_OPEN_SETTINGS, 0, 0)
            log().info("already running (pid %s); asked it to open Settings", pid.value)
        else:
            log().warning("another instance holds the mutex but its window did not "
                          "appear within 3 s; request dropped")
        _k32.CloseHandle(mutex)
        return 0
    global _MUTEX_HANDLE
    _MUTEX_HANDLE = mutex                       # keep alive for process lifetime
    install_exception_hooks()
    try:
        ctl = Controller(open_settings_on_start=open_settings_on_start)
        ctl.run()
    except Exception:
        log().exception("app crashed")
        return 1
    return 0


_MUTEX_HANDLE = None


def _arg_value(args, name, default=None):
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
    return default


# Modes whose only effect is output: the ones --out may be combined with. None
# of them starts the tray application or takes the single-instance mutex.
OUTPUT_MODES = ("--version", "--status", "--selftest", "--selftest-hotkey",
                "--selftest-tray", "--selftest-settings", "--selftest-controller")


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    args = argv[1:]
    out_path = None
    if "--out" in args:
        OUTPUT.start()
        out_path = _arg_value(args, "--out")
        if not out_path or out_path.startswith("--"):
            say("ERROR: --out needs a file name")
            return 2
        if not any(m in args for m in OUTPUT_MODES):
            # never let --out start the tray application or change a device
            say("ERROR: --out can only be used with --version, --status or --selftest")
            OUTPUT.write_to(out_path)
            return 2
    try:
        rc = _run_cli(args)
    except Exception as e:
        try:
            log().exception("command %s failed", args)
        except Exception:
            pass
        say("ERROR: %r" % (e,))
        rc = 3
    if out_path and not OUTPUT.write_to(out_path):
        rc = rc or 2
    return rc


def _report(ok, detail):
    say(("PASS " if ok else "FAIL ") + detail)
    return 0 if ok else 1


def _run_cli(args):
    if "--version" in args:
        say("InputToggle %s" % VERSION)        # no log, no data folder
        return 0
    # before any window exists (Tk fixes its scaling when the root is created)
    set_dpi_aware()
    try:
        log().info("start v%s%s argv=%s", VERSION, " (release build)" if FROZEN else "",
                   args)
    except Exception:
        pass
    if "--status" in args:
        return cli_status()
    if "--selftest" in args:
        ok, lines = selftest_readonly()
        for line in lines:
            say(line)
        say("RESULT: %s" % ("PASS" if ok else "FAIL"))
        return 0 if ok else 1
    if "--selftest-hotkey" in args:
        try:
            attempts = int(_arg_value(args, "--attempts", "3"))
        except ValueError:
            attempts = 1
        return _report(*selftest_hotkey_f24(attempts))
    if "--selftest-tray" in args:
        return _report(*selftest_tray())
    if "--selftest-settings" in args:
        return _report(*selftest_settings())
    if "--selftest-controller" in args:
        return _report(*selftest_controller())
    if "--quit" in args:
        return cli_quit()
    if "--enable-all" in args:
        return cli_enable_all()
    if "--toggle" in args:
        return cli_toggle()
    return run_app(open_settings_on_start=("--settings" in args))


if __name__ == "__main__":
    try:
        _rc = main()
    except SystemExit:
        raise
    except BaseException:
        # The windowed release build would show a traceback dialog for an
        # exception that escapes; log it and exit with an error code instead.
        try:
            log().exception("fatal error")
        except Exception:
            pass
        _rc = 3
    sys.exit(_rc)
