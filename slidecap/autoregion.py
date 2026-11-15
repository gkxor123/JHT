"""Find the slide area of a lecture video without asking the user.

Works on a few dozen gray frames sampled over the whole video (numpy only,
so it can be tuned on frames saved with ``SlideCap.exe --dump-frames``).

Idea: the slide area is where the picture changes *now and then* (slide
flips).  Screen borders, toolbars and black bars never change; a webcam or a
playing video changes almost all the time.  The slide area is the largest
rectangle whose rows/columns are covered by "sometimes changing" pixels;
its edges are then snapped to strong straight lines (the border of the
shared screen) when there is one close by.

``detect_region`` returns (roi, confidence, reason): roi = (x, y, w, h) in
the coordinates of the given frames, confidence "high" or "low".  With "low"
the caller should ask the user (the roi can be offered as a starting point).
"""
from typing import List, Optional, Sequence, Tuple

import numpy as np

Rect = Tuple[int, int, int, int]

DIFF = 25            # gray-level difference counted as change
SOMETIMES = (0.02, 0.6)   # share of sample pairs with change: slide content
ALWAYS = 0.6         # changing in more pairs than this: webcam / video


def _runs(flags: np.ndarray):
    """Contiguous True runs as (start, end) with end exclusive."""
    out, start = [], None
    for i, f in enumerate(list(flags) + [False]):
        if f and start is None:
            start = i
        elif not f and start is not None:
            out.append((start, i))
            start = None
    return out


def _span(profile: np.ndarray, thresh: float, gap: int) -> Optional[Tuple[int, int]]:
    """Longest run of profile > thresh, bridging holes up to ``gap``."""
    flags = profile > thresh
    runs = _runs(flags)
    if not runs:
        return None
    merged = [list(runs[0])]
    for a, b in runs[1:]:
        if a - merged[-1][1] <= gap:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    a, b = max(merged, key=lambda r: (r[1] - r[0]) * profile[r[0]:r[1]].sum())
    return a, b


def _snap(median: np.ndarray, a: int, b: int, axis: int, reach: int) -> Tuple[int, int]:
    """Move the span ends to the strongest straight edge within ``reach``.

    axis=1: a/b are columns (vertical edges); axis=0: rows."""
    g = median.astype(np.float32)
    if axis == 1:
        d = np.abs(np.diff(g, axis=1)).mean(axis=0)     # per column boundary
    else:
        d = np.abs(np.diff(g, axis=0)).mean(axis=1)     # per row boundary
    n = len(d) + 1

    def best(p, lo, hi):
        lo, hi = max(0, lo), min(len(d), hi)
        if hi <= lo:
            return p
        seg = d[lo:hi]
        k = int(np.argmax(seg))
        # only a clear line (much stronger than its surroundings) counts
        if seg[k] > max(12.0, 3.0 * float(np.median(d))):
            return lo + k + 1
        return p
    a2 = best(a, a - reach, a + reach)
    b2 = best(b, b - reach, b + reach)
    if b2 - a2 < (b - a) * 0.7:
        return a, b
    return max(0, a2), min(n, b2)


def detect_region(frames: Sequence[np.ndarray]) -> Tuple[Optional[Rect], str, str]:
    """frames: gray uint8 arrays of the same size (~320-640 px wide)."""
    if len(frames) < 6:
        return None, "low", "too few frames"
    st = np.stack([f.astype(np.int16) for f in frames])
    H, W = st.shape[1:]
    ch = (np.abs(np.diff(st, axis=0)) > DIFF).mean(axis=0)   # share of pairs with change
    content = (ch > SOMETIMES[0]) & (ch < SOMETIMES[1])
    always = ch >= ALWAYS
    if content.mean() < 0.02:
        return None, "low", "almost nothing changes"
    gap = max(3, W // 40)
    xs = _span(content.mean(axis=0), 0.03, gap)
    ys = _span(content.mean(axis=1), 0.03, gap)
    if xs is None or ys is None:
        return None, "low", "no changing area"
    x0, x1 = xs
    y0, y1 = ys
    med = np.median(st, axis=0).astype(np.uint8)
    reach = max(4, W // 30)
    x0, x1 = _snap(med[y0:y1], x0, x1, 1, reach)
    y0, y1 = _snap(med[:, x0:x1], y0, y1, 0, reach)
    w, h = x1 - x0, y1 - y0
    roi = (int(x0), int(y0), int(w), int(h))
    area = w * h / float(W * H)
    aspect = w / float(max(1, h))
    inside_always = float(always[y0:y1, x0:x1].mean())
    reasons = []
    if area < 0.2:
        reasons.append("small area %.0f%%" % (area * 100))
    if not 1.15 <= aspect <= 2.4:
        reasons.append("unusual shape %.2f" % aspect)
    if inside_always > 0.25:
        reasons.append("much motion inside %.0f%%" % (inside_always * 100))
    return roi, ("low" if reasons else "high"), ", ".join(reasons) or "ok"


def scale_rect(r: Rect, src_wh, dst_wh) -> Rect:
    sx, sy = dst_wh[0] / float(src_wh[0]), dst_wh[1] / float(src_wh[1])
    x, y, w, h = r
    X, Y = int(round(x * sx)), int(round(y * sy))
    return X, Y, min(dst_wh[0] - X, int(round(w * sx))), min(dst_wh[1] - Y, int(round(h * sy)))


def sample_frames(path: str, n: int = 40):
    """n frames spread over the video (BGR, full size), skipping the very
    start and end (titles, black frames).  Needs the video libraries."""
    from .engine import frame_at, video_info
    info = video_info(path)
    dur = info["duration"] or 0.0
    out = []
    for i in range(n):
        t = dur * (0.03 + 0.94 * (i + 0.5) / n) if dur > 0 else 0.0
        try:
            out.append((t, frame_at(path, t)))
        except Exception:
            continue
    return info, out


def to_gray_small(bgr: np.ndarray, width: int = 480) -> np.ndarray:
    import cv2
    g = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    h = max(1, int(round(g.shape[0] * width / float(g.shape[1]))))
    return cv2.resize(g, (width, h), interpolation=cv2.INTER_AREA)


def detect_region_in_video(path: str, n: int = 40):
    """(roi in video pixels or None, confidence, reason)."""
    info, frames = sample_frames(path, n)
    if not frames:
        return None, "low", "cannot read frames"
    small = [to_gray_small(f) for _, f in frames]
    roi, conf, why = detect_region(small)
    if roi is None:
        return None, conf, why
    sh = small[0].shape
    return scale_rect(roi, (sh[1], sh[0]), (info["width"], info["height"])), conf, why
