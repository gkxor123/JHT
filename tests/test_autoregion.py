"""Slide-area auto detection on synthetic screen recordings (numpy only).

Run:  python tests/test_autoregion.py
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from slidecap.autoregion import detect_region  # noqa: E402

W, H = 480, 270
rng = np.random.default_rng(0)


def iou(a, b):
    ix = max(0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    i = ix * iy
    return i / float(a[2] * a[3] + b[2] * b[3] - i)


def frames(slide, webcam=None, n=40, bar=False):
    """Screen recording: dark background, blue side panel, white slide whose
    text changes every sample, optional webcam with a slightly moving person."""
    sx, sy, sw, sh = slide
    out = []
    for k in range(n):
        f = np.full((H, W), 20, np.uint8)
        if bar:
            f[10:40, 20:460] = 90                      # static banner
        f[sy:sy + sh, sx:sx + sw] = 245               # slide background
        for _ in range(12):                           # text lines (new each slide)
            x = int(rng.integers(sx + 15, sx + sw // 2))
            y = int(rng.integers(sy + 15, sy + sh - 15))
            f[y:y + 4, x:x + int(rng.integers(20, sw // 2))] = 30
        if webcam:
            wx, wy, ww, wh = webcam
            f[wy:wy + wh, wx:wx + ww] = 110
            dx = int(rng.integers(-2, 3))
            f[wy + 15:wy + wh - 5, wx + 20 + dx:wx + ww - 20 + dx] = 60   # person
        out.append(f)
    return out


def main():
    cases = [
        ("full screen", (0, 0, W, H), None, False, "high"),
        ("slide + webcam + banner", (120, 60, 330, 186), (20, 60, 80, 70), True, "high"),
        ("letterboxed", (60, 0, 360, H), None, False, "high"),
    ]
    bad = 0
    for name, slide, cam, bar, want in cases:
        roi, conf, why = detect_region(frames(slide, cam, bar=bar))
        ok = roi is not None and iou(roi, slide) > 0.9 and conf == want
        print("%-26s -> %s %s (%s) IoU %.3f  %s" % (name, roi, conf, why,
              iou(roi, slide) if roi else 0, "ok" if ok else "FAIL"))
        bad += not ok
    # nothing changes -> no region
    roi, conf, why = detect_region([np.full((H, W), 128, np.uint8)] * 20)
    ok = roi is None and conf == "low"
    print("%-26s -> %s %s (%s)  %s" % ("static video", roi, conf, why, "ok" if ok else "FAIL"))
    bad += not ok
    print("autoregion tests %s" % ("passed" if not bad else "FAILED (%d)" % bad))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
