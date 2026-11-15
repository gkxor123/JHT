"""A cursor that wanders over a slide (and was never seen moving during
detection) and a webcam sliver along the region edge must not create extra
slides; an arrow really added to the slide must stay in the result.

Run:  python tests/test_cursor_merge.py
"""
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from slidecap.engine import Capture, Settings, _Analyzer, merge_captures  # noqa: E402

W, H = 1280, 720


def slide():
    img = np.full((H, W, 3), 245, np.uint8)
    cv2.putText(img, "Development of the spine", (160, 120), cv2.FONT_HERSHEY_SIMPLEX, 2.0, (30, 30, 30), 4)
    for k in range(6):
        cv2.putText(img, "- bullet line number %d about the notochord" % k, (180, 220 + 70 * k),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.1, (40, 40, 40), 2)
    return img


def cursor(img, x, y):
    img = img.copy()
    pts = np.array([[x, y], [x, y + 30], [x + 8, y + 22], [x + 14, y + 34], [x + 19, y + 31],
                    [x + 13, y + 19], [x + 23, y + 19]], np.int32)
    cv2.fillPoly(img, [pts], (200, 40, 200))          # pink arrow cursor
    cv2.polylines(img, [pts], True, (255, 255, 255), 1)
    return img


def webcam_edge(img, k):
    img = img.copy()
    img[150:330, 0:40] = (70 + 8 * (k % 5), 90, 120)   # sliver of a speaker video
    cv2.circle(img, (20 + (k % 3) * 4, 200 + (k % 4) * 6), 14, (180, 150, 130), -1)
    return img


def arrow(img):
    img = img.copy()
    cv2.arrowedLine(img, (900, 560), (980, 470), (0, 0, 220), 6, tipLength=0.4)
    return img


def cap(a, img, t):
    c = Capture(t, a.prep(img), [img])
    c.finalize()
    return c


def main():
    a = _Analyzer((0, 0, W, H), [], Settings())
    base = slide()
    spots = [(300, 300), (700, 420), (1000, 200), (500, 600), (850, 330), (400, 180)]
    imgs = [webcam_edge(base, 0)]
    for k, (x, y) in enumerate(spots):
        imgs.append(webcam_edge(cursor(base, x, y), k + 1))
    with_arrow = arrow(base)
    imgs.append(webcam_edge(with_arrow, 9))
    imgs.append(webcam_edge(cursor(with_arrow, 600, 250), 10))
    caps = [cap(a, im, float(t)) for t, im in enumerate(imgs)]
    out = merge_captures(caps, a, Settings(), None)
    times = [c.time for c in out]
    print("captures", len(caps), "-> slides", len(out), "at", times)
    # an arrow drawn onto blank slide area is a build step: one final slide
    # that contains the arrow and no cursor
    img = cv2.imdecode(np.frombuffer(out[-1].data, np.uint8), cv2.IMREAD_COLOR)
    has_arrow = bool((img[480:560, 900:990, 2] > 150).any() & (img[480:560, 900:990, 0] < 80).any())
    pink = (img[:, :, 2] > 150) & (img[:, :, 0] > 150) & (img[:, :, 1] < 90)
    print("final slide has arrow:", has_arrow, "| cursor pixels left:", int(pink.sum()))
    ok = len(out) == 1 and has_arrow and pink.sum() < 30
    print("cursor merge test %s" % ("passed" if ok else "FAILED"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
