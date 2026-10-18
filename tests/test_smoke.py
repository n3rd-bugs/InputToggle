# -*- coding: utf-8 -*-
"""Local smoke tests for InputToggle (they need a real desktop session).

Everything is skipped unless INPUTTOGGLE_LOCAL_SMOKE=1, so CI never runs
them. They only do things that cannot change a device:

- --status and --selftest: read-only enumeration (plus a read-only query of
  the helper task)
- the F24 hotkey self-test, only with INPUTTOGGLE_ALLOW_F24=1 as well: it
  registers plain F24, injects ONE F24 press, waits for WM_HOTKEY and
  unregisters. The hotkey consumes F24, so no application sees it.
- Settings and the Controller built with a withdrawn Tk root (never mapped)
- the helper compiled into build\\smoke and run UNELEVATED in its test-only
  --validate-only mode (and in the refusal cases), against requests written
  under _testtmp; it never enables or disables anything
- a PowerShell parse of every .ps1 (nothing is executed)

Run:  set INPUTTOGGLE_LOCAL_SMOKE=1 && python -I tests\\test_smoke.py
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import time
import unittest

sys.dont_write_bytecode = True     # keep the repository free of __pycache__

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PYW = os.path.join(ROOT, "src", "inputtoggle.pyw")
HELPER_SRC = os.path.join(ROOT, "helper", "InputToggleHelper.cs")
BUILD = os.path.join(ROOT, "build", "smoke")
PY = sys.executable
CSC = os.path.join(os.environ.get("WINDIR", r"C:\Windows"),
                   r"Microsoft.NET\Framework64\v4.0.30319\csc.exe")
CREATE_NO_WINDOW = 0x08000000
LOCAL = os.environ.get("INPUTTOGGLE_LOCAL_SMOKE") == "1"
SKIP_REASON = "local smoke test (set INPUTTOGGLE_LOCAL_SMOKE=1)"

DATADIR = os.path.join(ROOT, "_testtmp", "smoke")
os.environ["INPUTTOGGLE_DATADIR"] = DATADIR       # also for the in-process import


def _env():
    e = dict(os.environ)
    e["INPUTTOGGLE_DATADIR"] = DATADIR
    e.pop("INPUTTOGGLE_TEST", None)
    return e


def _load():
    spec = importlib.util.spec_from_file_location("inputtoggle_mod_smoke", PYW)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _run_py(args, timeout=60):
    return subprocess.run([PY, PYW] + args, env=_env(), stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, timeout=timeout,
                          creationflags=CREATE_NO_WINDOW)


@unittest.skipUnless(LOCAL, SKIP_REASON)
class StatusTests(unittest.TestCase):
    def test_status_enumeration(self):
        p = _run_py(["--status"])
        out = p.stdout
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("InputToggle device inventory (Mouse + Keyboard classes)", out)
        self.assertRegex(out, r"\d+ groups, \d+ present devnode\(s\)")
        # a desktop PC has at least one keyboard or mouse; each shows ON or OFF
        self.assertIn("GROUP:", out)
        self.assertRegex(out, r"\[(ON |OFF)\] (Mouse   |Keyboard) vid=")

    def test_readonly_selftest(self):
        p = _run_py(["--selftest"])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("RESULT: PASS", p.stdout)
        self.assertIn("from source", p.stdout)


@unittest.skipUnless(LOCAL, SKIP_REASON)
@unittest.skipUnless(os.environ.get("INPUTTOGGLE_ALLOW_F24") == "1",
                     "F24 injection not enabled (set INPUTTOGGLE_ALLOW_F24=1)")
class HotkeyTests(unittest.TestCase):
    def test_f24_roundtrip(self):
        p = _run_py(["--selftest-hotkey", "--attempts", "1"], timeout=30)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("PASS", p.stdout)
        self.assertIn("WM_HOTKEY", p.stdout)


@unittest.skipUnless(LOCAL, SKIP_REASON)
class SettingsTests(unittest.TestCase):
    def test_settings_withdrawn_and_fits(self):
        p = _run_py(["--selftest-settings"])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("PASS", p.stdout)
        self.assertIn("Settings built withdrawn", p.stdout)
        self.assertIn("layout fits", p.stdout)


@unittest.skipUnless(LOCAL, SKIP_REASON)
class ControllerWiringTests(unittest.TestCase):
    def test_controller_builds_withdrawn(self):
        # fresh data dir, so the first-run plan really runs
        d = os.path.join(ROOT, "_testtmp", "controller_%d" % os.getpid())
        shutil.rmtree(d, ignore_errors=True)
        e = _env()
        e["INPUTTOGGLE_DATADIR"] = d
        try:
            p = subprocess.run([PY, PYW, "--selftest-controller"], env=e,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               timeout=60, creationflags=CREATE_NO_WINDOW)
        finally:
            shutil.rmtree(d, ignore_errors=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("PASS controller built withdrawn", p.stdout)
        # a fresh install pre-selects nothing and opens Settings once
        self.assertIn("0 device(s) selected", p.stdout)
        self.assertIn("open_settings=True", p.stdout)
        self.assertIn("state=nothing", p.stdout)


@unittest.skipUnless(LOCAL, SKIP_REASON)
class HelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.path.exists(CSC):
            raise unittest.SkipTest("csc.exe not found: %s" % CSC)
        os.makedirs(BUILD, exist_ok=True)
        cls.exe = os.path.join(BUILD, "InputToggleHelper.exe")
        # the same switches build.ps1 uses
        c = subprocess.run([CSC, "/nologo", "/target:winexe", "/optimize+", "/langversion:5",
                            "/platform:anycpu", "/warnaserror+", "/out:" + cls.exe, HELPER_SRC],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True, timeout=120, creationflags=CREATE_NO_WINDOW)
        cls.compile_rc = c.returncode
        cls.compile_out = c.stdout + c.stderr
        cls.mod = _load()
        cls.tmp = os.path.join(ROOT, "_testtmp", "helper_%d" % os.getpid())
        os.makedirs(cls.tmp, exist_ok=True)
        cls.junctions = []

    @classmethod
    def tearDownClass(cls):
        for j in cls.junctions:                       # remove links before the tree
            try:
                os.rmdir(j)
            except OSError:
                pass
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.assertEqual(self.compile_rc, 0, self.compile_out)

    def _write(self, name, text):
        path = os.path.join(self.tmp, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        return path

    def _helper(self, args, test_env=True):
        e = _env()
        if test_env:
            e["INPUTTOGGLE_TEST"] = "1"
        return subprocess.run([self.exe] + args, env=e, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=30).returncode

    def _validate(self, req, name):
        out = os.path.join(self.tmp, name + ".out.txt")
        if os.path.exists(out):
            os.remove(out)
        rc = self._helper(["--validate-only", "--request", req, "--out", out])
        summary = ""
        if os.path.exists(out):
            with open(out, "r", encoding="utf-8") as fh:
                summary = fh.read().strip()
        return rc, summary, out

    def test_compile_and_validate_live_devices(self):
        self.assertTrue(os.path.exists(self.exe))
        # A request built from the LIVE present Mouse-class devnodes, validated
        # only (no device change), plus one id of the wrong class.
        groups = self.mod.enumerate_devices(False)
        mice = [d["id"] for d in self.mod.flatten(groups)
                if d.get("present") and d.get("class_name") == "Mouse"
                and not d.get("virtual")][:8]
        if not mice:
            self.skipTest("no present Mouse-class devnode on this PC")
        composite = [d.get("group_key") for d in self.mod.flatten(groups)
                     if d.get("present") and d.get("group_key")
                     and self.mod.is_composite_parent_id(d.get("group_key"))]
        wrong_class = composite[0] if composite else "USB\\VID_1234&PID_5678\\NOTACLASS"
        ids = mice + [wrong_class]
        req = self._write(os.path.join("local", "InputToggle", "request.txt"),
                          self.mod.format_request("disable", ids))
        rc, summary, _out = self._validate(req, "live")
        self.assertEqual(rc, 0, "helper exit %s (%s)" % (rc, summary))
        self.assertIn("VALIDATE action=disable", summary)
        # every present mouse node validates; the wrong-class node is skipped
        self.assertIn("ok=%d" % len(mice), summary)
        self.assertIn("skipped=1", summary)

    def test_test_switches_refused_without_env(self):
        req = self._write("noenv\\request.txt", self.mod.format_request("disable", ["HID\\X\\1"]))
        out = os.path.join(self.tmp, "noenv.out.txt")
        rc = self._helper(["--validate-only", "--request", req, "--out", out], test_env=False)
        self.assertEqual(rc, 4)
        self.assertFalse(os.path.exists(out), "--out must not be written when refused")

    def test_unknown_argument_refused(self):
        missing = os.path.join(self.tmp, "does-not-exist.txt")
        rc = self._helper(["--validate-only", "--request", missing, "--bogus"])
        self.assertEqual(rc, 4)

    def test_stale_future_and_undated_requests_refused(self):
        now = int(time.time())
        cases = [("stale", self.mod.format_request("enable", ["HID\\X\\1"], issued=now - 3600),
                  "ERROR stale-request"),
                 ("future", self.mod.format_request("enable", ["HID\\X\\1"], issued=now + 3600),
                  "ERROR stale-request"),
                 ("undated", "action=enable\ndevice=HID\\X\\1\n", "ERROR missing-issued"),
                 ("badaction", "action=nuke\nissued=%d\n" % now, "ERROR bad-action")]
        for name, text, expect in cases:
            req = self._write(name + "\\request.txt", text)
            rc, summary, _out = self._validate(req, name)
            self.assertEqual(rc, 2, name)
            self.assertEqual(summary, expect, name)

    def test_oversize_request_refused(self):
        text = self.mod.format_request("enable", ["HID\\X\\1"]) + ("#" * 100 + "\n") * 200
        req = self._write("big\\request.txt", text)
        rc, summary, _out = self._validate(req, "big")
        self.assertEqual(rc, 2)
        self.assertEqual(summary, "ERROR request-too-large")

    def _junction(self, target, link):
        import _winapi
        os.makedirs(target, exist_ok=True)
        _winapi.CreateJunction(target, link)
        self.junctions.append(link)

    def test_junction_paths_refused(self):
        text = self.mod.format_request("enable", ["HID\\X\\1"])
        # the request's own folder is a junction
        tgt = os.path.join(self.tmp, "jtarget")
        self._write("jtarget\\request.txt", text)
        link = os.path.join(self.tmp, "jlink")
        self._junction(tgt, link)
        rc, summary, _out = self._validate(os.path.join(link, "request.txt"), "jdir")
        self.assertEqual(rc, 2)
        self.assertEqual(summary, "ERROR request-dir-is-reparse-point")
        # a junction further up: caught by the final-path check of the handle
        tgt2 = os.path.join(self.tmp, "jtarget2")
        self._write("jtarget2\\sub\\request.txt", text)
        link2 = os.path.join(self.tmp, "jlink2")
        self._junction(tgt2, link2)
        rc, summary, _out = self._validate(os.path.join(link2, "sub", "request.txt"), "jup")
        self.assertEqual(rc, 2)
        self.assertEqual(summary, "ERROR request-path-mismatch")
        # the same file through its real path is accepted
        rc, summary, _out = self._validate(os.path.join(tgt2, "sub", "request.txt"), "jreal")
        self.assertEqual(rc, 0, summary)
        self.assertTrue(summary.startswith("VALIDATE action=enable"), summary)


@unittest.skipUnless(LOCAL, SKIP_REASON)
class PowerShellParseTests(unittest.TestCase):
    def test_all_ps1_parse(self):
        ps1 = [os.path.join(ROOT, "build.ps1")]
        inst = os.path.join(ROOT, "installer")
        ps1 += [os.path.join(inst, f) for f in os.listdir(inst) if f.lower().endswith(".ps1")]
        self.assertGreaterEqual(len(ps1), 3, ps1)
        for path in ps1:
            script = (
                "$errs=$null; $toks=$null;"
                "[System.Management.Automation.Language.Parser]::ParseFile("
                "'%s',[ref]$toks,[ref]$errs) | Out-Null;"
                "if($errs -and $errs.Count){ $errs | ForEach-Object { $_.Message };"
                "exit 1 } else { 'OK' }" % path.replace("'", "''")
            )
            p = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive",
                               "-Command", script],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, timeout=60, creationflags=CREATE_NO_WINDOW)
            self.assertEqual(p.returncode, 0,
                             "parse errors in %s:\n%s" % (path, p.stdout + p.stderr))


if __name__ == "__main__":
    unittest.main(verbosity=2)
