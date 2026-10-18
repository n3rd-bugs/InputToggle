// InputToggleHelper.exe - the ONLY elevated part of InputToggle.
//
// Started solely by the on-demand Scheduled Task \InputToggle\Apply
// (RunLevel Highest), which the installer registers for the installing
// account. It reads the user-writable request file, validates it as untrusted
// input, and enables/disables ONLY Mouse- or Keyboard-class device nodes named
// in it. It is installed to C:\Program Files\InputToggle, where only
// administrators can change it.
//
// Disable is NON-persistent. cfgmgr32.h (SDK 10.0.26100) defines:
//   CM_DISABLE_POLITE    0x0  "Ask the driver"
//   CM_DISABLE_ABSOLUTE  0x1  "Don't ask the driver"
//   CM_DISABLE_HARDWARE  0x2  "Don't ask the driver, and won't be restarteable"
//   CM_DISABLE_UI_NOT_OK 0x4  "Don't popup any veto API"
//   CM_DISABLE_PERSIST   0x8  "Persists through restart by setting
//                              CONFIGFLAG_DISABLED in the registry"
// This helper passes CM_DISABLE_UI_NOT_OK only (never CM_DISABLE_PERSIST), so
// no registry flag is written and a restart always brings the device back.
//
// Build (build.ps1 does this; the .NET Framework 4.x compiler is part of
// Windows):
//   %WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe /nologo ^
//       /target:winexe /optimize+ /langversion:5 /platform:anycpu ^
//       /out:InputToggleHelper.exe InputToggleHelper.cs
// /target:winexe => no console subsystem, so no console window can ever flash.
// /platform:anycpu (not 32-bit preferred) => a 64-bit process on 64-bit
// Windows, so the SetupAPI fallback below never runs under WOW64.
//
// Language level: C# 5 / .NET Framework 4.x only (no C# 6+ syntax).
//
// Normal run (by the task): no arguments; the request is read from
//   %LOCALAPPDATA%\InputToggle\request.txt  (Environment.SpecialFolder.LocalApplicationData)
// The request must be <= 16 KB, must not be (or sit in) a junction/symlink,
// must open to exactly that path, and must carry issued=<unix seconds> no older
// than 60 s (and at most 5 s in the future), so a stale file is never replayed.
//
// Test-only switches. They are honoured ONLY when the environment variable
// INPUTTOGGLE_TEST=1 is set AND the process is NOT elevated; otherwise the
// helper refuses to run (exit 4), so they can never become an elevated
// file-read/write primitive even if the task definition were changed.
//   --validate-only        parse + resolve + validate; never Disable/Enable
//   --request <path>       read the request from <path> instead of LOCALAPPDATA
//   --out <path>           write a one-line result summary to <path>
//
// Exit codes: 0 ok, 1 some device failed, 2 missing/invalid/stale request,
//             3 unexpected error, 4 refused arguments.

using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Principal;
using System.Text;
using System.Threading;
using Microsoft.Win32.SafeHandles;

namespace InputToggle
{
    internal static class Helper
    {
        // ---- CfgMgr32 (verified against cfgmgr32.h / cfg.h, SDK 10.0.26100) ----
        private const uint CR_SUCCESS = 0x00000000;
        private const uint CR_BUFFER_SMALL = 0x0000001A;
        private const uint CM_LOCATE_DEVNODE_NORMAL = 0x00000000;
        private const uint CM_PROB_DISABLED = 0x00000016;          // 22
        private const uint DN_HAS_PROBLEM = 0x00000400;
        private const uint CM_DISABLE_UI_NOT_OK = 0x00000004;      // no CM_DISABLE_PERSIST(0x8)
        private const uint DEVPROP_TYPE_GUID = 0x0000000D;

        // ---- SetupAPI (enable fallback for a persistently-disabled device) ----
        private const uint DIF_PROPERTYCHANGE = 0x00000012;
        private const uint DICS_ENABLE = 0x00000001;
        private const uint DICS_FLAG_GLOBAL = 0x00000001;
        private const uint DICS_FLAG_CONFIGSPECIFIC = 0x00000002;

        private static readonly Guid GUID_MOUSE =
            new Guid("4d36e96f-e325-11ce-bfc1-08002be10318");
        private static readonly Guid GUID_KEYBOARD =
            new Guid("4d36e96b-e325-11ce-bfc1-08002be10318");

        private const int MAX_DEVICE_LINES = 32;
        private const int MAX_ID_LEN = 400;
        private const int MAX_REQUEST_BYTES = 16384;
        private const int MAX_REQUEST_LINES = 64;
        private const long MAX_AGE_SECONDS = 60;
        private const long MAX_FUTURE_SECONDS = 5;
        private const int STATUS_WAIT_MS = 2000;

        private const int EXIT_OK = 0;
        private const int EXIT_DEVICE_FAILED = 1;
        private const int EXIT_BAD_REQUEST = 2;
        private const int EXIT_FATAL = 3;
        private const int EXIT_REFUSED = 4;

        private enum DevState { Enabled, Disabled, Unknown }

        [StructLayout(LayoutKind.Sequential)]
        private struct DEVPROPKEY
        {
            public Guid fmtid;
            public uint pid;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct SP_DEVINFO_DATA
        {
            public uint cbSize;
            public Guid ClassGuid;
            public uint DevInst;
            public IntPtr Reserved;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct SP_CLASSINSTALL_HEADER
        {
            public uint cbSize;
            public uint InstallFunction;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct SP_PROPCHANGE_PARAMS
        {
            public SP_CLASSINSTALL_HEADER ClassInstallHeader;
            public uint StateChange;
            public uint Scope;
            public uint HwProfile;
        }

        [DllImport("cfgmgr32.dll", CharSet = CharSet.Unicode)]
        private static extern uint CM_Locate_DevNodeW(out uint pdnDevInst, string pDeviceID, uint ulFlags);

        [DllImport("cfgmgr32.dll")]
        private static extern uint CM_Get_DevNode_Status(out uint pulStatus, out uint pulProblemNumber, uint dnDevInst, uint ulFlags);

        [DllImport("cfgmgr32.dll")]
        private static extern uint CM_Disable_DevNode(uint dnDevInst, uint ulFlags);

        [DllImport("cfgmgr32.dll")]
        private static extern uint CM_Enable_DevNode(uint dnDevInst, uint ulFlags);

        [DllImport("cfgmgr32.dll", CharSet = CharSet.Unicode)]
        private static extern uint CM_Get_DevNode_PropertyW(uint dnDevInst, ref DEVPROPKEY propertyKey,
            out uint propertyType, [Out] byte[] propertyBuffer, ref uint propertyBufferSize, uint ulFlags);

        [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr SetupDiCreateDeviceInfoList(IntPtr classGuid, IntPtr hwndParent);

        [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool SetupDiOpenDeviceInfoW(IntPtr deviceInfoSet, string deviceInstanceId,
            IntPtr hwndParent, uint openFlags, ref SP_DEVINFO_DATA deviceInfoData);

        [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool SetupDiSetClassInstallParamsW(IntPtr deviceInfoSet,
            ref SP_DEVINFO_DATA deviceInfoData, ref SP_PROPCHANGE_PARAMS classInstallParams, uint size);

        [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool SetupDiCallClassInstaller(uint installFunction, IntPtr deviceInfoSet,
            ref SP_DEVINFO_DATA deviceInfoData);

        [DllImport("setupapi.dll", SetLastError = true)]
        private static extern bool SetupDiDestroyDeviceInfoList(IntPtr deviceInfoSet);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern uint GetFinalPathNameByHandleW(SafeFileHandle hFile,
            [Out] StringBuilder lpszFilePath, uint cchFilePath, uint dwFlags);

        // DEVPKEY_Device_ClassGuid {a45c254e-df1c-4efd-8020-67d146a850e0} 10
        private static DEVPROPKEY DevpkeyClassGuid()
        {
            DEVPROPKEY k = new DEVPROPKEY();
            k.fmtid = new Guid("a45c254e-df1c-4efd-8020-67d146a850e0");
            k.pid = 10;
            return k;
        }

        private static string logPath = null;   // null until resolved; may stay null
        private static string outPath = null;   // set only when test switches are allowed

        private static int Main(string[] args)
        {
            bool validateOnly = false;
            string requestOverride = null;
            string outOverride = null;
            bool testSwitch = false;
            bool badArgs = false;
            try
            {
                ResolveLogPath();
                for (int i = 0; i < args.Length; i++)
                {
                    string a = args[i];
                    if (a == "--validate-only") { validateOnly = true; testSwitch = true; }
                    else if (a == "--request" && i + 1 < args.Length) { requestOverride = args[++i]; testSwitch = true; }
                    else if (a == "--out" && i + 1 < args.Length) { outOverride = args[++i]; testSwitch = true; }
                    else badArgs = true;
                }
                if (badArgs)
                {
                    Log("REFUSED unknown arguments (the task never passes any)");
                    return EXIT_REFUSED;
                }
                if (testSwitch)
                {
                    if (!TestSwitchesAllowed())
                    {
                        Log("REFUSED test-only switches outside an unelevated INPUTTOGGLE_TEST=1 run");
                        return EXIT_REFUSED;
                    }
                    outPath = outOverride;
                }

                string requestFile = requestOverride;
                if (String.IsNullOrEmpty(requestFile))
                {
                    string local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
                    requestFile = Path.Combine(Path.Combine(local, "InputToggle"), "request.txt");
                }

                string text;
                string why;
                if (!ReadRequestFile(requestFile, out text, out why))
                {
                    Log("ERROR " + why);
                    WriteOut("ERROR " + why);
                    return EXIT_BAD_REQUEST;
                }

                string action;
                List<string> ids;
                List<string> skipped;
                long issued;
                string bad = ParseRequest(text, out action, out ids, out skipped, out issued);
                foreach (string s in skipped) Log("SKIP " + s);
                if (bad != null)
                {
                    Log("ERROR invalid request: " + bad);
                    WriteOut("ERROR " + bad);
                    return EXIT_BAD_REQUEST;
                }
                long age;
                if (!IsFresh(issued, out age))
                {
                    Log("ERROR stale request (age " + age.ToString(CultureInfo.InvariantCulture) + " s)");
                    WriteOut("ERROR stale-request");
                    return EXIT_BAD_REQUEST;
                }

                Log("START action=" + action + " devices=" + ids.Count.ToString(CultureInfo.InvariantCulture)
                    + " age=" + age.ToString(CultureInfo.InvariantCulture) + "s"
                    + (validateOnly ? " (validate-only)" : ""));

                int okCount = 0, skipCount = 0, failCount = 0;
                StringBuilder summary = new StringBuilder();
                summary.Append("action=").Append(action);

                foreach (string id in ids)
                {
                    uint devinst;
                    uint cr = CM_Locate_DevNodeW(out devinst, id, CM_LOCATE_DEVNODE_NORMAL);
                    if (cr != CR_SUCCESS)
                    {
                        Log("SKIP locate failed cr=0x" + cr.ToString("X") + " id=" + id);
                        skipCount++;
                        continue;
                    }
                    Guid classGuid;
                    if (!TryGetClassGuid(devinst, out classGuid) ||
                        (classGuid != GUID_MOUSE && classGuid != GUID_KEYBOARD))
                    {
                        Log("SKIP not a Mouse/Keyboard class devnode id=" + id);
                        skipCount++;
                        continue;
                    }

                    if (validateOnly)
                    {
                        Log("VALIDATE ok class=" + (classGuid == GUID_MOUSE ? "Mouse" : "Keyboard") + " id=" + id);
                        okCount++;
                        continue;
                    }

                    bool done;
                    if (action == "disable") done = DoDisable(devinst, id);
                    else done = DoEnable(devinst, id);
                    if (done) okCount++; else failCount++;
                }

                summary.Append(" ok=").Append(okCount).Append(" skipped=").Append(skipCount)
                       .Append(" failed=").Append(failCount);
                if (validateOnly) summary.Insert(0, "VALIDATE ");
                Log("DONE " + summary.ToString());
                WriteOut(summary.ToString());
                return failCount == 0 ? EXIT_OK : EXIT_DEVICE_FAILED;
            }
            catch (Exception ex)
            {
                try { Log("FATAL " + ex.GetType().Name + ": " + ex.Message); } catch { }
                try { WriteOut("FATAL " + ex.GetType().Name); } catch { }
                return EXIT_FATAL;
            }
        }

        // ---- test-switch gate ---------------------------------------------
        private static bool TestSwitchesAllowed()
        {
            string v = Environment.GetEnvironmentVariable("INPUTTOGGLE_TEST");
            if (v != "1") return false;
            return !IsElevated();
        }

        private static bool IsElevated()
        {
            try
            {
                WindowsPrincipal p = new WindowsPrincipal(WindowsIdentity.GetCurrent());
                return p.IsInRole(WindowsBuiltInRole.Administrator);
            }
            catch
            {
                return true;   // unknown: treat as elevated (refuse test switches)
            }
        }

        // ---- reading the (untrusted) request file --------------------------
        private static bool ReadRequestFile(string path, out string text, out string why)
        {
            text = null;
            why = null;
            string full;
            try { full = Path.GetFullPath(path); }
            catch (Exception) { why = "request-path-invalid"; return false; }
            try
            {
                if (!File.Exists(full)) { why = "request-not-found"; return false; }
                string dir = Path.GetDirectoryName(full);
                if ((File.GetAttributes(full) & FileAttributes.ReparsePoint) != 0)
                {
                    why = "request-is-reparse-point"; return false;
                }
                if (!String.IsNullOrEmpty(dir) && (File.GetAttributes(dir) & FileAttributes.ReparsePoint) != 0)
                {
                    why = "request-dir-is-reparse-point"; return false;
                }
                using (FileStream fs = new FileStream(full, FileMode.Open, FileAccess.Read,
                                                      FileShare.Read | FileShare.Delete))
                {
                    // A junction or symlink anywhere on the way changes the
                    // final path of the handle we actually opened.
                    string final = FinalPath(fs.SafeFileHandle);
                    if (final == null || !String.Equals(final, full, StringComparison.OrdinalIgnoreCase))
                    {
                        why = "request-path-mismatch"; return false;
                    }
                    if (fs.Length > MAX_REQUEST_BYTES) { why = "request-too-large"; return false; }
                    byte[] buf = new byte[MAX_REQUEST_BYTES + 1];
                    int total = 0;
                    int n;
                    while (total < buf.Length && (n = fs.Read(buf, total, buf.Length - total)) > 0)
                        total += n;
                    if (total > MAX_REQUEST_BYTES) { why = "request-too-large"; return false; }
                    text = new UTF8Encoding(false, false).GetString(buf, 0, total);
                }
                return true;
            }
            catch (Exception ex)
            {
                why = "request-unreadable " + ex.GetType().Name;
                return false;
            }
        }

        private static string FinalPath(SafeFileHandle h)
        {
            StringBuilder sb = new StringBuilder(1024);
            uint n = GetFinalPathNameByHandleW(h, sb, (uint)sb.Capacity, 0);
            if (n == 0 || n >= sb.Capacity) return null;
            string s = sb.ToString();
            if (s.StartsWith(@"\\?\UNC\", StringComparison.OrdinalIgnoreCase)) return @"\\" + s.Substring(8);
            if (s.StartsWith(@"\\?\", StringComparison.Ordinal)) return s.Substring(4);
            return s;
        }

        // ---- request parsing (untrusted input) ----------------------------
        // Returns null when the request is usable, else a short reason. Skipped
        // lines are described by number and length only: their text is never
        // logged, so the log cannot be used to read another file.
        private static string ParseRequest(string text, out string action, out List<string> ids,
                                           out List<string> skipped, out long issued)
        {
            action = null;
            ids = new List<string>();
            skipped = new List<string>();
            issued = -1;
            string[] lines = text.Split('\n');
            int used = 0;
            for (int ln = 0; ln < lines.Length; ln++)
            {
                string line = lines[ln].Trim();
                if (line.Length == 0 || line.StartsWith("#")) continue;
                used++;
                if (used > MAX_REQUEST_LINES) return "too-many-lines";
                string pos = "line " + (ln + 1).ToString(CultureInfo.InvariantCulture)
                    + " (" + line.Length.ToString(CultureInfo.InvariantCulture) + " chars)";
                int eq = line.IndexOf('=');
                if (eq < 0) { skipped.Add(pos + ": malformed"); continue; }
                string k = line.Substring(0, eq).Trim().ToLowerInvariant();
                string v = line.Substring(eq + 1).Trim();
                if (k == "action")
                {
                    action = v.ToLowerInvariant();
                }
                else if (k == "issued")
                {
                    long t;
                    if (v.Length > 0 && v.Length <= 12 && IsDigits(v) &&
                        long.TryParse(v, NumberStyles.None, CultureInfo.InvariantCulture, out t))
                        issued = t;
                    else
                        skipped.Add(pos + ": invalid issued value");
                }
                else if (k == "device")
                {
                    if (ids.Count >= MAX_DEVICE_LINES) skipped.Add(pos + ": more than 32 devices");
                    else if (!ValidInstanceId(v)) skipped.Add(pos + ": invalid instance id");
                    else ids.Add(v);
                }
                else skipped.Add(pos + ": unknown key");
            }
            if (action != "disable" && action != "enable") return "bad-action";
            if (issued < 0) return "missing-issued";
            return null;
        }

        private static bool IsDigits(string s)
        {
            for (int i = 0; i < s.Length; i++)
                if (s[i] < '0' || s[i] > '9') return false;
            return true;
        }

        private static bool IsFresh(long issued, out long age)
        {
            DateTime epoch = new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc);
            long now = (long)(DateTime.UtcNow - epoch).TotalSeconds;
            age = now - issued;
            return issued >= now - MAX_AGE_SECONDS && issued <= now + MAX_FUTURE_SECONDS;
        }

        private static bool ValidInstanceId(string s)
        {
            if (s == null) return false;
            if (s.Length < 1 || s.Length > MAX_ID_LEN) return false;
            if (s.IndexOf('\\') < 0) return false;
            for (int i = 0; i < s.Length; i++)
            {
                char c = s[i];
                bool ok = (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
                          (c >= '0' && c <= '9') ||
                          c == '\\' || c == '&' || c == '_' || c == '.' ||
                          c == '#' || c == '-' || c == '{' || c == '}' ||
                          c == '+' || c == ',';
                if (!ok) return false;
            }
            return true;
        }

        // ---- disable / enable ---------------------------------------------
        private static bool DoDisable(uint devinst, string id)
        {
            // NON-persistent on purpose: no CM_DISABLE_PERSIST (0x8). A restart
            // re-enables the device because CONFIGFLAG_DISABLED is never set.
            uint cr = CM_Disable_DevNode(devinst, CM_DISABLE_UI_NOT_OK);
            if (cr != CR_SUCCESS)
            {
                Log("FAIL disable cr=0x" + cr.ToString("X") + " id=" + id);
                return false;
            }
            uint prob;
            DevState s = ReadState(id, out prob);
            Log("OK disable id=" + id + " state=" + s.ToString() + " problem="
                + prob.ToString(CultureInfo.InvariantCulture));
            return true;
        }

        private static bool DoEnable(uint devinst, string id)
        {
            uint prob = 0;
            uint cr = CM_Enable_DevNode(devinst, 0);
            DevState s;
            if (cr == CR_SUCCESS)
            {
                // give the stack time to restart before judging the result
                s = WaitWhileDisabled(id, STATUS_WAIT_MS, out prob);
                if (s == DevState.Enabled)
                {
                    Log("OK enable id=" + id);
                    return true;
                }
                if (s == DevState.Unknown)
                {
                    Log("FAIL enable id=" + id + " (status unreadable after CM_Enable_DevNode)");
                    return false;
                }
            }
            else
            {
                Log("WARN CM_Enable_DevNode cr=0x" + cr.ToString("X") + " id=" + id);
            }

            // Still disabled (or the call failed): likely a persistent disable
            // set elsewhere. Clear it with the documented SetupAPI property change.
            Log("INFO still disabled (problem=" + prob.ToString(CultureInfo.InvariantCulture)
                + "); trying SetupDiChangeState id=" + id);
            if (!SetupDiEnable(id))
            {
                Log("FAIL enable id=" + id + " (SetupAPI fallback failed)");
                return false;
            }
            s = WaitWhileDisabled(id, STATUS_WAIT_MS, out prob);
            if (s == DevState.Enabled)
            {
                Log("OK enable-via-setupapi id=" + id);
                return true;
            }
            Log("FAIL enable id=" + id + " state=" + s.ToString() + " problem="
                + prob.ToString(CultureInfo.InvariantCulture));
            return false;
        }

        // Re-locates the devnode on every read (it restarts while being enabled).
        private static DevState ReadState(string id, out uint prob)
        {
            prob = 0;
            uint dn;
            if (CM_Locate_DevNodeW(out dn, id, CM_LOCATE_DEVNODE_NORMAL) != CR_SUCCESS)
                return DevState.Unknown;
            uint st, pr;
            if (CM_Get_DevNode_Status(out st, out pr, dn, 0) != CR_SUCCESS)
                return DevState.Unknown;
            prob = pr;
            if ((st & DN_HAS_PROBLEM) != 0 && pr == CM_PROB_DISABLED) return DevState.Disabled;
            return DevState.Enabled;
        }

        private static DevState WaitWhileDisabled(string id, int timeoutMs, out uint prob)
        {
            int waited = 0;
            while (true)
            {
                DevState s = ReadState(id, out prob);
                if (s == DevState.Enabled) return s;
                if (waited >= timeoutMs) return s;
                Thread.Sleep(100);
                waited += 100;
            }
        }

        private static bool SetupDiEnable(string id)
        {
            IntPtr set = SetupDiCreateDeviceInfoList(IntPtr.Zero, IntPtr.Zero);
            if (set == IntPtr.Zero || set == new IntPtr(-1)) return false;
            try
            {
                SP_DEVINFO_DATA did = new SP_DEVINFO_DATA();
                did.cbSize = (uint)Marshal.SizeOf(typeof(SP_DEVINFO_DATA));
                if (!SetupDiOpenDeviceInfoW(set, id, IntPtr.Zero, 0, ref did))
                {
                    Log("WARN SetupDiOpenDeviceInfo gle=" + Marshal.GetLastWin32Error());
                    return false;
                }
                // GLOBAL first, then CONFIGSPECIFIC (as devcon does); stop at the
                // first one that works so the stack is restarted only once.
                uint[] scopes = new uint[] { DICS_FLAG_GLOBAL, DICS_FLAG_CONFIGSPECIFIC };
                for (int s = 0; s < scopes.Length; s++)
                {
                    SP_PROPCHANGE_PARAMS pcp = new SP_PROPCHANGE_PARAMS();
                    pcp.ClassInstallHeader.cbSize = (uint)Marshal.SizeOf(typeof(SP_CLASSINSTALL_HEADER));
                    pcp.ClassInstallHeader.InstallFunction = DIF_PROPERTYCHANGE;
                    pcp.StateChange = DICS_ENABLE;
                    pcp.Scope = scopes[s];
                    pcp.HwProfile = 0;
                    if (!SetupDiSetClassInstallParamsW(set, ref did, ref pcp,
                            (uint)Marshal.SizeOf(typeof(SP_PROPCHANGE_PARAMS))))
                    {
                        Log("WARN SetupDiSetClassInstallParams scope=" + scopes[s]
                            + " gle=" + Marshal.GetLastWin32Error());
                        continue;
                    }
                    if (SetupDiCallClassInstaller(DIF_PROPERTYCHANGE, set, ref did))
                        return true;
                    Log("WARN SetupDiCallClassInstaller scope=" + scopes[s]
                        + " gle=" + Marshal.GetLastWin32Error());
                }
                return false;
            }
            finally
            {
                SetupDiDestroyDeviceInfoList(set);
            }
        }

        private static bool TryGetClassGuid(uint devinst, out Guid classGuid)
        {
            classGuid = Guid.Empty;
            DEVPROPKEY key = DevpkeyClassGuid();
            uint type = 0;
            uint size = 0;
            uint cr = CM_Get_DevNode_PropertyW(devinst, ref key, out type, null, ref size, 0);
            if (cr != CR_BUFFER_SMALL || size != 16) return false;
            byte[] buf = new byte[size];
            cr = CM_Get_DevNode_PropertyW(devinst, ref key, out type, buf, ref size, 0);
            if (cr != CR_SUCCESS || type != DEVPROP_TYPE_GUID || size != 16) return false;
            classGuid = new Guid(buf);
            return true;
        }

        // ---- logging (Program Files only; never a user-writable path) ------
        private static void ResolveLogPath()
        {
            try
            {
                string pf = Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles);
                string dir = Path.Combine(Path.Combine(pf, "InputToggle"), "logs");
                if (Directory.Exists(dir)) logPath = Path.Combine(dir, "helper.log");
            }
            catch { logPath = null; }
        }

        private static void Log(string msg)
        {
            if (logPath == null) return;
            try
            {
                FileInfo fi = new FileInfo(logPath);
                if (fi.Exists && fi.Length > (1 << 20))
                {
                    string bak = logPath + ".1";
                    if (File.Exists(bak)) File.Delete(bak);
                    File.Move(logPath, bak);
                }
                string line = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss.fff", CultureInfo.InvariantCulture)
                    + " " + msg + Environment.NewLine;
                File.AppendAllText(logPath, line, new UTF8Encoding(false));
            }
            catch { /* never fall back to a user-writable location */ }
        }

        private static void WriteOut(string summary)
        {
            if (String.IsNullOrEmpty(outPath)) return;    // test runs only
            try { File.WriteAllText(outPath, summary + Environment.NewLine, new UTF8Encoding(false)); }
            catch { }
        }
    }
}
