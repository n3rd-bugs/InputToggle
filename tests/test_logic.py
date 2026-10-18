# -*- coding: utf-8 -*-
"""Unit tests for InputToggle pure logic.

They pass on a clean CI runner: no input hardware, no tray, no helper task,
no registry writes. No device is changed and no window is created: the
toggle flow is exercised with fake device status and a fake task runner, and
every file lands in _testtmp\\logic inside the repository.
"""
import contextlib
import ctypes
import importlib.util
import io
import re
import shutil
import sys
import os
import threading
import time
import unittest

sys.dont_write_bytecode = True     # keep the repository free of __pycache__

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PYW = os.path.join(ROOT, "src", "inputtoggle.pyw")
DATADIR = os.path.join(ROOT, "_testtmp", "logic")

# keep every data_dir() write inside the repository (forced, not setdefault)
os.environ["INPUTTOGGLE_DATADIR"] = DATADIR


def _load():
    spec = importlib.util.spec_from_file_location("inputtoggle_mod", PYW)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


it = _load()


class HotkeyTests(unittest.TestCase):
    def test_format_default(self):
        self.assertEqual(it.format_hotkey(it.DEFAULT_HOTKEY), "Ctrl+Alt+Shift+M")

    def test_parse_roundtrip(self):
        hk = it.parse_hotkey("Ctrl+Alt+Shift+M")
        self.assertTrue(hk["ctrl"] and hk["alt"] and hk["shift"])
        self.assertFalse(hk["win"])
        self.assertEqual(hk["key"], "M")
        self.assertEqual(hk["vk"], 0x4D)
        self.assertEqual(it.format_hotkey(hk), "Ctrl+Alt+Shift+M")

    def test_parse_case_insensitive_fkey(self):
        hk = it.parse_hotkey("ctrl+shift+f5")
        self.assertEqual(hk["key"], "F5")
        self.assertEqual(hk["vk"], 0x74)

    def test_normalize_from_vk_only(self):
        hk = it.normalize_hotkey({"ctrl": True, "vk": 0x4D})
        self.assertEqual(hk["key"], "M")

    def test_win32_bits(self):
        mods, vk = it.hotkey_win32(it.DEFAULT_HOTKEY)
        self.assertEqual(vk, 0x4D)
        self.assertTrue(mods & it.MOD_CONTROL)
        self.assertTrue(mods & it.MOD_ALT)
        self.assertTrue(mods & it.MOD_SHIFT)
        self.assertTrue(mods & it.MOD_NOREPEAT)
        self.assertFalse(mods & it.MOD_WIN)

    def test_validate_default_ok(self):
        ok, _ = it.validate_hotkey(it.DEFAULT_HOTKEY)
        self.assertTrue(ok)

    def test_validate_requires_mod(self):
        ok, reason = it.validate_hotkey({"shift": True, "key": "M"})
        self.assertFalse(ok)
        self.assertIn("Ctrl", reason)

    def test_validate_fkey_exempt(self):
        ok, _ = it.validate_hotkey({"key": "F13"})
        self.assertTrue(ok)

    def test_validate_pause_exempt(self):
        ok, _ = it.validate_hotkey({"key": "Pause"})
        self.assertTrue(ok)

    def test_validate_f12_not_exempt(self):
        ok, _ = it.validate_hotkey({"key": "F12"})
        self.assertFalse(ok)      # F12 is 0x7B, below F13

    def test_reject_ctrl_alt_del(self):
        ok, _ = it.validate_hotkey(it.normalize_hotkey(
            {"ctrl": True, "alt": True, "vk": it.VK_DELETE, "key": "Delete"}))
        self.assertFalse(ok)

    def test_reject_ctrl_alt_esc(self):
        ok, _ = it.validate_hotkey(it.normalize_hotkey(
            {"ctrl": True, "alt": True, "vk": it.VK_ESCAPE, "key": "Esc"}))
        self.assertFalse(ok)

    def test_reject_win_l(self):
        ok, _ = it.validate_hotkey({"win": True, "key": "L"})
        self.assertFalse(ok)

    def test_reject_alt_f4(self):
        ok, reason = it.validate_hotkey({"alt": True, "key": "F4"})
        self.assertFalse(ok)
        self.assertIn("Alt+F4", reason)

    def test_reject_common_ctrl_letters(self):
        for k in "ACVXZYSFWTNPR":
            ok, _ = it.validate_hotkey({"ctrl": True, "key": k})
            self.assertFalse(ok, "Ctrl+%s must be refused" % k)

    def test_two_modifiers_with_common_letter_allowed(self):
        self.assertTrue(it.validate_hotkey({"ctrl": True, "shift": True, "key": "C"})[0])
        self.assertTrue(it.validate_hotkey({"ctrl": True, "alt": True, "key": "F4"})[0])

    def test_single_modifier_warning(self):
        self.assertIn("Ctrl+M", it.hotkey_warning({"ctrl": True, "key": "M"}))
        self.assertIn("Win+5", it.hotkey_warning({"win": True, "key": "5"}))
        self.assertEqual(it.hotkey_warning(it.DEFAULT_HOTKEY), "")
        self.assertEqual(it.hotkey_warning({"key": "F13"}), "")
        self.assertEqual(it.hotkey_warning({"alt": True, "key": "F13"}), "")
        self.assertEqual(it.hotkey_warning({"ctrl": True, "key": "C"}), "")   # refused

    def test_record_modifier_alone_waits(self):
        for vk in (0x10, 0x11, 0x12, 0x5B, 0x5C, 0xA0, 0xA5):
            self.assertEqual(it.hotkey_from_record(vk, True, False, False, False)[0], "wait")

    def test_record_escape_cancels(self):
        self.assertEqual(it.hotkey_from_record(0x1B, False, False, False, False), ("cancel", None))

    def test_record_unsupported_key(self):
        self.assertEqual(it.hotkey_from_record(0x09, True, False, False, False)[0], "unsupported")
        # Ctrl+Esc is not a choice either (and never the cancel key)
        self.assertEqual(it.hotkey_from_record(0x1B, True, False, False, False)[0], "unsupported")

    def test_record_uses_given_modifiers_only(self):
        # The flags come from GetKeyState. Nothing like Tk's NumLock bit can
        # sneak in an Alt, and Win is captured.
        st, hk = it.hotkey_from_record(0x4D, True, False, True, False)
        self.assertEqual(st, "ok")
        self.assertEqual(it.format_hotkey(hk), "Ctrl+Shift+M")
        st, hk = it.hotkey_from_record(0x78, False, False, False, True)
        self.assertEqual(it.format_hotkey(hk), "Win+F9")


class TrayConstantTests(unittest.TestCase):
    def test_nin_values_match_shellapi_h(self):
        # SDK 10.0.26100 um\shellapi.h: all relative to WM_USER (0x400)
        self.assertEqual(it.WM_USER, 0x0400)
        self.assertEqual(it.NIN_SELECT, 0x0400)
        self.assertEqual(it.NIN_KEYSELECT, 0x0401)
        self.assertEqual(it.NIN_BALLOONSHOW, 0x0402)
        self.assertEqual(it.NIN_BALLOONHIDE, 0x0403)
        self.assertEqual(it.NIN_BALLOONTIMEOUT, 0x0404)
        self.assertEqual(it.NIN_BALLOONUSERCLICK, 0x0405)
        self.assertEqual(it.NIN_POPUPOPEN, 0x0406)
        self.assertEqual(it.NIN_POPUPCLOSE, 0x0407)
        self.assertEqual(it.NIF_GUID, 0x20)
        self.assertEqual(it.NIF_SHOWTIP, 0x80)
        self.assertEqual(it.WM_CONTEXTMENU, 0x007B)
        self.assertEqual(it.ENDSESSION_CLOSEAPP, 0x1)

    def test_balloon_and_popup_events_never_open_anything(self):
        for evt in (it.NIN_SELECT, it.NIN_BALLOONSHOW, it.NIN_BALLOONHIDE,
                    it.NIN_BALLOONTIMEOUT, it.NIN_BALLOONUSERCLICK,
                    it.NIN_POPUPOPEN, it.NIN_POPUPCLOSE):
            for v4 in (True, False):
                self.assertIsNone(it.tray_event_action(evt, v4), hex(evt))

    def test_settings_on_double_click_or_keyboard(self):
        self.assertEqual(it.tray_event_action(it.WM_LBUTTONDBLCLK, True), "settings")
        self.assertEqual(it.tray_event_action(it.NIN_KEYSELECT, True), "settings")

    def test_menu_once_per_right_click(self):
        # v4 sends WM_RBUTTONUP and then WM_CONTEXTMENU: only one opens the menu
        self.assertIsNone(it.tray_event_action(it.WM_RBUTTONUP, True))
        self.assertEqual(it.tray_event_action(it.WM_CONTEXTMENU, True), "menu")
        # without v4 there is no WM_CONTEXTMENU, so button-up opens it
        self.assertEqual(it.tray_event_action(it.WM_RBUTTONUP, False), "menu")
        self.assertIsNone(it.tray_event_action(it.WM_CONTEXTMENU, False))

    def test_signed16(self):
        self.assertEqual(it.signed16(0xFFFF), -1)
        self.assertEqual(it.signed16(0x8000), -32768)
        self.assertEqual(it.signed16(0x7FFF), 32767)
        self.assertEqual(it.signed16(0x12345), 0x2345)

    @unittest.skipUnless(ctypes.sizeof(ctypes.c_void_p) == 8, "64-bit layout")
    def test_struct_sizes_x64(self):
        self.assertEqual(ctypes.sizeof(it.NOTIFYICONDATAW), 976)
        self.assertEqual(ctypes.sizeof(it.INPUT), 40)
        self.assertEqual(ctypes.sizeof(it.PROCESSENTRY32W), 568)


class ToggleDecisionTests(unittest.TestCase):
    def test_any_enabled_disables(self):
        self.assertEqual(it.decide_action([True, False]), "disable")
        self.assertEqual(it.decide_action([True, True]), "disable")

    def test_none_enabled_enables(self):
        self.assertEqual(it.decide_action([False, False]), "enable")

    def test_empty_enables(self):
        self.assertEqual(it.decide_action([]), "enable")


class SettingsTests(unittest.TestCase):
    def test_defaults(self):
        s = it.default_settings()
        self.assertEqual(s["devices"], [])
        self.assertEqual(it.format_hotkey(s["hotkey"]), "Ctrl+Alt+Shift+M")
        self.assertTrue(s["reenable_on_exit"])
        self.assertTrue(s["reenable_at_startup"])
        self.assertFalse(s["auto_disable_crd"])
        self.assertTrue(s["start_with_windows"])
        self.assertTrue(s["show_notifications"])
        self.assertFalse(s["play_sound"])

    def test_defaults_independent(self):
        a = it.default_settings()
        a["devices"].append("x")
        b = it.default_settings()
        self.assertEqual(b["devices"], [])

    def test_migrate_v1_string_hotkey(self):
        s = it.migrate_settings({"hotkey": "Ctrl+Alt+M", "devices": ["HID\\X"]})
        self.assertIsInstance(s["hotkey"], dict)
        self.assertEqual(it.format_hotkey(s["hotkey"]), "Ctrl+Alt+M")
        self.assertEqual(s["devices"], ["HID\\X"])
        self.assertEqual(s["version"], 2)

    def test_migrate_drops_unknown_and_coerces(self):
        s = it.migrate_settings({"bogus": 1, "play_sound": 1, "devices": ["a", 3, ""]})
        self.assertNotIn("bogus", s)
        self.assertIs(s["play_sound"], True)
        self.assertEqual(s["devices"], ["a"])

    def test_migrate_non_dict(self):
        self.assertEqual(it.migrate_settings(None), it.default_settings())
        self.assertEqual(it.migrate_settings("x"), it.default_settings())


class StateTests(unittest.TestCase):
    A, B = "HID\\A\\1", "HID\\B\\2"

    def test_normalize(self):
        self.assertEqual(it.normalize_state(None), it.empty_state())
        st = it.normalize_state({"disabled": ["b", "a", "a", 3, ""], "pending": "x"})
        self.assertEqual(st, {"disabled": ["a", "b"], "auto_disabled": [], "pending": []})

    def test_tracked_ids(self):
        st = {"disabled": [self.A], "auto_disabled": [self.A], "pending": [self.B]}
        self.assertEqual(it.tracked_ids(st), [self.A, self.B])

    def test_mark_pending(self):
        st = it.state_mark_pending(it.empty_state(), [self.A])
        self.assertEqual(st["pending"], [self.A])

    def test_disable_ok_moves_pending_to_disabled(self):
        st = it.state_mark_pending(it.empty_state(), [self.A])
        st = it.reconcile_state(st, "disable", [self.A], {}, ok=True, auto=True)
        self.assertEqual(st, {"disabled": [self.A], "auto_disabled": [self.A], "pending": []})

    def test_disable_timeout_still_on_stays_pending_if_helper_may_run(self):
        st = it.state_mark_pending(it.empty_state(), [self.A])
        fin = {self.A: {"present": True, "enabled": True}}
        kept = it.reconcile_state(st, "disable", [self.A], fin, ok=False, may_still_apply=True)
        self.assertEqual(kept["pending"], [self.A])
        self.assertEqual(kept["disabled"], [])
        dropped = it.reconcile_state(st, "disable", [self.A], fin, ok=False, may_still_apply=False)
        self.assertEqual(dropped["pending"], [])

    def test_disable_timeout_but_off_is_recorded(self):
        st = it.state_mark_pending(it.empty_state(), [self.A, self.B])
        fin = {self.A: {"present": True, "enabled": False}, self.B: {"present": False}}
        st = it.reconcile_state(st, "disable", [self.A, self.B], fin, ok=False, may_still_apply=True)
        self.assertEqual(st["disabled"], [self.A, self.B])
        self.assertEqual(st["pending"], [])

    def test_enable_only_forgets_verified(self):
        st = {"disabled": [self.A, self.B], "auto_disabled": [self.A], "pending": []}
        fin = {self.A: {"present": True, "enabled": True}, self.B: {"present": True, "enabled": False}}
        st = it.reconcile_state(st, "enable", [self.A, self.B], fin, ok=False)
        self.assertEqual(st["disabled"], [self.B])
        self.assertEqual(st["auto_disabled"], [])


class RequestTests(unittest.TestCase):
    def test_format(self):
        txt = it.format_request("disable", ["HID\\A", "HID\\B"], issued=123)
        self.assertEqual(txt, "action=disable\nissued=123\ndevice=HID\\A\ndevice=HID\\B\n")

    def test_format_default_issued_is_now(self):
        _a, _i, _s, issued = it.parse_request(it.format_request("enable", ["HID\\A"]))
        self.assertLessEqual(abs(issued - time.time()), 5)

    def test_format_bad_action(self):
        self.assertRaises(ValueError, it.format_request, "nuke", [])

    def test_parse_roundtrip(self):
        txt = it.format_request("enable", ["HID\\VID_1234&PID_5678&MI_00\\8&x&0&0000"], issued=99)
        action, ids, skipped, issued = it.parse_request(txt)
        self.assertEqual(action, "enable")
        self.assertEqual(len(ids), 1)
        self.assertEqual(skipped, [])
        self.assertEqual(issued, 99)

    def test_parse_missing_action(self):
        self.assertRaises(ValueError, it.parse_request, "device=HID\\A\n")

    def test_parse_bad_issued(self):
        _a, _i, skipped, issued = it.parse_request("action=enable\nissued=12x\n")
        self.assertIsNone(issued)
        self.assertEqual(len(skipped), 1)

    def test_parse_skips_bad_ids(self):
        txt = "action=disable\ndevice=HID\\A\ndevice=has space\ndevice=noslash\n"
        action, ids, skipped, _issued = it.parse_request(txt)
        self.assertEqual(ids, ["HID\\A"])
        self.assertEqual(len(skipped), 2)

    def test_parse_caps_at_32(self):
        lines = ["action=disable"] + ["device=HID\\D%d" % i for i in range(40)]
        action, ids, skipped, _issued = it.parse_request("\n".join(lines))
        self.assertEqual(len(ids), 32)
        self.assertEqual(len(skipped), 8)

    def test_freshness_window(self):
        now = 1000000
        self.assertTrue(it.request_is_fresh(now, now))
        self.assertTrue(it.request_is_fresh(now - 60, now))
        self.assertFalse(it.request_is_fresh(now - 61, now))
        self.assertTrue(it.request_is_fresh(now + 5, now))
        self.assertFalse(it.request_is_fresh(now + 6, now))
        self.assertFalse(it.request_is_fresh(None, now))


class InstanceIdTests(unittest.TestCase):
    def test_valid(self):
        self.assertTrue(it.valid_instance_id("HID\\VID_1234&PID_5678&MI_00\\7&1a2b3c4d&0&0000"))

    def test_invalid(self):
        self.assertFalse(it.valid_instance_id("has space\\x"))
        self.assertFalse(it.valid_instance_id("noslash"))
        self.assertFalse(it.valid_instance_id(""))
        self.assertFalse(it.valid_instance_id("A\\" + "x" * 500))
        self.assertFalse(it.valid_instance_id(None))
        self.assertFalse(it.valid_instance_id("HID\\*"))      # no wildcards

    def test_parse_vid_pid(self):
        d = it.parse_ids_from_devid("HID\\VID_1234&PID_5678&MI_01&Col02\\7&5e6f7a8b&0&0001")
        self.assertEqual(d["vid"], "1234")
        self.assertEqual(d["pid"], "5678")
        self.assertEqual(d["mi"], "MI_01")
        self.assertEqual(d["col"], "COL02")

    def test_composite_parent(self):
        self.assertTrue(it.is_composite_parent_id("USB\\VID_1234&PID_5678\\5&2c3d4e5f&0&1"))
        self.assertFalse(it.is_composite_parent_id("HID\\VID_1234&PID_5678&MI_00\\x"))

    def test_virtual_ids(self):
        for v in ("ROOT\\RDP_KBD\\0000", "ROOT\\RDP_MOU\\0000", "root\\rdp_kbd\\0000",
                  "TS_INPT\\TS_KBD\\1&2", "TERMINPUT_BUS\\UMB\\2&1", "VMBUS\\{x}\\1",
                  "SWD\\X\\1"):
            self.assertTrue(it.is_virtual_input_id(v), v)
        for v in ("HID\\VID_1234&PID_5678&MI_00\\8&x", "ACPI\\PNP0303\\4&1",
                  "HID\\{00001124-0000-1000-8000-00805f9b34fb}_VID&0002046d_PID&b35b\\9&x"):
            self.assertFalse(it.is_virtual_input_id(v), v)


class NamingTests(unittest.TestCase):
    def test_display_name(self):
        self.assertEqual(it.display_name("USB Receiver", "046D", "C52B"),
                         "Logitech USB Receiver (046D:C52B)")
        self.assertEqual(it.display_name("Microsoft Basic Optical Mouse", "045E", "0084"),
                         "Microsoft Basic Optical Mouse (045E:0084)")
        self.assertEqual(it.display_name("USB Receiver", "046D", "C52B", with_ids=False),
                         "Logitech USB Receiver")
        self.assertEqual(it.display_name("", None, None), "Device")
        self.assertEqual(it.display_name("Thing", "ABCD", "0001"), "Thing (ABCD:0001)")

    def test_child_label(self):
        self.assertEqual(it.child_label({"class_name": "Keyboard", "mi": "MI_01", "col": "COL01"}),
                         "Keyboard - MI_01/COL01")
        self.assertEqual(it.child_label({"class_name": "Mouse", "mi": "MI_00"}), "Mouse - MI_00")
        self.assertIn("virtual", it.child_label({"class_name": "Keyboard", "virtual": True,
                                                 "name": "Terminal Server Keyboard Driver"}))

    def test_group_status_and_type(self):
        on = {"present": True, "enabled": True, "class_name": "Mouse"}
        off = {"present": True, "enabled": False, "class_name": "Keyboard"}
        gone = {"present": False, "class_name": "Mouse"}
        self.assertEqual(it.group_status_text([gone]), "not connected")
        self.assertEqual(it.group_status_text([on, gone]), "ON")
        self.assertEqual(it.group_status_text([off]), "OFF")
        self.assertEqual(it.group_status_text([on, off, dict(off), dict(on)]), "2 of 4 OFF")
        self.assertEqual(it.group_type_text([on, off]), "Mouse + Keyboard")
        self.assertEqual(it.group_type_text([on]), "Mouse")

    def test_group_devices_marks_other_port_copies(self):
        def dev(iid, gk, present):
            return {"id": iid, "group_key": gk, "group_name": "USB Receiver",
                    "class_name": "Keyboard", "present": present, "enabled": True if present else None,
                    "vid": "046D", "pid": "C52B", "name": "USB Receiver", "virtual": False}
        groups = it.group_devices([dev("HID\\A\\1", "USB\\VID_046D&PID_C52B\\P1", True),
                                   dev("HID\\B\\2", "USB\\VID_046D&PID_C52B\\P2", False)])
        self.assertEqual(groups[0]["name"], "Logitech USB Receiver (046D:C52B)")
        self.assertEqual(groups[1]["name"],
                         "Logitech USB Receiver (046D:C52B) - seen on another USB port")
        self.assertEqual(groups[0]["status"], "ON")
        self.assertEqual(groups[1]["status"], "not connected")
        self.assertEqual(groups[0]["devices"][0]["group_display"],
                         "Logitech USB Receiver (046D:C52B)")

    def test_selection_label(self):
        p_rz = {"id": "HID\\VID_1234&PID_5678&MI_00\\8&x", "name": "Example Mouse"}
        p_rz2 = {"id": "HID\\VID_1234&PID_5678&MI_01&COL02\\8&y", "name": "Example Mouse"}
        p_lg = {"id": "HID\\VID_046D&PID_C52B&MI_00\\8&z", "name": "USB Receiver"}
        p_x = {"id": "HID\\VID_1234&PID_0001\\1", "name": "Other"}
        self.assertEqual(it.selection_label_from_probes([p_rz, p_rz2]), "Example Mouse")
        self.assertEqual(it.selection_label_from_probes([p_rz, p_lg]),
                         "Example Mouse + Logitech USB Receiver")
        self.assertEqual(it.selection_label_from_probes([p_rz, p_lg, p_x]),
                         "Example Mouse + 2 more")
        long_p = {"id": "HID\\X\\1", "name": "N" * 100}
        self.assertLessEqual(len(it.selection_label_from_probes([long_p])), 48)
        self.assertEqual(it.selection_label_from_probes([]), "")


def _dev(iid, cls, present=True, virtual=False, vid="046D", pid="C52B", mi="MI_00",
         col=None, grp="Logitech USB Receiver (046D:C52B)"):
    return {"id": iid, "class_name": cls, "present": present, "virtual": virtual,
            "vid": vid, "pid": pid, "mi": mi, "col": col, "group_display": grp,
            "enabled": True if present else None}


FLAT = [
    _dev("HID\\LOGI_KB\\1", "Keyboard"),
    _dev("HID\\LOGI_MS\\1", "Mouse", mi="MI_01", col="COL01"),
    _dev("HID\\EX_MS0\\1", "Mouse", vid="1234", pid="5678", grp="Example Mouse (1234:5678)"),
    _dev("HID\\EX_KB1\\1", "Keyboard", vid="1234", pid="5678", mi="MI_01", col="COL01",
         grp="Example Mouse (1234:5678)"),
    _dev("HID\\OLD_KB\\1", "Keyboard", present=False, pid="0001", grp="Logitech USB Keyboard"),
    _dev("ROOT\\RDP_KBD\\0000", "Keyboard", virtual=True, vid=None, pid=None, mi=None,
         grp="Terminal Server Keyboard Driver"),
]


class WarningTests(unittest.TestCase):
    def codes(self, sel, flat=FLAT, enum_ok=True):
        return [c for c, _ in it.selection_warnings(flat, sel, enum_ok)]

    def test_mouse_only_selection_is_silent(self):
        self.assertEqual(self.codes(["HID\\EX_MS0\\1"]), [])

    def test_any_keyboard_class_needs_confirmation(self):
        self.assertEqual(self.codes(["HID\\LOGI_KB\\1"]), ["keyboard"])
        self.assertEqual(self.codes(["HID\\EX_MS0\\1", "HID\\EX_KB1\\1"]), ["keyboard"])
        text = it.selection_warnings(FLAT, ["HID\\LOGI_KB\\1"])[0][1]
        self.assertIn("Logitech USB Receiver (046D:C52B)", text)
        self.assertIn("Keyboard - MI_00", text)

    def test_every_keyboard_and_mouse_is_total(self):
        sel = ["HID\\LOGI_KB\\1", "HID\\LOGI_MS\\1", "HID\\EX_MS0\\1", "HID\\EX_KB1\\1"]
        self.assertEqual(self.codes(sel), ["total"])     # the virtual keyboard does not count

    def test_virtual_needs_confirmation(self):
        self.assertIn("virtual", self.codes(["ROOT\\RDP_KBD\\0000"]))

    def test_disconnected_keyboard_is_not_counted(self):
        self.assertEqual(self.codes(["HID\\OLD_KB\\1"]), [])

    def test_unknown_list_needs_confirmation(self):
        self.assertEqual(self.codes(["HID\\EX_MS0\\1"], flat=None), ["unknown"])
        self.assertEqual(self.codes(["HID\\EX_MS0\\1"], enum_ok=False), ["unknown"])


class MenuTooltipTests(unittest.TestCase):
    def test_menu_spec_follows_label(self):
        self.assertEqual(it.menu_toggle_spec("disabled"), ("Enable selected devices", "enable", False))
        self.assertEqual(it.menu_toggle_spec("enabled"), ("Disable selected devices", "disable", False))
        self.assertEqual(it.menu_toggle_spec("mixed")[1], "disable")
        for st in ("nothing", "absent", "unknown"):
            self.assertTrue(it.menu_toggle_spec(st)[2], st)
        self.assertTrue(it.menu_toggle_spec("enabled", busy=True)[2])

    def test_tooltip(self):
        hk = "Ctrl+Alt+Shift+M"
        self.assertEqual(it.build_tooltip("disabled", "Example Mouse", hk),
                         "InputToggle: Example Mouse OFF - Ctrl+Alt+Shift+M")
        self.assertIn("NOT active (used by another program)",
                      it.build_tooltip("enabled", "Example Mouse", hk, hotkey_ok=False))
        self.assertIn("helper not installed",
                      it.build_tooltip("enabled", "Example", hk, helper_ok=False))
        self.assertIn("switching", it.build_tooltip("enabled", "Example", hk, busy=True))
        self.assertIn("not connected", it.build_tooltip("absent", "Example", hk))
        self.assertIn("no device selected", it.build_tooltip("nothing", "", hk))
        huge = it.build_tooltip("enabled", "X" * 300, hk, hotkey_ok=False, hotkey_error="E" * 300)
        self.assertLessEqual(len(huge), 127)

    def test_status_text(self):
        self.assertEqual(it.status_text("disabled", "Example Mouse"), "Example Mouse: OFF")
        self.assertIn("Switching", it.status_text("enabled", "Example", busy=True))
        self.assertIn(it.helper_fix_hint(), it.status_text("enabled", "Example", helper_ok=False))

    def test_outcome_messages(self):
        lbl = "Example Mouse"
        off = {"ok": True, "action": "disable", "targets": ["x"]}
        self.assertEqual(it.outcome_message(off, lbl, "Ctrl+Alt+Shift+M"),
                         "Example Mouse is OFF. Press Ctrl+Alt+Shift+M to turn it back on.")
        self.assertIn("tray menu", it.outcome_message(off, lbl, None))
        on = {"ok": True, "action": "enable", "targets": ["x"]}
        self.assertEqual(it.outcome_message(on, lbl), "Example Mouse is ON again.")
        self.assertIn("are ON", it.outcome_message(on, "A + B"))
        tmo = {"ok": False, "action": "disable", "targets": ["x"], "reason": "timeout"}
        m = it.outcome_message(tmo, lbl)
        self.assertIn("timed out", m)
        self.assertIn(it.HELPER_LOG_HINT, m)
        self.assertIn(it.helper_fix_hint(),
                      it.outcome_message({"ok": False, "task_missing": True}, lbl))
        nothing = {"ok": True, "action": "enable", "targets": []}
        self.assertEqual(it.outcome_message(nothing, lbl, source="startup"), "")
        self.assertIn("Nothing to re-enable", it.outcome_message(nothing, lbl))
        self.assertTrue(it.outcome_message(off, lbl, "K", source="crd_start")
                        .startswith("Chrome Remote Desktop connected."))
        self.assertEqual(it.outcome_message({"ok": False, "reason": "none_present"}, lbl,
                                            source="crd_start"), "")
        long_fail = {"ok": False, "reason": "task_failed", "outcome": "x" * 500}
        self.assertLessEqual(len(it.outcome_message(long_fail, lbl)), 255)


class CrdTrackerTests(unittest.TestCase):
    def test_start_and_debounced_end(self):
        t = it.CrdTracker(end_after=3)
        self.assertEqual(t.step({10}), "start")
        self.assertIsNone(t.step({10}))
        self.assertIsNone(t.step(set()))
        self.assertIsNone(t.step(set()))
        self.assertEqual(t.step(set()), "end")
        self.assertFalse(t.active)

    def test_api_failure_changes_nothing(self):
        t = it.CrdTracker(end_after=3)
        t.step({10})
        for _ in range(5):
            self.assertIsNone(t.step(None))
        self.assertTrue(t.active)
        self.assertIsNone(t.step(set()))
        self.assertIsNone(t.step({10}))          # back: the miss count resets
        self.assertIsNone(t.step(set()))
        self.assertIsNone(t.step(set()))
        self.assertTrue(t.active)

    def test_new_pid_is_a_new_session(self):
        t = it.CrdTracker()
        t.step({10})
        self.assertEqual(t.step({11}), "restart")
        self.assertIsNone(t.step({11}))

    def test_idle(self):
        t = it.CrdTracker()
        self.assertIsNone(t.step(set()))
        self.assertIsNone(t.step(None))
        self.assertFalse(t.active)


class FakeDevices(object):
    """id -> True (on) / False (off) / 'absent'."""

    def __init__(self, states):
        self.states = dict(states)

    def status(self, ids):
        out = {}
        for i in set(ids):
            s = self.states.get(i, "absent")
            out[i] = {"id": i, "present": s != "absent",
                      "enabled": None if s == "absent" else s, "name": i}
        return out

    def runner(self, apply=True, rc=0, seen=None, tamper=False):
        def run(timeout):
            with open(it.request_path(), "r", encoding="utf-8") as fh:
                text = fh.read()
            if seen is not None:
                seen.append((text, it.load_state()))
            if tamper:
                with open(it.request_path(), "a", encoding="utf-8") as fh:
                    fh.write("device=HID\\EVIL\\1\n")
            if apply and rc == 0:
                action, ids, _s, _i = it.parse_request(text)
                for i in ids:
                    self.states[i] = (action == "enable")
            return rc, "SUCCESS"
        return run


def _quiet(**kw):
    kw.setdefault("_sleep", lambda s: None)
    return kw


class ToggleFlowTests(unittest.TestCase):
    A, B = "HID\\A\\1", "HID\\B\\2"

    def setUp(self):
        os.makedirs(DATADIR, exist_ok=True)
        for f in (it.state_path(), it.request_path()):
            if os.path.exists(f):
                os.remove(f)

    def test_disable_writes_ahead_then_records_and_cleans_up(self):
        fake = FakeDevices({self.A: True, self.B: True})
        seen = []
        res = it.toggle_core([self.A, self.B], None, **_quiet(
            _run=fake.runner(seen=seen), _status=fake.status, timeout=1.0))
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["action"], "disable")
        text, st_during = seen[0]
        self.assertIn("issued=", text)
        self.assertIn("device=" + self.A, text)
        self.assertEqual(st_during["pending"], [self.A, self.B])      # recorded BEFORE the run
        self.assertFalse(os.path.exists(it.request_path()))           # no replay later
        st = it.load_state()
        self.assertEqual(st["disabled"], [self.A, self.B])
        self.assertEqual(st["pending"], [])
        # toggling again enables and forgets them
        res = it.toggle_core([self.A, self.B], None, **_quiet(
            _run=fake.runner(), _status=fake.status, timeout=1.0))
        self.assertEqual(res["action"], "enable")
        self.assertTrue(res["ok"], res)
        self.assertEqual(it.tracked_ids(it.load_state()), [])

    def test_timeout_keeps_the_device_recorded(self):
        fake = FakeDevices({self.A: True})
        res = it.toggle_core([self.A], None, **_quiet(
            _run=fake.runner(apply=False), _status=fake.status, timeout=0))
        self.assertEqual(res["reason"], "timeout")
        self.assertTrue(res["started"])
        self.assertEqual(it.load_state()["pending"], [self.A])
        self.assertFalse(os.path.exists(it.request_path()))
        # re-enable-all sees it, finds it on, and forgets it
        res = it.enable_all_core([], **_quiet(_run=fake.runner(), _status=fake.status))
        self.assertEqual(res["targets"], [])
        self.assertEqual(it.tracked_ids(it.load_state()), [])

    def test_task_failure_clears_pending(self):
        fake = FakeDevices({self.A: True})
        res = it.toggle_core([self.A], None, **_quiet(
            _run=fake.runner(rc=1), _status=fake.status, check_missing=False))
        self.assertEqual(res["reason"], "task_failed")
        self.assertEqual(it.tracked_ids(it.load_state()), [])
        self.assertFalse(os.path.exists(it.request_path()))

    def test_forced_disable_skips_devices_already_off(self):
        fake = FakeDevices({self.A: False, self.B: True})
        res = it.toggle_core([self.A, self.B], "disable", **_quiet(
            _run=fake.runner(), _status=fake.status, timeout=1.0))
        self.assertEqual(res["targets"], [self.B])

    def test_nothing_selected_or_connected(self):
        fake = FakeDevices({})
        self.assertEqual(it.toggle_core([], **_quiet(_status=fake.status))["reason"], "none_selected")
        self.assertEqual(it.toggle_core([self.A], **_quiet(_status=fake.status))["reason"],
                         "none_present")

    def test_enable_all_falls_back_to_selection_when_state_is_empty(self):
        fake = FakeDevices({self.A: False})
        res = it.enable_all_core([self.A], **_quiet(
            _run=fake.runner(), _status=fake.status, timeout=1.0))
        self.assertTrue(res["fallback"])
        self.assertTrue(res["ok"], res)
        self.assertTrue(fake.states[self.A])
        res = it.enable_all_core([self.A], fallback=False, **_quiet(_status=fake.status))
        self.assertEqual(res["reason"], "nothing")

    def test_swapped_request_is_logged(self):
        fake = FakeDevices({self.A: True})
        with self.assertLogs(it.log(), level="WARNING") as cm:
            it.toggle_core([self.A], None, **_quiet(
                _run=fake.runner(tamper=True), _status=fake.status, timeout=1.0))
        self.assertTrue(any("changed by another process" in m for m in cm.output))


class CliQuitTests(unittest.TestCase):
    def run_quiet(self, **kw):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = it.cli_quit(**kw)
        return rc, buf.getvalue()

    def test_not_running(self):
        rc, out = self.run_quiet(_find=lambda: 0, _alive=lambda: False)
        self.assertEqual(rc, 0)
        self.assertIn("not running", out)

    def test_waits_until_gone(self):
        calls = {"find": 0, "post": 0}

        def find():
            calls["find"] += 1
            return 5 if calls["find"] < 3 else 0

        def post(h):
            calls["post"] += 1

        rc, out = self.run_quiet(_find=find, _post=post, _alive=lambda: False,
                                 _sleep=lambda s: None)
        self.assertEqual(rc, 0)
        self.assertEqual(calls["post"], 1)
        self.assertIn("stopped", out)

    def test_gives_up(self):
        rc, _out = self.run_quiet(timeout=0.2, _find=lambda: 5, _post=lambda h: None,
                                  _alive=lambda: True, _sleep=lambda s: time.sleep(0.01))
        self.assertEqual(rc, 1)

    def test_mutex_without_window(self):
        rc, _out = self.run_quiet(_find=lambda: 0, _alive=lambda: True)
        self.assertEqual(rc, 1)


class FakeApp(object):
    """Stands in for Win32App: records posted messages, optionally reacts."""

    def __init__(self):
        self.hwnd = 1
        self.posted = []
        self.hotkey_ok = True
        self.hotkey_error = ""
        self.on_post = None

    def post(self, msg, wparam=0, lparam=0):
        self.posted.append((msg, wparam))
        if self.on_post is not None:
            self.on_post(msg, wparam)
        return True


def bare_controller():
    """A Controller without Tk and without a Win32 window."""
    c = it.Controller.__new__(it.Controller)
    c._init_runtime()
    c.app = FakeApp()
    c.settings = it.default_settings()
    c.settings["devices"] = ["HID\\A\\1"]
    c.selection_label = lambda: "Example Mouse"
    return c


def _wait_for(cond, timeout=5.0):
    deadline = time.time() + timeout
    while not cond() and time.time() < deadline:
        time.sleep(0.01)
    return cond()


class ControllerTests(unittest.TestCase):
    def setUp(self):
        os.makedirs(DATADIR, exist_ok=True)
        for f in (it.state_path(), it.request_path()):
            if os.path.exists(f):
                os.remove(f)

    def test_manual_toggle_is_exclusive_and_reports(self):
        c = bare_controller()
        gate = threading.Event()
        calls = []

        def fake_toggle(forced):
            calls.append(forced)
            gate.wait(5)
            return {"ok": True, "action": "disable", "targets": ["HID\\A\\1"], "rc": 0}

        c.do_toggle = fake_toggle
        c._log_foreground = lambda before, res: None
        c.trigger_toggle("disable")                     # e.g. the menu's label action
        self.assertTrue(_wait_for(lambda: calls))
        self.assertTrue(c.is_busy())
        c.trigger_toggle(None)                          # hotkey while in flight: ignored
        c.trigger_enable_all()                          # menu while in flight: ignored
        gate.set()
        self.assertTrue(_wait_for(lambda: not c.is_busy() and c._results))
        self.assertEqual(calls, ["disable"])
        msgs = c.take_results()
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0][0], "Example Mouse is OFF. Press Ctrl+Alt+Shift+M "
                                     "to turn it back on.")
        self.assertTrue(c.helper_ok)
        self.assertIn(it.MSG_REFRESH, [m for m, _w in c.app.posted])

    def test_worker_start_failure_resets_busy(self):
        c = bare_controller()
        c.start_worker = lambda fn, name="w", daemon=True: None
        c.do_toggle = lambda forced: self.fail("must not run")
        c.trigger_toggle(None)
        self.assertFalse(c.is_busy())
        self.assertIn("worker thread", c.take_results()[0][0])

    def test_reregister_sequence(self):
        c = bare_controller()

        def win32_side(msg, seq):
            if msg == it.MSG_REREGISTER and seq:
                req = c.take_rereg(seq)
                req["ok"], req["reason"] = True, ""
                req["event"].set()

        c.app.on_post = win32_side
        self.assertEqual(c._reregister_sync({"key": "F13"}, it.DEFAULT_HOTKEY), (True, ""))
        self.assertEqual(c._rereg_reqs, {})

    def test_reregister_timeout_abandons_and_restores(self):
        c = bare_controller()
        ok, reason = c._reregister_sync({"key": "F13"}, it.DEFAULT_HOTKEY, timeout=0.05)
        self.assertFalse(ok)
        self.assertIn("timeout", reason)
        seqs = [w for m, w in c.app.posted if m == it.MSG_REREGISTER]
        self.assertEqual(seqs, [1, 0])              # the request, then "restore saved"
        self.assertIsNone(c.take_rereg(1))          # a late win32 thread finds nothing

    def test_crd_manual_override(self):
        c = bare_controller()
        calls = []
        orig_t, orig_e = it.toggle_core, it.enable_core

        def fake_toggle(ids, forced=None, auto=False, **kw):
            calls.append(("toggle", forced, auto))
            return {"ok": True, "action": forced or "disable", "targets": list(ids), "rc": 0}

        def fake_enable(ids, **kw):
            calls.append(("enable", list(ids)))
            return {"ok": True, "action": "enable", "targets": list(ids), "rc": 0}

        it.toggle_core, it.enable_core = fake_toggle, fake_enable
        try:
            it.save_state({"disabled": ["HID\\A\\1"], "auto_disabled": ["HID\\A\\1"]})
            c._crd_start()
            self.assertEqual(calls[-1], ("toggle", "disable", True))
            self.assertTrue(c._crd_active)
            c._crd_end()                                 # no manual change: re-enable
            self.assertEqual(calls[-1], ("enable", ["HID\\A\\1"]))
            c._crd_start()
            c.do_toggle("enable")                        # the user switched manually
            self.assertTrue(c._crd_manual_override)
            n = len(calls)
            self.assertIsNone(c._crd_end())              # ... so the end leaves it alone
            self.assertEqual(len(calls), n)
        finally:
            it.toggle_core, it.enable_core = orig_t, orig_e

    def test_end_session_is_bounded_when_an_operation_holds_the_lock(self):
        c = bare_controller()
        orig = it.enable_all_core
        seen = {}

        def fake_enable_all(selected, fallback=True, **kw):
            seen.update(kw)
            return {"ok": True, "outcome": "x", "targets": []}

        it.enable_all_core = fake_enable_all
        c._op_lock.acquire()                             # an operation is stuck
        try:
            t0 = time.monotonic()
            c.on_end_session_sync()
            dt = time.monotonic() - t0
        finally:
            c._op_lock.release()
            it.enable_all_core = orig
        self.assertLess(dt, 2.0)
        self.assertEqual(seen.get("timeout"), 2.0)
        self.assertEqual(seen.get("run_timeout"), 3.0)
        self.assertIs(seen.get("check_missing"), False)
        self.assertIs(seen.get("cleanup"), False)
        self.assertTrue(c._ended_session)

    def test_exit_worker_reenables_then_signals(self):
        c = bare_controller()
        called = []

        def fake_enable_all(fallback=True, manual=False, **kw):
            called.append(fallback)
            return {"ok": True, "outcome": "done", "targets": []}

        c.do_enable_all = fake_enable_all
        c._exit_worker()
        self.assertEqual(called, [True])
        self.assertEqual(c.q.get_nowait(), ("exit_ready",))
        c.settings["reenable_on_exit"] = False
        c._exit_worker()                                 # only waits for the lock
        self.assertEqual(called, [True])
        self.assertEqual(c.q.get_nowait(), ("exit_ready",))


class SnapshotTests(unittest.TestCase):
    def test_states(self):
        A, B = "HID\\VID_1234&PID_5678&MI_00\\8&x", "HID\\VID_1234&PID_5678&MI_01\\8&y"
        def snap(states, ids):
            fake = FakeDevices(states)
            return it.selection_snapshot(ids, _status=fake.status)["state"]
        self.assertEqual(snap({}, []), "nothing")
        self.assertEqual(snap({}, [A]), "absent")
        self.assertEqual(snap({A: True, B: True}, [A, B]), "enabled")
        self.assertEqual(snap({A: False, B: False}, [A, B]), "disabled")
        self.assertEqual(snap({A: True, B: False}, [A, B]), "mixed")


class FrozenModeTests(unittest.TestCase):
    """What differs in the PyInstaller build (it.FROZEN)."""

    def setUp(self):
        self._frozen = it.FROZEN

    def tearDown(self):
        it.FROZEN = self._frozen

    def test_helper_hint_points_at_the_installer(self):
        it.FROZEN = True
        self.assertEqual(it.helper_fix_hint(), "reinstall InputToggle")
        for text in (it.status_text("enabled", "X", helper_ok=False),
                     it.build_tooltip("enabled", "X", "K", helper_ok=False),
                     it.outcome_message({"ok": False, "task_missing": True}, "X")):
            self.assertIn("reinstall InputToggle", text)
        it.FROZEN = False
        for text in (it.status_text("enabled", "X", helper_ok=False),
                     it.build_tooltip("enabled", "X", "K", helper_ok=False),
                     it.outcome_message({"ok": False, "task_missing": True}, "X")):
            self.assertIn("setup program", text)
            self.assertNotIn(".ps1", text)

    def test_logon_command(self):
        exe = r"C:\Program Files\InputToggle\app\InputToggle.exe"
        self.assertEqual(it.run_on_logon_command(frozen=True, executable=exe), '"%s"' % exe)
        src = it.run_on_logon_command(script=r"C:\x\inputtoggle.pyw", frozen=False)
        self.assertTrue(src.endswith('"C:\\x\\inputtoggle.pyw"'), src)
        self.assertEqual(src.count('"'), 4)               # "pythonw.exe" "script"

    def test_same_run_command(self):
        self.assertTrue(it.same_run_command('"C:\\A\\B.exe"', '"c:\\a\\b.exe" '))
        self.assertFalse(it.same_run_command('"C:\\A\\B.exe"', '"C:\\A\\C.exe"'))
        self.assertFalse(it.same_run_command(None, '"C:\\A\\B.exe"'))
        self.assertTrue(it.same_run_command(None, None))

    def test_tray_identity_differs_between_builds(self):
        self.assertNotEqual(bytes(it.TRAY_GUID_FROZEN), bytes(it.TRAY_GUID_SOURCE))


class RunOnLogonSyncTests(unittest.TestCase):
    """sync_run_on_logon with a fake registry (the real one is never touched)."""

    def setUp(self):
        self._frozen = it.FROZEN
        it.FROZEN = True
        self.reg = {"value": None, "calls": []}

    def tearDown(self):
        it.FROZEN = self._frozen

    def sync(self, want):
        def get():
            return self.reg["value"]

        def put(enable):
            self.reg["calls"].append(enable)
            self.reg["value"] = it.run_on_logon_command() if enable else None

        return it.sync_run_on_logon(want, _get=get, _set=put)

    def test_missing_entry_is_added(self):
        self.assertEqual(self.sync(True), "set")
        self.assertEqual(self.reg["calls"], [True])

    def test_matching_entry_is_left_alone(self):
        self.reg["value"] = it.run_on_logon_command()
        self.assertEqual(self.sync(True), "")
        self.assertEqual(self.reg["calls"], [])

    def test_entry_of_an_older_copy_is_replaced(self):
        self.reg["value"] = '"C:\\Python\\pythonw.exe" "C:\\old\\inputtoggle.pyw"'
        self.assertEqual(self.sync(True), "set")
        self.assertTrue(it.same_run_command(self.reg["value"], it.run_on_logon_command()))

    def test_setting_off_removes_the_entry(self):
        self.reg["value"] = it.run_on_logon_command()
        self.assertEqual(self.sync(False), "removed")
        self.assertIsNone(self.reg["value"])
        self.assertEqual(self.sync(False), "")


class FirstRunTests(unittest.TestCase):
    def setUp(self):
        os.makedirs(DATADIR, exist_ok=True)
        if os.path.exists(it.settings_path()):
            os.remove(it.settings_path())

    tearDown = setUp

    def test_plan(self):
        s, show = it.first_run_plan(it.default_settings())
        self.assertTrue(show)
        self.assertTrue(s["first_run_done"])
        self.assertEqual(s["devices"], [])                # nothing is pre-selected
        self.assertFalse(it.first_run_plan(s)[1])         # only once
        upgrade = dict(it.default_settings(), devices=["HID\\A\\1"])
        s3, show3 = it.first_run_plan(upgrade)
        self.assertFalse(show3)
        self.assertEqual(s3["devices"], ["HID\\A\\1"])

    def test_controller_opens_settings_on_the_first_start_only(self):
        c = it.Controller.__new__(it.Controller)
        c._init_runtime()
        self.assertTrue(c.first_run)
        c._first_run_init()
        self.assertTrue(c.open_settings_on_start)
        self.assertEqual(c.selected_ids(), [])
        self.assertTrue(it.load_settings()["first_run_done"])     # saved
        c2 = it.Controller.__new__(it.Controller)
        c2._init_runtime()
        c2._first_run_init()
        self.assertFalse(c2.open_settings_on_start)


class IconOrderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.join(ROOT, "_testtmp", "icons_%d" % os.getpid())
        os.makedirs(os.path.join(self.tmp, "icons"), exist_ok=True)
        self._resource_dir = it.resource_dir

    def tearDown(self):
        it.resource_dir = self._resource_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_bundled_icons_come_first(self):
        for n in (it.ICON_ON_NAME, it.ICON_OFF_NAME):
            with open(os.path.join(self.tmp, "icons", n), "wb") as fh:
                fh.write(b"\0")
        it.resource_dir = lambda: self.tmp
        self.assertEqual(it.ensure_icons(),
                         (os.path.join(self.tmp, "icons", it.ICON_ON_NAME),
                          os.path.join(self.tmp, "icons", it.ICON_OFF_NAME)))

    def test_missing_bundle(self):
        it.resource_dir = lambda: self.tmp                # empty icons folder
        self.assertIsNone(it.bundled_icon(it.ICON_ON_NAME))
        self.assertIsNone(it.bundled_icon(it.APP_ICON_NAME))

    def test_source_resource_dir(self):
        self.assertEqual(self._resource_dir(), os.path.dirname(PYW))


class CliTests(unittest.TestCase):
    """main() and the output helper. Every entry point that could start the
    tray application or touch a device is replaced by a trap first."""

    TRAPPED = ("run_app", "cli_quit", "cli_toggle", "cli_enable_all", "cli_status")

    def setUp(self):
        self.tmp = os.path.join(ROOT, "_testtmp", "cli_%d" % os.getpid())
        os.makedirs(self.tmp, exist_ok=True)
        self.saved = {n: getattr(it, n) for n in self.TRAPPED}
        for n in self.TRAPPED:
            setattr(it, n, self._trap(n))
        self._output = it.OUTPUT
        it.OUTPUT = it.CliOutput()

    def tearDown(self):
        for n, fn in self.saved.items():
            setattr(it, n, fn)
        it.OUTPUT = self._output
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _trap(name):
        def trap(*a, **k):
            raise AssertionError("%s must not run in this test" % name)
        return trap

    @staticmethod
    def _read(path):
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()

    def test_version_to_a_file_without_a_console(self):
        out = os.path.join(self.tmp, "version.txt")
        with contextlib.redirect_stdout(None):             # like the windowed build
            rc = it.main(["InputToggle.exe", "--version", "--out", out])
        self.assertEqual(rc, 0)
        self.assertEqual(self._read(out).splitlines(), ["InputToggle %s" % it.VERSION])

    def test_version_writes_nothing_else(self):
        target = os.path.join(self.tmp, "no-data-here")
        os.environ["INPUTTOGGLE_DATADIR"] = target
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = it.main(["x", "--version"])
        finally:
            os.environ["INPUTTOGGLE_DATADIR"] = DATADIR
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue().strip(), "InputToggle %s" % it.VERSION)
        self.assertFalse(os.path.exists(target))

    def test_out_never_starts_the_app_or_a_device_command(self):
        out = os.path.join(self.tmp, "refused.txt")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(it.main(["x", "--out", out]), 2)
            self.assertIn("ERROR", self._read(out))
            for extra in ("--toggle", "--quit", "--enable-all", "--settings"):
                self.assertEqual(it.main(["x", extra, "--out", out]), 2, extra)
        self.assertIn("--out can only be used with", buf.getvalue())

    def test_out_needs_a_file_name(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(it.main(["x", "--version", "--out"]), 2)
            self.assertEqual(it.main(["x", "--out", "--version"]), 2)
        self.assertIn("--out needs a file name", buf.getvalue())

    def test_status_output_goes_to_the_file(self):
        out = os.path.join(self.tmp, "status.txt")

        def fake_status():
            it.say("inventory line")
            return 0

        it.cli_status = fake_status
        with contextlib.redirect_stdout(None):
            rc = it.main(["x", "--status", "--out", out])
        self.assertEqual(rc, 0)
        self.assertIn("inventory line", self._read(out))

    def test_errors_are_reported_not_raised(self):
        out = os.path.join(self.tmp, "error.txt")

        def boom():
            raise RuntimeError("boom")

        it.cli_status = boom
        with contextlib.redirect_stdout(None):
            rc = it.main(["x", "--status", "--out", out])
        self.assertEqual(rc, 3)
        self.assertIn("ERROR", self._read(out))

    def test_say_survives_text_the_console_cannot_encode(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="ascii", errors="strict", newline="\n")
        with contextlib.redirect_stdout(stream):
            it.say("caf\u00e9")
        stream.flush()
        self.assertEqual(raw.getvalue(), b"caf?\n")


class ElevationTests(unittest.TestCase):
    def test_only_the_elevated_half_of_a_split_token_is_refused(self):
        self.assertTrue(it.refuse_elevated(it.TOKEN_ELEVATION_TYPE_FULL))
        for t in (it.TOKEN_ELEVATION_TYPE_DEFAULT, it.TOKEN_ELEVATION_TYPE_LIMITED, None):
            self.assertFalse(it.refuse_elevated(t), t)

    def test_token_type_is_readable(self):
        self.assertIn(it.token_elevation_type(), (1, 2, 3))


class RepositoryContractTests(unittest.TestCase):
    """Values other files of the repository rely on."""

    @staticmethod
    def read(*parts):
        with open(os.path.join(ROOT, *parts), "r", encoding="utf-8") as fh:
            return fh.read()

    def test_no_psscriptroot_in_parameter_defaults(self):
        # Windows PowerShell 5.1 leaves $PSScriptRoot empty in the param()
        # defaults of an advanced script started with -File (1.2.0 shipped
        # that bug: the installer could not register the task).
        for rel in (("installer", "register_task.ps1"), ("installer", "unregister_task.ps1"),
                    ("build.ps1",)):
            text = self.read(*rel)
            m = re.search(r"(?ims)^\s*param\s*\((.*?)^\s*\)\s*$", text)
            self.assertIsNotNone(m, "no param() block in %s" % "/".join(rel))
            code = "\n".join(ln.split("#", 1)[0] for ln in m.group(1).splitlines())
            self.assertNotIn("$PSScriptRoot", code,
                             "%s uses $PSScriptRoot in a parameter default" % "/".join(rel))

    @unittest.skipUnless(sys.platform == "win32", "needs Windows PowerShell")
    def test_register_script_starts_under_windows_powershell(self):
        # Run it the way the installer does. Unelevated it must stop at its
        # elevation check (exit 3); elevated (a CI runner) at its own checks.
        # Either way its own code must run: no parameter-binding error.
        import subprocess
        ps = os.path.join(os.environ.get("SystemRoot", "C:\\Windows"),
                          "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        script = os.path.join(ROOT, "installer", "register_task.ps1")
        p = subprocess.run([ps, "-NoLogo", "-NoProfile", "-NonInteractive",
                            "-ExecutionPolicy", "Bypass", "-File", script],
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=120,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = p.stdout.decode("utf-8", "replace")
        self.assertNotIn("Cannot bind argument", out)
        self.assertIn("register:", out)
        self.assertNotEqual(p.returncode, 0, out)

    def test_installer_can_stop_any_version(self):
        iss = self.read("installer", "InputToggle.iss")
        self.assertEqual(it.WND_CLASS, "InputToggleWndCls")
        self.assertEqual(it.MUTEX_NAME, "Local\\InputToggle")
        self.assertEqual(it.MSG_REQUEST_QUIT, 0x8009)
        self.assertIn("InstanceWindowClass = '%s';" % it.WND_CLASS, iss)
        self.assertIn("InstanceMutex = '%s';" % it.MUTEX_NAME, iss)
        self.assertIn("MsgRequestQuit = $%04X;" % it.MSG_REQUEST_QUIT, iss)

    def test_task_and_log_locations_agree(self):
        self.assertEqual(it.TASK_NAME, "\\InputToggle\\Apply")
        reg = self.read("installer", "register_task.ps1")
        unreg = self.read("installer", "unregister_task.ps1")
        for text in (reg, unreg):
            self.assertIn("'\\InputToggle\\'", text)
            self.assertIn("'Apply'", text)
        self.assertEqual(it.HELPER_LOG_HINT, r"C:\Program Files\InputToggle\logs\helper.log")
        helper = self.read("helper", "InputToggleHelper.cs")
        self.assertIn('Path.Combine(Path.Combine(pf, "InputToggle"), "logs")', helper)
        self.assertIn('"helper.log"', helper)
        self.assertIn('Path.Combine(Path.Combine(local, "InputToggle"), "request.txt")', helper)
        iss = self.read("installer", "InputToggle.iss")
        self.assertIn("DefaultDirName={autopf}\\{#AppName}", iss)
        self.assertIn('#define AppName "InputToggle"', iss)

    def test_changelog_has_this_version(self):
        self.assertRegex(it.VERSION, r"^\d+\.\d+\.\d+$")
        self.assertRegex(self.read("CHANGELOG.md"), r"(?m)^## \[%s\]" % re.escape(it.VERSION))

    def test_build_requirements_are_pinned_with_hashes(self):
        text = self.read("requirements-build.txt")
        # one or more hashes per pin (Dependabot writes one per file of a release)
        reqs = re.findall(r"(?m)^([A-Za-z0-9_.-]+)==(\S+)(?: \\\n\s+--hash=sha256:[0-9a-f]{64})+$",
                          text)
        names = set(n.lower() for n, _v in reqs)
        for n in ("pyinstaller", "pyinstaller-hooks-contrib", "pillow"):
            self.assertIn(n, names)
        lines = [ln for ln in text.splitlines()
                 if ln.strip() and not ln.strip().startswith(("#", "--hash"))]
        self.assertEqual(len(lines), len(reqs), "every requirement needs ==version and a hash")

    def test_windows_scripts_are_ascii(self):
        # Windows PowerShell 5.1 reads a script without a BOM in the ANSI code page
        for rel in (("build.ps1",), ("installer", "register_task.ps1"),
                    ("installer", "unregister_task.ps1"), ("installer", "InputToggle.iss"),
                    ("helper", "InputToggleHelper.cs")):
            with open(os.path.join(ROOT, *rel), "rb") as fh:
                data = fh.read()
            self.assertTrue(all(b < 128 for b in data), os.path.join(*rel))


if __name__ == "__main__":
    unittest.main(verbosity=2)
