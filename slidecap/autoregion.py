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


def _quantile_span(profile: np.ndarray, q: float) -> Tuple[int, int]:
    """Span holding all but q of the profile's mass on each side."""
    c = np.cumsum(profile) / max(1e-9, float(profile.sum()))
    a = int(np.searchsorted(c, q))
    b = int(np.searchsorted(c, 1.0 - q)) + 1
    return a, max(a + 1, b)


def _grow(med: np.ndarray, x0: int, y0: int, x1: int, y1: int):
    """Grow the box over plain slide background (same tone as the slide) until a
    clearly different area (screen border, dark surroundings) or the frame edge.

    Returns the box and how many of its 4 sides ended on a clear border or the
    frame edge (rather than on something vague)."""
    H, W = med.shape
    m = med.astype(np.int16)
    vals = m[y0:y1, x0:x1].ravel()
    hist = np.bincount(np.clip(vals, 0, 255), minlength=256)
    bg = int(np.argmax(np.convolve(hist, np.ones(9), "same")))   # slide background tone

    def plain(seg):          # mostly slide background (text/figures allowed)
        return float((np.abs(seg - bg) <= 20).mean()) >= 0.6

    def border(seg):         # a solid area clearly unlike the slide
        return float((np.abs(seg - bg) > 40).mean()) >= 0.7

    sides = {}
    for _ in range(2):
        while x0 > 0 and plain(m[y0:y1, x0 - 1]):
            x0 -= 1
        while x1 < W and plain(m[y0:y1, x1]):
            x1 += 1
        while y0 > 0 and plain(m[y0 - 1, x0:x1]):
            y0 -= 1
        while y1 < H and plain(m[y1, x0:x1]):
            y1 += 1
    # look a few pixels beyond the box: a thin frame line may sit right at it
    k, s = 3, max(2, W // 160)
    sides["l"] = x0 <= 1 or any(border(m[y0:y1, max(0, x0 - o - k):x0 - o]) for o in (0, s) if x0 - o > 0)
    sides["r"] = x1 >= W - 1 or any(border(m[y0:y1, x1 + o:x1 + o + k]) for o in (0, s) if x1 + o < W)
    sides["t"] = y0 <= 1 or any(border(m[max(0, y0 - o - k):y0 - o, x0:x1]) for o in (0, s) if y0 - o > 0)
    sides["b"] = y1 >= H - 1 or any(border(m[y1 + o:y1 + o + k, x0:x1]) for o in (0, s) if y1 + o < H)
    return x0, y0, x1, y1, sum(sides.values())


def detect_region(frames: Sequence[np.ndarray]) -> Tuple[Optional[Rect], str, str]:
    """frames: gray uint8 arrays of the same size (~320-640 px wide)."""
    if len(frames) < 6:
        return None, "low", "too few frames"
    st = np.stack([f.astype(np.int16) for f in frames])
    H, W = st.shape[1:]
    ch = (np.abs(np.diff(st, axis=0)) > DIFF).mean(axis=0)   # share of pairs with change
    if ch.mean() < 0.005:
        return None, "low", "almost nothing changes"
    # core of the changes (a moving webcam or toolbar is trimmed off) ...
    x0, x1 = _quantile_span(ch.sum(axis=0), 0.03)
    y0, y1 = _quantile_span(ch.sum(axis=1), 0.03)
    # ... grown over the blank parts of the slide up to its border
    med = np.median(st, axis=0).astype(np.uint8)
    x0, y0, x1, y1, sides = _grow(med, x0, y0, x1, y1)
    w, h = x1 - x0, y1 - y0
    roi = (int(x0), int(y0), int(w), int(h))
    area = w * h / float(W * H)
    aspect = w / float(max(1, h))
    inside = float(ch[y0:y1, x0:x1].sum() / ch.sum())
    reasons = []
    if area < 0.15:
        reasons.append("small area %.0f%%" % (area * 100))
    if not 1.15 <= aspect <= 2.4:
        reasons.append("unusual shape %.2f" % aspect)
    if inside < 0.9:
        reasons.append("changes outside %.0f%%" % ((1 - inside) * 100))
    if sides < 4:
        reasons.append("no clear border (%d/4 sides)" % sides)
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
