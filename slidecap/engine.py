"""Slide change detection engine.

Reads a video, watches a user-selected region (the slide area) and captures a
screenshot each time the slide content changes.

Design notes
------------
* Frames are sampled at ``sample_fps`` (4 fps) and analysed on a downscaled,
  blurred grayscale copy of the region.
* Pointer removal: every comparison uses a *clean* frame, the per-pixel
  median of the last 3 samples, so a moving mouse cursor / laser dot
  disappears.  Places where the picture differed only briefly (pointer trail)
  are additionally masked for ``pointer_hold`` seconds, and a small change
  that has the size of the recently seen pointer (a parked cursor) is ignored.
* A slide is captured only after the clean picture has been stable for
  ``stable_samples`` samples, so transitions / fly-in animations are never
  captured half-way, while every animation step (bullet, picture, arrow)
  that stays on screen is captured.
* Dynamic regions (GIF, embedded video, webcam overlay) are found on 8x8
  blocks: a block that keeps changing (``dyn_count`` changed samples or
  ``dyn_edges`` separate change events within ``dyn_window`` samples) is
  ignored until it has been still for a whole window.  Slide flips that
  change a large part of the region are not counted, so fast flipping still
  works.
* Duplicates (going back to an earlier slide) are skipped.
* The saved image is the median of the stable frames (cursor removed).
"""
from __future__ import annotations

import io
import math
import os
import threading
from collections import deque
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple

import av
import cv2
import numpy as np

Rect = Tuple[int, int, int, int]  # x, y, w, h in source video pixels

VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".m4v", ".flv", ".ts",
    ".mts", ".m2ts", ".mpg", ".mpeg", ".3gp", ".asf", ".vob",
}


class Cancelled(Exception):
    pass


@dataclass
class Settings:
    sample_fps: float = 4.0         # analysed frames per second
    work_width: int = 640           # analysis resolution (width)
    pixel_thresh: int = 28          # gray-level difference that counts as change
    tiny_blob_frac: float = 0.0003  # smaller blobs = noise
    small_frac: float = 0.0004      # minimum changed area for a new slide
    change_frac: float = 0.01       # change this large is never a pointer
    stable_samples: int = 3         # samples without change before capturing
    dyn_window: int = 20            # samples of history for dynamic regions
    dyn_count: int = 10             # changed samples in window => dynamic
    dyn_off: int = 40               # still samples before a dynamic area ends
    periodic_span: int = 48         # 4 equally spaced changes within => GIF
    pointer_hold: float = 0.0       # seconds a pointer trail stays masked
    pointer_sig_hold: float = 15.0  # seconds the pointer size is remembered
    median_frames: int = 9          # frames combined to remove the cursor
    dedupe: bool = True             # skip slides identical to an earlier one
    image_format: str = "jpg"       # jpg (smaller) or png (lossless)

    @classmethod
    def preset(cls, name: str) -> "Settings":
        s = cls()
        if name == "high":      # more sensitive: catches tiny changes
            s.pixel_thresh = 20
            s.small_frac = 0.0002
            s.tiny_blob_frac = 0.0002
        elif name == "low":     # less sensitive: ignores small changes
            s.pixel_thresh = 36
            s.small_frac = 0.003
            s.tiny_blob_frac = 0.001
        return s


class Capture:
    """One captured slide.  ``data`` holds the encoded image once final."""

    def __init__(self, time, small, frames, near=None, fmt="jpg"):
        self.time, self.small, self.frames, self.fmt = time, small, frames, fmt
        self.data, self.size = None, (0, 0)
        self._shape = small.shape
        self.near_bits = np.packbits(near) if near is not None else None

    @property
    def near(self) -> Optional[np.ndarray]:
        if self.near_bits is None:
            return None
        n = self._shape[0] * self._shape[1]
        return np.unpackbits(self.near_bits)[:n].reshape(self._shape)

    def finalize(self) -> None:
        if self.data is not None:
            return
        if len(self.frames) >= 3:
            img = np.median(np.stack(self.frames, axis=0), axis=0).astype(np.uint8)
        else:
            img = self.frames[-1]
        self.size = (img.shape[1], img.shape[0])
        self.data = _encode(img, self.fmt)
        self.frames = []

    @property
    def image(self) -> np.ndarray:
        return cv2.imdecode(np.frombuffer(self.data, np.uint8), cv2.IMREAD_COLOR)


# --------------------------------------------------------------------------
# video helpers
# --------------------------------------------------------------------------

def list_videos(path: str) -> List[str]:
    if os.path.isdir(path):
        out = []
        for name in sorted(os.listdir(path)):
            p = os.path.join(path, name)
            if os.path.isfile(p) and os.path.splitext(name)[1].lower() in VIDEO_EXTS:
                out.append(p)
        return out
    return [path]


def _open(path: str):
    container = av.open(path)
    if not container.streams.video:
        container.close()
        raise ValueError("영상 트랙이 없는 파일입니다: %s" % path)
    stream = container.streams.video[0]
    stream.thread_type = "AUTO"
    return container, stream


def video_info(path: str) -> dict:
    container, stream = _open(path)
    try:
        duration = None
        if stream.duration and stream.time_base:
            duration = float(stream.duration * stream.time_base)
        elif container.duration:
            duration = container.duration / av.time_base
        cc = stream.codec_context
        return {"width": cc.width, "height": cc.height, "duration": duration or 0.0}
    finally:
        container.close()


def frame_at(path: str, t: float) -> np.ndarray:
    """Return the BGR frame at (or just after) time ``t`` seconds."""
    container, stream = _open(path)
    try:
        if t > 0 and stream.time_base:
            try:
                container.seek(int(t / stream.time_base), stream=stream,
                               backward=True, any_frame=False)
            except Exception:
                pass
        last = None
        for frame in container.decode(stream):
            last = frame
            ft = frame.time
            if ft is None or ft >= t - 0.02:
                break
        if last is None:
            raise ValueError("프레임을 읽을 수 없습니다.")
        return last.to_ndarray(format="bgr24")
    finally:
        container.close()


# --------------------------------------------------------------------------
# detection
# --------------------------------------------------------------------------

def _median3(a: np.ndarray, b: np.ndarray, c: np.ndarray) -> np.ndarray:
    return np.maximum(np.minimum(a, b), np.minimum(np.maximum(a, b), c))


class _Analyzer:
    BLOCK = 8

    def __init__(self, roi: Rect, excludes: Sequence[Rect], s: Settings):
        self.s = s
        x, y, w, h = roi
        scale = min(1.0, s.work_width / float(w))
        self.ww = max(32, int(round(w * scale)))
        self.wh = max(32, int(round(h * scale)))
        self.valid = np.ones((self.wh, self.ww), np.uint8)
        sx, sy = self.ww / float(w), self.wh / float(h)
        for ex, ey, ew, eh in excludes:
            x0 = int(math.floor((ex - x) * sx)); y0 = int(math.floor((ey - y) * sy))
            x1 = int(math.ceil((ex + ew - x) * sx)); y1 = int(math.ceil((ey + eh - y) * sy))
            x0, y0 = max(0, x0), max(0, y0)
            x1, y1 = min(self.ww, x1), min(self.wh, y1)
            if x1 > x0 and y1 > y0:
                self.valid[y0:y1, x0:x1] = 0
        self.total = float(self.ww * self.wh)
        self.tiny_blob = max(6, int(s.tiny_blob_frac * self.total))
        k = max(3, int(round(self.ww / 90.0)) | 1)
        self.kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (k, k))
        r = max(5, int(round(self.ww * 0.035)) | 1)
        self.ptr_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (r, r))
        r2 = max(9, int(round(self.ww * 0.06)) | 1)
        self.near_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (r2, r2))
        self.bw = (self.ww + self.BLOCK - 1) // self.BLOCK
        self.bh = (self.wh + self.BLOCK - 1) // self.BLOCK
        self.block_kernel = np.ones((3, 3), np.uint8)

    def prep(self, crop: np.ndarray) -> np.ndarray:
        g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        g = cv2.resize(g, (self.ww, self.wh), interpolation=cv2.INTER_AREA)
        return cv2.GaussianBlur(g, (3, 3), 0)

    def diff(self, a: np.ndarray, b: np.ndarray) -> np.ndarray:
        d = cv2.absdiff(a, b)
        return ((d > self.s.pixel_thresh) & (self.valid > 0)).astype(np.uint8)

    def blobs(self, mask: np.ndarray) -> np.ndarray:
        """Connected components (after dilation) as rows of x, y, w, h, area."""
        if not mask.any():
            return np.zeros((0, 5), np.int32)
        md = cv2.dilate(mask, self.kernel)
        n, _, stats, _ = cv2.connectedComponentsWithStats(md, connectivity=8)
        st = stats[1:]
        return st[st[:, cv2.CC_STAT_AREA] >= self.tiny_blob]

    def to_blocks(self, mask: np.ndarray) -> np.ndarray:
        m = cv2.resize(mask.astype(np.float32), (self.bw, self.bh), interpolation=cv2.INTER_AREA)
        return (m > 1e-4).astype(np.int16)

    def dyn_mask(self, bmask: np.ndarray) -> np.ndarray:
        """Dynamic blocks -> pixel mask; each moving area is filled to its
        bounding rectangle (GIFs and videos are rectangular)."""
        b = cv2.dilate(bmask.astype(np.uint8), self.block_kernel)
        n, _, stats, _ = cv2.connectedComponentsWithStats(b, connectivity=8)
        out = np.zeros_like(b)
        for i in range(1, n):
            bx, by, bw, bh = stats[i, :4]
            out[by:by + bh, bx:bx + bw] = 1
        return cv2.resize(out, (self.ww, self.wh), interpolation=cv2.INTER_NEAREST)

    def is_change(self, mask: np.ndarray, ptr_sig=None) -> bool:
        """Decide whether a change mask represents real slide content.

        ``mask`` must already have dynamic / pointer regions removed.
        Large changes are always accepted.  Small changes are rejected when
        they look like a (parked) pointer: at most two compact blobs not
        larger than the pointer recently seen moving.
        """
        st = self.blobs(mask)
        if len(st) == 0:
            return False
        area = float(st[:, cv2.CC_STAT_AREA].sum()) / self.total
        if area >= self.s.change_frac:
            return True
        if area < self.s.small_frac:
            return False
        if ptr_sig is not None and len(st) <= 2:
            (pw, ph, pa), near = ptr_sig
            if all(r[2] <= pw * 1.5 and r[3] <= ph * 1.5 and r[4] <= pa * 1.6
                   and near[r[1]:r[1] + r[3], r[0]:r[0] + r[2]].any() for r in st):
                return False
        return True


def detect_slides(
    path: str,
    roi: Rect,
    excludes: Sequence[Rect] = (),
    settings: Optional[Settings] = None,
    progress: Optional[Callable[[float, int], None]] = None,
    cancel: Optional[threading.Event] = None,
    debug: Optional[Callable[[str], None]] = None,
) -> List[Capture]:
    s = settings or Settings()
    a = _Analyzer(roi, excludes, s)
    x, y, w, h = roi

    container, stream = _open(path)
    duration = 0.0
    if stream.duration and stream.time_base:
        duration = float(stream.duration * stream.time_base)
    elif container.duration:
        duration = container.duration / av.time_base
    start = 0.0
    if stream.start_time is not None and stream.time_base:
        start = float(stream.start_time * stream.time_base)

    step = 1.0 / s.sample_fps
    D = 2                                  # delay for pointer evaluation (samples)
    next_t = None
    raws: deque = deque(maxlen=3)          # raw small frames
    cleans: deque = deque(maxlen=D + 2)    # cleaned (pointer-free) small frames
    transients: deque = deque(maxlen=D + 1)
    ptr_masks: deque = deque()             # (t, mask)
    ptr_sigs: deque = deque()              # (t, (w, h, area))
    ptr_last: deque = deque(maxlen=4)      # last pointer positions (held)
    near = None
    zero_mask = np.zeros((a.wh, a.ww), np.uint8)
    prev_small = None
    prev_small_near = None
    bhist: deque = deque(maxlen=s.dyn_window)
    bsum = np.zeros((a.bh, a.bw), np.int16)
    dyn_state = np.zeros((a.bh, a.bw), np.uint8)
    prev_bm = np.zeros((a.bh, a.bw), np.int16)
    last_change = np.full((a.bh, a.bw), -10 ** 6, np.int64)
    edges = np.zeros((a.bh, a.bw, 4), np.int64)

    run_start = None
    run_t = 0.0
    run_frames: deque = deque(maxlen=s.median_frames)
    stable = 0
    ref = None
    ref_near = None
    run_near = None
    captures: List[Capture] = []
    active: Optional[Capture] = None
    frame_idx = 0
    last_report = -10.0
    sample_idx = 0

    try:
        for frame in container.decode(stream):
            frame_idx += 1
            t = frame.time
            if t is None:
                t = frame_idx / float(stream.average_rate or 30)
            t -= start
            if next_t is not None and t + 1e-6 < next_t:
                continue
            next_t = t + step if next_t is None else max(next_t + step, t + step * 0.5)
            sample_idx += 1

            if cancel is not None and cancel.is_set():
                raise Cancelled()
            if progress and duration > 0 and (t - last_report >= 2.0):
                last_report = t
                progress(min(1.0, t / duration), len(captures))

            img = frame.to_ndarray(format="bgr24")
            crop = img[y:y + h, x:x + w]
            if crop.size == 0:
                raise ValueError("선택 영역이 영상 밖에 있습니다.")
            raw = a.prep(crop)
            raws.append(raw)
            clean = _median3(*raws) if len(raws) == 3 else raw
            clean_prev = cleans[-1] if cleans else clean
            cleans.append(clean)

            # ---- dynamic regions (GIF / video / webcam), block based ----
            motion = a.diff(clean, clean_prev)
            bm = a.to_blocks(motion)
            if len(bhist) == bhist.maxlen:
                bsum -= bhist[0]
            bhist.append(bm)
            bsum += bm
            changed = bm > 0
            last_change[changed] = sample_idx
            edge = changed & (prev_bm == 0)
            prev_bm = bm
            if edge.any():
                edges[edge] = np.roll(edges[edge], -1, axis=1)
                edges[edge, 3] = sample_idx
                e = edges[edge]
                iv = np.diff(e, axis=1)
                periodic = ((e[:, 0] > 0) & (e[:, 3] - e[:, 0] <= s.periodic_span) &
                            (iv.max(axis=1) - iv.min(axis=1) <= 1))
                if periodic.any():
                    tmp = dyn_state[edge]; tmp[periodic] = 1; dyn_state[edge] = tmp
            dyn_state[bsum >= s.dyn_count] = 1
            dyn_state[(sample_idx - last_change) > s.dyn_off] = 0
            dyn = a.dyn_mask(dyn_state) if dyn_state.any() else None

            # ---- pointer tracking ----
            T = a.diff(raw, clean)
            if dyn is not None:
                T &= (1 - dyn)
            transients.append(T)
            if len(transients) == D + 1 and len(cleans) == D + 2:
                # pixels that differed from the clean frame D samples ago and
                # did not become a lasting change => pointer
                persist = a.diff(cleans[-1], cleans[0])
                pm = transients[0] & (1 - persist)
                if pm.any():
                    st = a.blobs(pm)
                    st = st[st[:, cv2.CC_STAT_AREA] < 0.02 * a.total]
                    if len(st):
                        pmd = cv2.dilate(pm, a.ptr_kernel)
                        if s.pointer_hold > 0:
                            ptr_masks.append((t, pmd))
                        ptr_last.append(pmd)
                        acc = ptr_last[0].copy()
                        for m_ in list(ptr_last)[1:]:
                            acc |= m_
                        near = cv2.dilate(acc, a.near_kernel)
                        big = st[np.argmax(st[:, cv2.CC_STAT_AREA])]
                        ptr_sigs.append((t, (big[2], big[3], big[4])))
            # slowly moving pointer: small blobs in consecutive raw differences
            # that continue next to each other
            if len(raws) >= 2:
                rm = a.diff(raw, raws[-2])
                if dyn is not None:
                    rm &= (1 - dyn)
                cur = np.zeros_like(rm)
                for r in a.blobs(rm):
                    if r[4] < 0.01 * a.total:
                        cur[r[1]:r[1] + r[3], r[0]:r[0] + r[2]] = 1
                if cur.any() and prev_small is not None and (cur & prev_small_near).any():
                    ptr_last.append(cv2.dilate(cur | prev_small, a.ptr_kernel))
                    acc = ptr_last[0].copy()
                    for m_ in list(ptr_last)[1:]:
                        acc |= m_
                    near = cv2.dilate(acc, a.near_kernel)
                prev_small = cur if cur.any() else None
                prev_small_near = cv2.dilate(cur, a.near_kernel) if cur.any() else None
            while ptr_masks and t - ptr_masks[0][0] > s.pointer_hold:
                ptr_masks.popleft()
            while ptr_sigs and t - ptr_sigs[0][0] > s.pointer_sig_hold:
                ptr_sigs.popleft()
            ptr_sig = None
            if ptr_sigs:
                arr = np.array([v for _, v in ptr_sigs])
                ptr_sig = tuple(np.percentile(arr, 75, axis=0))
            near_now = near if near is not None else zero_mask

            def chg(m, other_near=None):
                """Real change?  Small cursor-sized blobs where the pointer is
                now, or was when the compared frame was taken, are ignored."""
                if ptr_sig is None:
                    return a.is_change(m)
                n_ = near_now if other_near is None else (near_now | other_near)
                return a.is_change(m, (ptr_sig, n_))

            keep = np.ones((a.wh, a.ww), np.uint8)
            if dyn is not None:
                keep &= (1 - dyn)
            for _, pmk in ptr_masks:
                keep &= (1 - pmk)

            # ---- stability ----
            if run_start is None:
                run_start, run_t, run_near = clean, t, near_now.copy()
                run_frames.append((t, raw, crop.copy()))
                continue
            moving = (chg(a.diff(clean, clean_prev) & keep) or
                      chg(a.diff(clean, run_start) & keep, run_near))
            if debug:
                debug("t=%.2f moving=%d stable=%d dyn=%d ptr=%d sig=%s ref=%s" % (
                    t, moving, stable, 0 if dyn is None else int(dyn.sum()),
                    int(sum(int(m.sum()) for _, m in ptr_masks)),
                    None if ptr_sig is None else tuple(int(v) for v in ptr_sig),
                    None if ref is None else chg(a.diff(clean, ref) & keep, ref_near)))
            if moving:
                if active is not None:
                    active.finalize(); active = None
                stable = 0
                run_start, run_t, run_near = clean, t, near_now.copy()
                run_frames.clear()
                run_frames.append((t, raw, crop.copy()))
            else:
                stable += 1
                if active is not None:
                    active.frames.append(crop.copy())
                    if len(active.frames) >= s.median_frames:
                        active.finalize(); active = None
                else:
                    run_frames.append((t, raw, crop.copy()))

            if stable >= s.stable_samples and active is None:
                if ref is None or chg(a.diff(clean, ref) & keep, ref_near):
                    dup = False
                    if s.dedupe:
                        for c in captures:
                            if not chg(a.diff(clean, c.small) & keep, c.near):
                                dup = True
                                break
                    ref, ref_near = clean, near_now.copy()
                    if debug:
                        debug("t=%.2f %s" % (run_t, "dup" if dup else "capture"))
                    if not dup:
                        # only frames that really show the current picture
                        good = [(ft, cr) for ft, rs, cr in run_frames
                                if not chg(a.diff(rs, clean) & keep)]
                        if not good:
                            good = [(t, crop.copy())]
                        cap = Capture(time=max(0.0, good[0][0] - step), small=clean,
                                      frames=[cr for _, cr in good], near=near_now.copy(),
                                      fmt=s.image_format)
                        captures.append(cap)
                        if len(cap.frames) >= s.median_frames:
                            cap.finalize()
                        else:
                            active = cap
                    run_frames.clear()
        # end of video: the last slide may have been shown only briefly
        if (active is None and run_frames and stable >= 1 and
                (ref is None or chg(a.diff(clean, ref) & keep, ref_near))):
            dup = s.dedupe and any(not chg(a.diff(clean, c.small) & keep, c.near)
                                   for c in captures)
            if not dup:
                good = [(ft, cr) for ft, rs, cr in run_frames
                        if not chg(a.diff(rs, clean) & keep)] or \
                       [(run_frames[-1][0], run_frames[-1][2])]
                captures.append(Capture(time=max(0.0, good[0][0] - step), small=clean,
                                        frames=[cr for _, cr in good], near=near_now.copy(),
                                        fmt=s.image_format))
    finally:
        container.close()

    for c in captures:
        c.finalize()
    if progress:
        progress(1.0, len(captures))
    return captures


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------

def fmt_time(t: float) -> str:
    t = max(0, int(t))
    return "%02d:%02d:%02d" % (t // 3600, (t % 3600) // 60, t % 60)


def _encode(img: np.ndarray, fmt: str) -> bytes:
    if fmt == "png":
        ok, buf = cv2.imencode(".png", img, [cv2.IMWRITE_PNG_COMPRESSION, 6])
    else:
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 95])
    if not ok:
        raise RuntimeError("이미지 인코딩 실패")
    return buf.tobytes()


def unique_path(p: str) -> str:
    if not os.path.exists(p):
        return p
    base, ext = os.path.splitext(p)
    i = 1
    while os.path.exists("%s (%d)%s" % (base, i, ext)):
        i += 1
    return "%s (%d)%s" % (base, i, ext)


def write_pptx(captures: Sequence[Capture], out_path: str,
               image_dir: Optional[str] = None, title: str = "") -> str:
    from pptx import Presentation
    from pptx.util import Emu

    if not captures:
        raise ValueError("캡처된 슬라이드가 없습니다.")
    w, h = captures[0].size
    prs = Presentation()
    sw = 12192000  # 13.333 in (16:9 width)
    sh = int(round(sw * h / float(w)))
    sh = max(914400, min(sh, 51206400))
    prs.slide_width, prs.slide_height = Emu(sw), Emu(sh)
    layout = prs.slide_layouts[6]  # blank

    if image_dir:
        os.makedirs(image_dir, exist_ok=True)
    for i, c in enumerate(captures, 1):
        data = c.data
        if image_dir:
            fn = os.path.join(image_dir, "slide_%03d_%s.%s" % (i, fmt_time(c.time).replace(":", "-"), c.fmt))
            with open(fn, "wb") as f:
                f.write(data)
        slide = prs.slides.add_slide(layout)
        iw, ih = c.size
        # fit image into slide keeping aspect ratio
        r = min(sw / float(iw), sh / float(ih))
        pw, ph = int(iw * r), int(ih * r)
        slide.shapes.add_picture(io.BytesIO(data), Emu((sw - pw) // 2), Emu((sh - ph) // 2), Emu(pw), Emu(ph))
        notes = slide.notes_slide.notes_text_frame
        notes.text = "%s영상 시간 %s" % ((title + " / ") if title else "", fmt_time(c.time))

    out_path = unique_path(out_path)
    prs.save(out_path)
    return out_path


def process_video(path: str, roi: Rect, excludes: Sequence[Rect] = (),
                  settings: Optional[Settings] = None, out_dir: Optional[str] = None,
                  save_images: bool = False,
                  progress=None, cancel=None) -> Tuple[Optional[str], int]:
    caps = detect_slides(path, roi, excludes, settings, progress, cancel)
    if not caps:
        return None, 0
    base = os.path.splitext(os.path.basename(path))[0]
    out_dir = out_dir or os.path.dirname(os.path.abspath(path))
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, base + ".pptx")
    img_dir = unique_path(os.path.join(out_dir, base + "_slides")) if save_images else None
    return write_pptx(caps, out, img_dir, title=base), len(caps)
