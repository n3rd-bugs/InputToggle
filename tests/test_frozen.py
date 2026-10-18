# -*- coding: utf-8 -*-
"""Checks on the built InputToggle.exe that are safe on any machine, CI
included.

Only the two read-only modes are run, each with --out (a windowed exe has no
console): --version and --selftest. Neither starts the tray application,
takes the single-instance mutex, opens a window or changes a device. The exe
gets a private data folder (INPUTTOGGLE_DATADIR) inside the repository.

    INPUTTOGGLE_FROZEN_EXE      path of InputToggle.exe
                                (default: build\\app\\InputToggle\\InputToggle.exe)
    INPUTTOGGLE_REQUIRE_FROZEN  1 = fail instead of skipping when it is missing
"""
import os
import re
import shutil
import subprocess
import sys
import unittest

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src", "inputtoggle.pyw")
EXE = os.environ.get("INPUTTOGGLE_FROZEN_EXE") or os.path.join(
    ROOT, "build", "app", "InputToggle", "InputToggle.exe")
REQUIRE = os.environ.get("INPUTTOGGLE_REQUIRE_FROZEN") == "1"
TMP = os.path.join(ROOT, "_testtmp", "frozen_%d" % os.getpid())
CREATE_NO_WINDOW = 0x08000000


def source_version():
    with open(SRC, "r", encoding="utf-8") as fh:
        m = re.search(r'^VERSION\s*=\s*"(\d+\.\d+\.\d+)"\s*$', fh.read(), re.M)
    return m.group(1)


class FrozenExeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.path.isfile(EXE):
            if REQUIRE:
                raise AssertionError("frozen exe not found: %s" % EXE)
            raise unittest.SkipTest("frozen exe not built: %s" % EXE)
        os.makedirs(TMP, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TMP, ignore_errors=True)

    def run_exe(self, mode, name):
        """Run EXE <mode> --out <file>; return (exit code, output text)."""
        self.assertIn(mode, ("--version", "--selftest"))   # nothing else, ever
        out = os.path.join(TMP, name + ".txt")
        if os.path.exists(out):
            os.remove(out)
        env = dict(os.environ)
        env["INPUTTOGGLE_DATADIR"] = os.path.join(TMP, "data")
        env.pop("INPUTTOGGLE_TEST", None)
        p = subprocess.run([EXE, mode, "--out", out], env=env, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180,
                           creationflags=CREATE_NO_WINDOW)
        text = ""
        if os.path.exists(out):
            with open(out, "r", encoding="utf-8") as fh:
                text = fh.read()
        return p.returncode, text

    def test_version(self):
        rc, text = self.run_exe("--version", "version")
        self.assertEqual(rc, 0, text)
        self.assertEqual(text.splitlines()[0], "InputToggle %s" % source_version())

    def test_selftest(self):
        rc, text = self.run_exe("--selftest", "selftest")
        self.assertEqual(rc, 0, text)
        self.assertIn("RESULT: PASS", text)
        self.assertIn("release build", text)               # it knows it is frozen
        self.assertRegex(text, r"\[PASS\] bundled icons - icon_on\.ico, icon_off\.ico, app\.ico")
        self.assertIn("[PASS] enumerate devices", text)
        self.assertIn("[PASS] data dir writable", text)
        self.assertIn("[PASS] Tcl/Tk runtime", text)       # tkinter really works
        self.assertNotIn("[FAIL]", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
