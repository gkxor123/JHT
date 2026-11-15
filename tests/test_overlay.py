"""Meeting-app notification boxes (Zoom chat "... to everyone") must not
create extra slides, while real changes near the bottom still count.

Run:  python tests/test_overlay.py
"""
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from slidecap.engine import Capture, Settings, _Analyzer, _relation  # noqa: E402

W, H = 1280, 720


def slide(dark=False):
    img = np.full((H, W, 3), 30 if dark else 245, np.uint8)
    col = (230, 230, 230) if dark else (20, 20, 20)
    cv2.rectangle(img, (0, 0), (W, 110), (90, 100, 20), -1)
    cv2.putText(img, "Quiz 1", (480, 80), cv2.FONT_HERSHEY_SIMPLEX, 2.2, (255, 255, 255), 4)
    cv2.putText(img, "What is your diagnosis?", (200, 400), cv2.FONT_HERSHEY_SIMPLEX, 2.0, col, 4)
    return img


def toast(img, text):
    img = img.copy()
    x0, y0, x1, y1 = 450, 650, 710, 720
    cv2.rectangle(img, (x0, y0), (x1, y1), (35, 35, 35), -1)
    cv2.circle(img, (x0 + 28, y0 + 32), 16, (180, 80, 160), -1)
    cv2.putText(img, "Kim to everyone", (x0 + 55, y0 + 25), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (240, 240, 240), 1)
    cv2.putText(img, text, (x0 + 55, y0 + 52), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (240, 240, 240), 1)
    return img


def cap(a, img, t):
    c = Capture(t, a.prep(img), [img])
    c.finalize()
    return c


def main():
    a = _Analyzer((0, 0, W, H), [], Settings())
    base = slide()
    figure = base.copy()
    cv2.rectangle(figure, (850, 180), (1200, 560), (60, 60, 60), -1)
    cv2.circle(figure, (1020, 370), 90, (200, 200, 200), -1)
    foot = base.copy()
    cv2.putText(foot, "Ref: Radiology 2024;310:e1234", (60, 690), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (20, 20, 20), 2)
    dark = slide(dark=True)
    dark2 = dark.copy()
    cv2.putText(dark2, "answer", (470, 700), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (230, 230, 230), 3)

    cases = [
        ("toast appears", base, toast(base, "sparganosis"), ("pointer", False)),
        ("toast replaced", toast(base, "sparganosis"), toast(base, "phyllodes tumor"), ("pointer", None)),
        ("toast vanishes", toast(base, "pash"), base, ("pointer", True)),
        ("new figure + toast", base, toast(figure, "x"), ("!pointer", None)),
        ("footnote added", base, foot, ("addition", None)),
        ("dark slide, text at bottom", dark, dark2, ("!pointer", None)),
    ]
    bad = 0
    for name, p, c, (want, keep) in cases:
        kind, keep_c = _relation(cap(a, p, 0), cap(a, c, 1), a, None)
        ok = (kind != want[1:]) if want.startswith("!") else (kind == want)
        if ok and keep is not None:
            ok = keep_c == keep
        print("%-28s -> %-9s keep_c=%s  %s" % (name, kind, keep_c, "ok" if ok else "FAIL"))
        bad += not ok
    print("overlay tests %s" % ("passed" if not bad else "FAILED (%d)" % bad))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
