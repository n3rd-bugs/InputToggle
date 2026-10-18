# -*- coding: utf-8 -*-
"""Write the Windows version resource for InputToggle.exe (PyInstaller
--version-file format). The FileDescription is the name Windows shows for the
app's notifications and in Settings > Personalization > Taskbar."""
import argparse
import re
import sys

TEMPLATE = """# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={tup},
    prodvers={tup},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [StringStruct('CompanyName', {company!r}),
         StringStruct('FileDescription', 'InputToggle'),
         StringStruct('FileVersion', {ver!r}),
         StringStruct('InternalName', 'InputToggle'),
         StringStruct('LegalCopyright', {copyright!r}),
         StringStruct('OriginalFilename', 'InputToggle.exe'),
         StringStruct('ProductName', 'InputToggle'),
         StringStruct('ProductVersion', {ver!r})])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--version", required=True, help="x.y.z")
    ap.add_argument("--out", required=True)
    ap.add_argument("--company", default="Usama Masood")
    ap.add_argument("--copyright", default="Copyright (c) 2026 Usama Masood")
    args = ap.parse_args(argv)
    if not re.fullmatch(r"\d+\.\d+\.\d+", args.version):
        print("ERROR: version must look like 1.2.3, got %r" % args.version, file=sys.stderr)
        return 2
    parts = [int(p) for p in args.version.split(".")] + [0]
    text = TEMPLATE.format(tup=tuple(parts), ver=args.version, company=args.company,
                           copyright=args.copyright)
    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    print("wrote %s (%s)" % (args.out, args.version))
    return 0


if __name__ == "__main__":
    sys.exit(main())
