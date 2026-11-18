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
    """frames: gray uint8 arrays of the same size (~320-640 px wide).

    The region found is finally widened so that it holds the slide changes
    of (nearly) all sampled slide flips: a slide that uses the edges only
    now and then must not be cut (a slightly wider region costs nothing)."""
    roi, conf, why = _detect_region(frames)
    if roi is not None:
        st = np.stack([f.astype(np.int16) for f in frames])
        D = np.abs(np.diff(st, axis=0)) > DIFF
        roi = _cover_flips(D, roi)
    return roi, conf, why


def _cover_flips(D: np.ndarray, roi: Rect, share: float = 0.9) -> Rect:
    """Widen ``roi`` so that it contains the changed area of ``share`` of the
    slide flips seen in the samples (never shrinks it).

    D: (steps, H, W) change masks between consecutive samples.  Pixels that
    change most of the time (webcam, audience, a playing video) are left
    out, so only slide content counts."""
    steps, H, W = D.shape
    x, y, w, h = roi
    # slide flips: steps where the region itself changes clearly
    flips = D[:, y:y + h, x:x + w].mean(axis=(1, 2)) >= 0.01
    # where things move all the time (webcam, audience, a playing video).
    # Only areas outside the region matter here, and the slide's own outer
    # parts are by definition used only now and then, so "often" is enough.
    # A swaying person: spread the area a little.
    busy = D.mean(axis=0) > 0.25
    if (~flips).sum() >= 5:
        busy |= D[~flips].mean(axis=0) > 0.25
    busy = _box_blur(busy.astype(np.float64), max(5, W // 40) | 1) > 0.02
    # look around the region, not across the whole picture
    mx, my = int(max(w * 0.5, W * 0.1)), int(max(h * 0.5, H * 0.1))
    X0, Y0, X1, Y1 = max(0, x - mx), max(0, y - my), min(W, x + w + mx), min(H, y + h + my)
    keep = ~busy[Y0:Y1, X0:X1]
    ext = []
    for k in np.nonzero(flips)[0]:
        m = D[k, Y0:Y1, X0:X1] & keep
        cols, rows = m.sum(axis=0), m.sum(axis=1)
        # ignore stray single pixels (noise): a column/row needs a few
        cols = np.where(cols >= 3, cols, 0); rows = np.where(rows >= 3, rows, 0)
        if cols.sum() == 0 or rows.sum() == 0:
            continue
        a, b = _quantile_span(cols, 0.003)
        c, d = _quantile_span(rows, 0.003)
        ext.append((X0 + a, Y0 + c, X0 + b, Y0 + d))
    if len(ext) < 3:
        return roi
    e = np.array(ext, np.float64)
    q = (1.0 - share) * 100
    ex0, ey0 = np.percentile(e[:, 0], q), np.percentile(e[:, 1], q)
    ex1, ey1 = np.percentile(e[:, 2], 100 - q), np.percentile(e[:, 3], 100 - q)
    nx0, ny0 = min(x, int(ex0)), min(y, int(ey0))
    nx1, ny1 = max(x + w, int(np.ceil(ex1))), max(y + h, int(np.ceil(ey1)))
    if (nx0, ny0, nx1, ny1) == (x, y, x + w, y + h):
        return roi
    # a little room around what was seen
    px, py = int(round(0.015 * (nx1 - nx0))), int(round(0.015 * (ny1 - ny0)))
    nx0, ny0 = max(0, nx0 - px), max(0, ny0 - py)
    nx1, ny1 = min(W, nx1 + px), min(H, ny1 + py)
    return (int(nx0), int(ny0), int(nx1 - nx0), int(ny1 - ny0))


def _detect_region(frames: Sequence[np.ndarray]) -> Tuple[Optional[Rect], str, str]:
    if len(frames) < 6:
        return None, "low", "too few frames"
    st = np.stack([f.astype(np.int16) for f in frames])
    H, W = st.shape[1:]
    D = np.abs(np.diff(st, axis=0)) > DIFF
    ch = D.mean(axis=0)                                       # share of pairs with change
    if ch.mean() < 0.005:
        return None, "low", "almost nothing changes"
    if len(frames) >= 80:
        # many closely spaced samples: keep only what changes AT SLIDE FLIPS.
        # A slide changes all at once when it is turned and stays still in
        # between; a webcam, the audience or camera noise change at any time.
        sync = _flip_score(D)
        if sync is not None:
            ch = sync
    # the slide is the one big, dense block of changes; a webcam, the audience
    # or a banner are separate, smaller / sparser blocks and are left out
    core = _dense_block(ch)
    if core is None:
        return None, "low", "no dense block of changes"
    cx0, cy0, cx1, cy1 = _extend_by_change(ch, core)
    sub = ch[cy0:cy1, cx0:cx1]
    x0, x1 = (cx0 + v for v in _quantile_span(sub.sum(axis=0), 0.02))
    y0, y1 = (cy0 + v for v in _quantile_span(sub.sum(axis=1), 0.02))
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
    if not reasons:
        return roi, "high", "screen recording"
    # the screen fills (almost) the whole picture, e.g. a close-up camera
    if area >= 0.75 and inside >= 0.9 and 1.15 <= aspect <= 2.4:
        return roi, "high", "screen fills the frame"
    # camera recording of a projector: a bright screen in a darker room
    cam = _bright_screen(med, ch, (x0, y0, x1, y1))
    if cam is not None:
        return cam, "high", "projector screen"
    return roi, "low", ", ".join(reasons)


def _flip_score(D: np.ndarray) -> Optional[np.ndarray]:
    """Per pixel: how much more it changes at slide flips than at other times.

    D: (steps, H, W) change masks between consecutive samples.  Flips are the
    steps where clearly more of the picture changes than usual."""
    e = D.mean(axis=(1, 2))
    med = float(np.median(e))
    mad = float(np.median(np.abs(e - med))) + 1e-6
    ev = (e > med + 4 * mad) & (e > 0.004)
    if ev.sum() < 4 or (~ev).sum() < 4:
        return None
    a = D[ev].mean(axis=0)
    b = D[~ev].mean(axis=0)
    s = np.clip(a - 3 * b, 0, 1)
    if s.mean() < 0.002:
        return None
    return s


def _box_blur(a: np.ndarray, k: int) -> np.ndarray:
    """Mean over a k x k window (same size output)."""
    p = k // 2
    s = np.pad(a.astype(np.float64), ((p + 1, p), (p + 1, p)), mode="edge").cumsum(0).cumsum(1)
    H, W = a.shape
    return (s[k:k + H, k:k + W] - s[:H, k:k + W] - s[k:k + H, :W] + s[:H, :W]) / float(k * k)


def _dense_block(ch: np.ndarray) -> Optional[Tuple[int, int, int, int]]:
    """Box (x0, y0, x1, y1) of the largest dense area of change."""
    H, W = ch.shape
    k = max(5, W // 24) | 1
    s = _box_blur(ch, k)
    top = float(np.percentile(s, 99))
    if top <= 0:
        return None
    f = max(1, W // 160)
    hs, ws = H // f, W // f
    small = s[: hs * f, : ws * f].reshape(hs, f, ws, f).mean(axis=(1, 3))
    mask = small > 0.35 * top
    comps, lab = _components(mask)
    if not comps:
        return None
    best = max(comps, key=lambda c: small[lab == c[5]].sum())
    _, y0, x0, y1, x1, _ = best
    # undo the widening by the blur window (half a window on each side)
    m = (k // 2) // f
    x0, y0 = x0 + m, y0 + m
    x1, y1 = max(x0 + 1, x1 - m), max(y0 + 1, y1 - m)
    return x0 * f, y0 * f, min(W, x1 * f), min(H, y1 * f)


def _extend_by_change(ch: np.ndarray, box):
    """Push each side of the box outward while the strip just outside still
    changes clearly (e.g. a title line above the dense block).  A webcam or the
    audience sits behind a gap without change and is not reached."""
    H, W = ch.shape
    x0, y0, x1, y1 = box
    dens = float(ch[y0:y1, x0:x1].mean()) or 1e-6
    step = max(2, W // 160)

    def busy(strip):
        return strip.size and float((strip > 0.05).mean()) >= 0.2 and float(strip.mean()) >= 0.25 * dens
    for _ in range(4):
        moved = False
        while y0 > 0 and busy(ch[max(0, y0 - step):y0, x0:x1]):
            y0 = max(0, y0 - step); moved = True
        while y1 < H and busy(ch[y1:y1 + step, x0:x1]):
            y1 = min(H, y1 + step); moved = True
        while x0 > 0 and busy(ch[y0:y1, max(0, x0 - step):x0]):
            x0 = max(0, x0 - step); moved = True
        while x1 < W and busy(ch[y0:y1, x1:x1 + step]):
            x1 = min(W, x1 + step); moved = True
        if not moved:
            break
    return x0, y0, x1, y1


def _otsu(img: np.ndarray) -> int:
    hist = np.bincount(img.ravel(), minlength=256).astype(np.float64)
    p = hist / hist.sum()
    w = np.cumsum(p)
    mu = np.cumsum(p * np.arange(256))
    with np.errstate(divide="ignore", invalid="ignore"):
        between = (mu[-1] * w - mu) ** 2 / (w * (1 - w))
    return int(np.nanargmax(between))


def _components(mask: np.ndarray):
    """Connected components (4-neighbour) of a small boolean mask:
    list of (pixel count, y0, x0, y1, x1, label image index)."""
    H, W = mask.shape
    lab = np.zeros((H, W), np.int32)
    comps = []
    n = 0
    for sy, sx in zip(*np.nonzero(mask)):
        if lab[sy, sx]:
            continue
        n += 1
        stack = [(sy, sx)]
        lab[sy, sx] = n
        cnt, y0, x0, y1, x1 = 0, sy, sx, sy, sx
        while stack:
            y, x = stack.pop()
            cnt += 1
            y0, x0, y1, x1 = min(y0, y), min(x0, x), max(y1, y), max(x1, x)
            for yy, xx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                if 0 <= yy < H and 0 <= xx < W and mask[yy, xx] and not lab[yy, xx]:
                    lab[yy, xx] = n
                    stack.append((yy, xx))
        comps.append((cnt, y0, x0, y1 + 1, x1 + 1, n))
    return comps, lab


def _bright_screen(med: np.ndarray, ch: np.ndarray, core) -> Optional[Rect]:
    """Bounding box of the projected screen when the video films a projector:
    the bright blob that holds the slide changes, clearly brighter than the room.
    A tilted (trapezoid) screen gives its outer box, so nothing is cut off."""
    H, W = med.shape
    f = max(1, W // 160)                       # work on a ~160 px wide copy
    small = med[: H // f * f, : W // f * f].reshape(H // f, f, W // f, f).mean(axis=(1, 3))
    chs = ch[: H // f * f, : W // f * f].reshape(H // f, f, W // f, f).sum(axis=(1, 3))
    # threshold half way between the dark room and the slide area found above
    cx0, cy0, cx1, cy1 = (int(v) // f for v in core)
    screen = float(np.median(small[cy0:max(cy0 + 1, cy1), cx0:max(cx0 + 1, cx1)]))
    room = float(np.percentile(small, 10))
    if screen - room < 30:
        return None
    thr = (screen + room) / 2.0
    bright = small > thr
    comps, lab = _components(bright)
    if not comps:
        return None
    total = float(chs.sum()) or 1.0
    best = max(comps, key=lambda c: chs[lab == c[5]].sum())
    cnt, y0, x0, y1, x1, idx = best
    box_area = (y1 - y0) * (x1 - x0)
    inside_mass = float(chs[y0:y1, x0:x1].sum()) / total
    fill = cnt / float(box_area)
    inner = small[y0:y1, x0:x1][lab[y0:y1, x0:x1] == idx]
    outer = small[~(lab == idx)]
    contrast = float(np.median(inner) - np.median(outer)) if outer.size else 0.0
    area = box_area / float(small.shape[0] * small.shape[1])
    aspect = (x1 - x0) / float(max(1, y1 - y0))
    if area < 0.15 or inside_mass < 0.85 or fill < 0.7 or contrast < 35 or not 1.1 <= aspect <= 2.5:
        return None
    pad = 1                                    # one small pixel of margin each side
    x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
    x1, y1 = min(small.shape[1], x1 + pad), min(small.shape[0], y1 + pad)
    X0, Y0, X1, Y1 = x0 * f, y0 * f, x1 * f, y1 * f
    # keep it near the block of slide changes: a bright banner or stage light
    # touching the screen is cut off (a tilted screen's corners stay inside)
    mx, my = int(0.08 * (cx1 - cx0) * f), int(0.08 * (cy1 - cy0) * f)
    X0, Y0 = max(X0, cx0 * f - mx), max(Y0, cy0 * f - my)
    X1, Y1 = min(X1, cx1 * f + mx), min(Y1, cy1 * f + my)
    if X1 - X0 < 8 or Y1 - Y0 < 8:
        return None
    return (X0, Y0, X1 - X0, Y1 - Y0)


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


def detect_region_in_video(path: str, n: int = 150):
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


def safe_region(roi: Optional[Rect], conf: str, wh) -> Rect:
    """Region to use without asking anyone.

    A sure region is used as is.  An unsure one is widened (missing part of a
    slide is worse than a slightly wider picture; moving things inside, such
    as a webcam, are ignored by the slide detection anyway).  No region at
    all -> the whole picture."""
    W, H = wh
    if roi is None:
        return (0, 0, W, H)
    if conf == "high":
        return roi
    x, y, w, h = roi
    mx, my = int(0.05 * W), int(0.05 * H)
    x0, y0 = max(0, x - mx), max(0, y - my)
    x1, y1 = min(W, x + w + mx), min(H, y + h + my)
    return (x0, y0, x1 - x0, y1 - y0)


def region_preview(frames, roi: Rect, sure: bool, width: int = 960):
    """JPEG bytes: a typical frame with the chosen region drawn on it
    (green = sure, orange = widened guess) for the user to glance at later."""
    import cv2
    imgs = [f for _, f in frames][:: max(1, len(frames) // 8)]
    med = np.median(np.stack(imgs), axis=0).astype(np.uint8) if len(imgs) > 1 else imgs[0].copy()
    x, y, w, h = roi
    color = (60, 200, 60) if sure else (0, 140, 255)
    t = max(3, med.shape[1] // 300)
    cv2.rectangle(med, (x, y), (x + w - 1, y + h - 1), color, t)
    s = width / float(med.shape[1])
    small = cv2.resize(med, (width, int(med.shape[0] * s)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return buf.tobytes() if ok else None


def auto_region_for_video(path: str, n: int = 150):
    """(region to use, sure?, reason, preview jpeg bytes) - never asks."""
    info, frames = sample_frames(path, n)
    wh = (info["width"], info["height"])
    if not frames:
        return (0, 0) + wh, False, "cannot read frames", None
    small = [to_gray_small(f) for _, f in frames]
    roi, conf, why = detect_region(small)
    if roi is not None:
        sh = small[0].shape
        roi = scale_rect(roi, (sh[1], sh[0]), wh)
    use = safe_region(roi, conf, wh)
    sure = roi is not None and conf == "high"
    try:
        prev = region_preview(frames, use, sure)
    except Exception:
        prev = None
    return use, sure, why, prev
