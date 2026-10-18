# -*- coding: utf-8 -*-
"""Draw InputToggle's icons at build time (needs Pillow, from the build venv).

Writes into --out:
    icon_on.ico    tray icon while the selected devices are on (green mouse)
    icon_off.ico   tray icon while they are off (red mouse, struck through)
    app.ico        program icon (exe, installer, Settings window)

Every size is drawn on its own at 8x and scaled down, so the small sizes
stay crisp. Frames are stored as BMP, which every Windows icon loader and
resource tool accepts. The release bundles these files, so the app itself
does not need Pillow.
"""
import argparse
import os
import sys

from PIL import Image, ImageDraw

TRAY_SIZES = (16, 20, 24, 32, 40, 48, 64)
APP_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
SUPERSAMPLE = 8

GREEN = (46, 160, 90, 255)
RED = (205, 60, 55, 255)
DARK_RED = (150, 30, 30, 255)
BLUE = (37, 99, 160, 255)
WHITE = (255, 255, 255, 255)


def _box(s, x0, y0, x1, y1):
    """A box given in 0..1 units on an s x s canvas."""
    return [round(x0 * s), round(y0 * s), round(x1 * s) - 1, round(y1 * s) - 1]


def _finish(img, size):
    return img.resize((size, size), Image.Resampling.LANCZOS)


def draw_tray(size, off):
    s = size * SUPERSAMPLE
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    body = RED if off else GREEN
    d.rounded_rectangle(_box(s, 0.27, 0.05, 0.73, 0.95), radius=round(0.23 * s), fill=body)
    d.rounded_rectangle(_box(s, 0.455, 0.15, 0.545, 0.38), radius=round(0.045 * s), fill=WHITE)
    if off:
        a, b = (0.12 * s, 0.88 * s), (0.88 * s, 0.12 * s)
        d.line([a, b], fill=WHITE, width=round(0.20 * s))
        d.line([a, b], fill=DARK_RED, width=round(0.09 * s))
    return _finish(img, size)


def draw_app(size):
    s = size * SUPERSAMPLE
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle(_box(s, 0.03, 0.03, 0.97, 0.97), radius=round(0.22 * s), fill=BLUE)
    d.rounded_rectangle(_box(s, 0.32, 0.13, 0.68, 0.87), radius=round(0.18 * s), fill=WHITE)
    d.rounded_rectangle(_box(s, 0.465, 0.21, 0.535, 0.40), radius=round(0.035 * s), fill=BLUE)
    # a small switch on the mouse: the "toggle" in the name
    d.rounded_rectangle(_box(s, 0.40, 0.56, 0.60, 0.70), radius=round(0.07 * s), fill=GREEN)
    d.ellipse(_box(s, 0.47, 0.565, 0.595, 0.695), fill=WHITE)
    return _finish(img, size)


def save_ico(path, frames):
    frames = sorted(frames, key=lambda im: im.size[0])
    base = frames[-1]
    base.save(path, format="ICO", sizes=[im.size for im in frames],
              append_images=frames[:-1], bitmap_format="bmp")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True, help="output folder")
    args = ap.parse_args(argv)
    os.makedirs(args.out, exist_ok=True)
    outputs = {
        "icon_on.ico": [draw_tray(n, off=False) for n in TRAY_SIZES],
        "icon_off.ico": [draw_tray(n, off=True) for n in TRAY_SIZES],
        "app.ico": [draw_app(n) for n in APP_SIZES],
    }
    for name, frames in outputs.items():
        path = os.path.join(args.out, name)
        save_ico(path, frames)
        with Image.open(path) as check:
            got = sorted(check.info.get("sizes", set()))
        want = sorted((f.size for f in frames))
        if got != want:
            print("ERROR: %s has sizes %r, expected %r" % (name, got, want), file=sys.stderr)
            return 1
        print("wrote %s (%d bytes, sizes %s)" % (path, os.path.getsize(path),
                                                ", ".join(str(w) for w, _h in got)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
