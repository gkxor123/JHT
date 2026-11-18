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
* Camera shake: lectures filmed with a camera jitter, wobble and change
  exposure.  Every frame is aligned to the previous picture (phase
  correlation) before it is analysed, and every comparison tolerates a
  shift of a pixel or two and a global brightness / contrast change, so a
  shaking camera is not taken for a new slide.
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
    dyn_count: int = 8              # samples with motion in a region => dynamic
    dyn_off: int = 4                # extra samples a dynamic area stays ignored
    global_frac: float = 0.25       # change this large = slide flip
    periodic_span: int = 48         # 4 equally spaced changes within => GIF
    pointer_hold: float = 0.0       # seconds a pointer trail stays masked
    median_frames: int = 9          # frames combined to remove the cursor
    dedupe: bool = True             # skip slides identical to an earlier one
    collapse_builds: bool = True    # text/figure build-up: keep only final step
    image_format: str = "jpg"       # jpg (smaller) or png (lossless)
    stabilize: bool = True          # compensate camera shake / exposure drift
    shift_tol: int = 1              # px (analysis res.) a pixel may move unnoticed
    max_shift: float = 0.04         # largest shake followed, part of the width

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

    def __init__(self, time, small, frames, near=None, fmt="jpg", dyn=None):
        self.time, self.small, self.frames, self.fmt = time, small, frames, fmt
        self.data, self.size = None, (0, 0)
        self._shape = small.shape
        self.near_bits = np.packbits(near) if near is not None else None
        self.dyn_bits = np.packbits(dyn) if dyn is not None else None
        self.merged = 1          # how many build steps this slide represents

    def _unpack(self, bits):
        if bits is None:
            return None
        n = self._shape[0] * self._shape[1]
        return np.unpackbits(bits)[:n].reshape(self._shape)

    @property
    def near(self) -> Optional[np.ndarray]:
        return self._unpack(self.near_bits)

    @property
    def dyn(self) -> Optional[np.ndarray]:
        return self._unpack(self.dyn_bits)

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

def list_videos(path: str, recursive: bool = False) -> List[str]:
    if os.path.isdir(path):
        out = []
        if recursive:
            for d, dirs, files in os.walk(path):
                dirs[:] = sorted(x for x in dirs if not x.startswith((".", "$")))
                for name in sorted(files):
                    if os.path.splitext(name)[1].lower() in VIDEO_EXTS:
                        out.append(os.path.join(d, name))
            return out
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


MAX_BAD_PACKETS = 3000   # give up only if the damage never ends


def _frames(container, stream, stats: Optional[dict] = None):
    """Decode ``stream`` frame by frame, skipping damaged packets.

    ``container.decode`` stops at the first broken packet ("Invalid data
    found when processing input"), which threw away whole lectures whose
    file had one damaged spot.  Here a packet that fails to demux or decode
    is skipped; the decoder recovers at the next key frame.  Only a file
    that never decodes again (MAX_BAD_PACKETS in a row) raises.
    """
    bad_run = 0
    it = container.demux(stream)
    while True:
        try:
            packet = next(it)
        except StopIteration:
            return
        except av.error.EOFError:
            return
        except av.error.FFmpegError:
            bad_run += 1
            if stats is not None:
                stats["bad"] = stats.get("bad", 0) + 1
            if bad_run > MAX_BAD_PACKETS:
                raise
            continue
        try:
            frames = packet.decode()
        except av.error.FFmpegError:
            bad_run += 1
            if stats is not None:
                stats["bad"] = stats.get("bad", 0) + 1
            if bad_run > MAX_BAD_PACKETS:
                raise
            continue
        for frame in frames:
            bad_run = 0
            yield frame


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
        for frame in _frames(container, stream):
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
        self.ptr_templates: List[np.ndarray] = []
        self._tpl_i = 0
        st = s.shift_tol if s.stabilize else 0
        self.tol_kernel = (cv2.getStructuringElement(cv2.MORPH_RECT, (2 * st + 1, 2 * st + 1))
                           if st > 0 else None)
        self.hann = cv2.createHanningWindow((self.ww, self.wh), cv2.CV_32F)
        ys, xs = np.nonzero(self.valid[::4, ::4])
        self.sub = (ys * 4, xs * 4)

    # ---- camera shake --------------------------------------------------
    def shift_of(self, img: np.ndarray, ref: np.ndarray) -> Tuple[float, float]:
        """Translation (dx, dy) that moves ``img`` onto ``ref``; (0, 0) when
        the two pictures do not match (e.g. a different slide)."""
        if not self.s.stabilize:
            return 0.0, 0.0
        (dx, dy), resp = cv2.phaseCorrelate(ref.astype(np.float32), img.astype(np.float32), self.hann)
        lim = self.s.max_shift * self.ww
        if resp < 0.05 or abs(dx) > lim or abs(dy) > lim:
            return 0.0, 0.0
        if abs(dx) < 0.25 and abs(dy) < 0.25:
            return 0.0, 0.0
        return -dx, -dy

    @staticmethod
    def shift(img: np.ndarray, dx: float, dy: float) -> np.ndarray:
        if dx == 0 and dy == 0:
            return img
        M = np.float32([[1, 0, dx], [0, 1, dy]])
        return cv2.warpAffine(img, M, (img.shape[1], img.shape[0]),
                              flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)

    def align(self, img: np.ndarray, ref: np.ndarray) -> np.ndarray:
        dx, dy = self.shift_of(img, ref)
        return self.shift(img, dx, dy)

    def _match_exposure(self, a: np.ndarray, b: np.ndarray) -> np.ndarray:
        """``b`` with brightness / contrast matched to ``a`` (auto exposure
        of a camera).  Fitted on pixels that agree, so new content on the
        slide does not disturb the fit."""
        ys, xs = self.sub
        if len(ys) < 50:
            return b
        va = a[ys, xs].astype(np.float32)
        vb = b[ys, xs].astype(np.float32)
        g, o = 1.0, float(np.median(va - vb))
        for _ in range(2):
            use = np.abs(va - (g * vb + o)) < 20
            if use.sum() < 0.3 * len(va):
                break
            A = np.stack([vb[use], np.ones(int(use.sum()), np.float32)], 1)
            (g, o), *_ = np.linalg.lstsq(A, va[use], rcond=None)
            g = float(np.clip(g, 0.8, 1.25)); o = float(np.clip(o, -40, 40))
        if abs(g - 1.0) < 0.01 and abs(o) < 2.0:
            return b
        return cv2.convertScaleAbs(b, alpha=g, beta=o)

    def diff_aligned(self, a: np.ndarray, b: np.ndarray) -> np.ndarray:
        """``diff`` after moving ``b`` onto ``a`` (pictures taken far apart in
        time, when the camera may have drifted)."""
        return self.diff(a, self.align(b, a))

    def add_pointer_template(self, patch: np.ndarray) -> None:
        """Remember what the pointer looks like, at full resolution.

        ``patch`` is a gray full-resolution crop around a pointer that was
        seen moving.  Pixels that differ from the patch border (the cursor's
        fill/outline) form the template mask."""
        if patch.shape[0] < 6 or patch.shape[1] < 6:
            return
        border = np.concatenate([patch[0], patch[-1], patch[:, 0], patch[:, -1]])
        bg = float(np.median(border))
        if np.abs(border.astype(np.float32) - bg).mean() > 12:
            return  # busy background: cursor shape not separable
        mask = (np.abs(patch.astype(np.int16) - bg) > 40).astype(np.uint8)
        if mask.sum() < 15:
            return
        ys, xs = np.nonzero(mask)
        y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
        tpl = (patch[y0:y1, x0:x1].copy(), mask[y0:y1, x0:x1].copy())
        if len(self.ptr_templates) < 40:
            self.ptr_templates.append(tpl)
        else:
            self.ptr_templates[self._tpl_i % 40] = tpl
        self._tpl_i += 1

    def looks_like_pointer(self, grays, x, y, w, h) -> bool:
        """Is there a pointer (as learnt) inside the work-res box x,y,w,h of
        one of the full-resolution gray images?"""
        if not self.ptr_templates:
            return False
        for g in grays:
            sx = g.shape[1] / float(self.ww)
            sy = g.shape[0] / float(self.wh)
            pad = 6
            X0 = max(0, int((x - pad) * sx)); Y0 = max(0, int((y - pad) * sy))
            X1 = min(g.shape[1], int((x + w + pad) * sx)); Y1 = min(g.shape[0], int((y + h + pad) * sy))
            reg = g[Y0:Y1, X0:X1]
            for tpl, msk in self.ptr_templates:
                th, tw = tpl.shape
                if th > reg.shape[0] or tw > reg.shape[1]:
                    continue
                r = cv2.matchTemplate(reg.astype(np.float32), tpl.astype(np.float32),
                                      cv2.TM_SQDIFF, mask=msk.astype(np.float32))
                if float(r.min()) / float(msk.sum()) < 25.0 ** 2:
                    return True
        return False

    def background(self, small: np.ndarray) -> np.ndarray:
        """Mask of plain slide background (smooth surface incl. gradients)."""
        g = cv2.resize(small, (80, 45), interpolation=cv2.INTER_AREA).astype(np.float32)
        yy, xx = np.mgrid[0:45, 0:80].astype(np.float32)
        xx /= 80.0; yy /= 45.0
        A = np.stack([np.ones_like(xx), xx, yy, xx * xx, yy * yy, xx * yy], -1).reshape(-1, 6)
        v = g.reshape(-1)
        # start from the dominant brightness, then follow gradients
        hist = np.bincount(np.clip(v, 0, 255).astype(np.int32), minlength=256)
        mode = int(np.argmax(np.convolve(hist, np.ones(9), "same")))
        use = np.abs(v - mode) < 16
        coef = None
        for _ in range(4):
            if use.sum() < 50:
                break
            coef, *_ = np.linalg.lstsq(A[use], v[use], rcond=None)
            use = np.abs(A @ coef - v) < 10
        if coef is None:
            return np.zeros_like(small, bool)
        Y, X = np.mgrid[0:self.wh, 0:self.ww].astype(np.float32)
        X /= self.ww; Y /= self.wh
        surf = coef[0] + coef[1] * X + coef[2] * Y + coef[3] * X * X + coef[4] * Y * Y + coef[5] * X * Y
        bg = np.abs(small.astype(np.float32) - surf) < 14
        # the fitted surface must describe a good part of the slide
        if bg.mean() < 0.25:
            return np.zeros_like(small, bool)
        return bg

    def prep(self, crop: np.ndarray) -> np.ndarray:
        g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        g = cv2.resize(g, (self.ww, self.wh), interpolation=cv2.INTER_AREA)
        return cv2.GaussianBlur(g, (3, 3), 0)

    def diff(self, a: np.ndarray, b: np.ndarray) -> np.ndarray:
        t = self.s.pixel_thresh
        if not self.s.stabilize:
            d = cv2.absdiff(a, b)
            return ((d > t) & (self.valid > 0)).astype(np.uint8)
        b = self._match_exposure(a, b)
        d = cv2.absdiff(a, b) > t
        if d.any() and self.tol_kernel is not None:
            # a pixel changed only if no pixel close by in the other picture
            # looks like it (edges moved by a shaking camera are not change)
            k = self.tol_kernel
            ai, bi = a.astype(np.int16), b.astype(np.int16)
            bmax, bmin = cv2.dilate(b, k).astype(np.int16), cv2.erode(b, k).astype(np.int16)
            amax, amin = cv2.dilate(a, k).astype(np.int16), cv2.erode(a, k).astype(np.int16)
            d &= ((ai > bmax + t) | (ai < bmin - t) | (bi > amax + t) | (bi < amin - t))
        return (d & (self.valid > 0)).astype(np.uint8)

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
    crops: deque = deque(maxlen=3)         # matching full-resolution crops
    cleans: deque = deque(maxlen=D + 2)    # cleaned (pointer-free) small frames
    transients: deque = deque(maxlen=D + 1)
    ptr_masks: deque = deque()             # (t, mask)
    ptr_sigs: deque = deque(maxlen=400)    # (t, (w, h, area)) pointer sizes
    ptr_last: deque = deque(maxlen=4)      # last pointer positions (held)
    near = None
    zero_mask = np.zeros((a.wh, a.ww), np.uint8)
    prev_small = None
    prev_small_near = None
    bhist: deque = deque(maxlen=s.dyn_window)
    ehist: deque = deque(maxlen=s.dyn_window)
    dyn_seen = np.full((a.bh, a.bw), -10 ** 6, np.int64)
    prev_bm = np.zeros((a.bh, a.bw), np.int16)
    sig_area = 0.0
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
        for frame in _frames(container, stream):
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
            if s.stabilize and cleans:
                dx, dy = a.shift_of(raw, cleans[-1])
                if dx or dy:
                    raw = a.shift(raw, dx, dy)
                    crop = a.shift(crop, dx * crop.shape[1] / float(a.ww),
                                   dy * crop.shape[0] / float(a.wh))
            raws.append(raw)
            crops.append(crop)
            clean = _median3(*raws) if len(raws) == 3 else raw
            clean_prev = cleans[-1] if cleans else clean
            cleans.append(clean)

            # ---- dynamic regions (GIF / video / webcam), block based ----
            # motion sources: changes of the clean picture, plus short-lived
            # changes much larger than a pointer (fast GIF objects / video,
            # which the median removes just like a cursor)
            motion = a.diff(clean, clean_prev)
            T = a.diff(raw, clean)
            # a change covering much of the slide is a flip, not animation
            bm = a.to_blocks(motion) if motion.mean() < s.global_frac else \
                np.zeros((a.bh, a.bw), np.int16)
            if T.any():
                big_area = max(2.5 * sig_area, 0.004 * a.total)
                tb = a.blobs(T)
                tb = tb[tb[:, cv2.CC_STAT_AREA] > big_area]
                if len(tb):
                    tm = np.zeros_like(T)
                    for x_, y_, w_, h_, _ in tb:
                        tm[y_:y_ + h_, x_:x_ + w_] = 1
                    bm |= a.to_blocks(tm)
            bhist.append(bm)
            changed = bm > 0
            edge = changed & (prev_bm == 0)
            ehist.append(edge.astype(np.int16))
            prev_bm = bm
            if edge.any():   # periodic changes (slow GIF)
                edges[edge] = np.roll(edges[edge], -1, axis=1)
                edges[edge, 3] = sample_idx
                e = edges[edge]
                iv = np.diff(e, axis=1)
                periodic = ((e[:, 0] > 0) & (e[:, 3] - e[:, 0] <= s.periodic_span) &
                            (iv.max(axis=1) - iv.min(axis=1) <= 1))
                if periodic.any():
                    tmp = dyn_seen[edge]; tmp[periodic] = sample_idx; dyn_seen[edge] = tmp
            # regions (connected over the window) that moved in many samples
            bsum = np.zeros((a.bh, a.bw), np.int16)
            for h_ in bhist:
                bsum += h_
            esum = np.zeros((a.bh, a.bw), np.int16)
            for h_ in ehist:
                esum += h_
            # blocks that keep moving or changed in several separate events;
            # a single change (slide flip, picture swap) does not count
            moving_blk = (bsum >= 6) | (esum >= 2)
            union = moving_blk.astype(np.uint8)
            if union.any():
                union = cv2.dilate(union, a.block_kernel)
                n_, lab, st_, _ = cv2.connectedComponentsWithStats(union, connectivity=8)
                counts = np.zeros(n_, np.int32)
                for h_ in bhist:
                    if h_.any():
                        counts[np.unique(lab[h_ > 0])] += 1
                for i_ in range(1, n_):
                    if counts[i_] >= s.dyn_count:
                        x_, y_, w_, h_ = st_[i_, :4]
                        sub = (lab[y_:y_ + h_, x_:x_ + w_] == i_) & moving_blk[y_:y_ + h_, x_:x_ + w_]
                        ys, xs = np.nonzero(sub)
                        dyn_seen[y_ + ys.min():y_ + ys.max() + 1, x_ + xs.min():x_ + xs.max() + 1] = sample_idx
            dyn_state = ((sample_idx - dyn_seen) <= s.dyn_off).astype(np.uint8)
            dyn = a.dyn_mask(dyn_state) if dyn_state.any() else None

            # ---- pointer tracking ----
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
                    st = st[st[:, cv2.CC_STAT_AREA] < 0.008 * a.total]
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
                        bx, by, bw_, bh_ = big[:4]
                        if len(crops) == 3 and bw_ >= 4 and bh_ >= 4:
                            fx = crops[0].shape[1] / float(a.ww); fy = crops[0].shape[0] / float(a.wh)
                            pg = cv2.cvtColor(crops[0][int(by * fy):int((by + bh_) * fy),
                                                       int(bx * fx):int((bx + bw_) * fx)], cv2.COLOR_BGR2GRAY)
                            a.add_pointer_template(pg)
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
            ptr_sig = None
            if ptr_sigs:
                arr = np.array([v for _, v in ptr_sigs])
                ptr_sig = tuple(np.percentile(arr, 75, axis=0))
                sig_area = float(ptr_sig[2])
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
                if ref is None or chg(a.diff_aligned(clean, ref) & keep, ref_near):
                    dup = False
                    if s.dedupe:
                        for c in captures:
                            if not chg(a.diff_aligned(clean, c.small) & keep, c.near):
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
                                      fmt=s.image_format, dyn=dyn)
                        captures.append(cap)
                        if len(cap.frames) >= s.median_frames:
                            cap.finalize()
                        else:
                            active = cap
                    run_frames.clear()
        # end of video: the last slide may have been shown only briefly
        if (active is None and run_frames and stable >= 1 and
                (ref is None or chg(a.diff_aligned(clean, ref) & keep, ref_near))):
            dup = s.dedupe and any(not chg(a.diff_aligned(clean, c.small) & keep, c.near)
                                   for c in captures)
            if not dup:
                good = [(ft, cr) for ft, rs, cr in run_frames
                        if not chg(a.diff(rs, clean) & keep)] or \
                       [(run_frames[-1][0], run_frames[-1][2])]
                captures.append(Capture(time=max(0.0, good[0][0] - step), small=clean,
                                        frames=[cr for _, cr in good], near=near_now.copy(),
                                        fmt=s.image_format, dyn=dyn))
    finally:
        container.close()

    for c in captures:
        c.finalize()
    sig = None
    if ptr_sigs:
        sig = tuple(np.percentile(np.array([v for _, v in ptr_sigs]), 75, axis=0))
    captures = merge_captures(captures, a, s, sig, debug)
    if progress:
        progress(1.0, len(captures))
    return captures


# --------------------------------------------------------------------------
# post-processing: drop pointer-only changes, collapse build-up animations
# --------------------------------------------------------------------------

def _relation(p: Capture, c: Capture, a: _Analyzer, sig) -> Tuple[str, bool]:
    """How does capture ``c`` differ from the previous capture ``p``?

    Returns (kind, keep_c) where kind is
      "pointer"  - only cursor-sized spots differ (pointer appeared/vanished),
                   or a meeting-app overlay (chat notification box at the
                   bottom edge) appeared/vanished
      "addition" - content was added onto empty background (bullet, text,
                   figure building up); nothing that was there changed
      "different"- anything else (new slide, image replaced, ...)
    keep_c tells, for "pointer", which version is cleaner (fewer edges,
    or without the overlay box).
    """
    # ignore only moving content present in both (e.g. the same GIF)
    excl = np.zeros_like(p.small)
    if p.dyn is not None and c.dyn is not None:
        excl = p.dyn & c.dyn
    dyn_any = None   # moving content seen in either capture
    if p.dyn is not None or c.dyn is not None:
        dyn_any = (p.dyn if p.dyn is not None else 0) | (c.dyn if c.dyn is not None else 0)
    m = a.diff_aligned(p.small, c.small) & (1 - excl)
    if not m.any():
        return "pointer", False
    md = cv2.dilate(m, a.kernel)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(md, connectivity=8)
    if sig is None:  # typical cursor size relative to the analysed width
        pw, ph = a.ww * 0.032, a.ww * 0.042
        sig = (pw, ph, pw * ph * 0.75)
    pw, ph, pa = sig
    ea = cv2.Canny(p.small, 40, 120) > 0
    eb = cv2.Canny(c.small, 40, 120) > 0
    bg_a = a.background(p.small)
    bg_b = a.background(c.small)
    grays = None
    kinds = []
    edge_a = edge_b = 0
    dark_a = dark_b = 0.0
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if area < a.tiny_blob:
            continue
        reg = labels[y:y + h, x:x + w] == i
        fa = float(ea[y:y + h, x:x + w][reg].mean())
        fb = float(eb[y:y + h, x:x + w][reg].mean())
        mreg = m[y:y + h, x:x + w].astype(bool) & reg
        if mreg.sum() < max(12, 0.0001 * a.total):
            continue   # faint residue (e.g. trace of a cursor), not content
        # changed pixels that were plain background before / are now
        on_bg_a = float(bg_a[y:y + h, x:x + w][mreg].mean()) if mreg.any() else 0.0
        on_bg_b = float(bg_b[y:y + h, x:x + w][mreg].mean()) if mreg.any() else 0.0
        ov = _overlay_box(p.small, c.small, a, x, y, w, h)
        if ov is not None:
            kinds.append("overlay")
            dark_a += ov[0]; dark_b += ov[1]
            continue
        if _edge_strip(a, x, y, w, h):
            # a thin strip along the region's edge: a webcam / window border
            # that slipped into the region, not slide content
            kinds.append("edge")
            continue
        small = w <= pw * 1.6 and h <= ph * 1.6 and area <= pa * 2.0
        if small and grays is None:
            grays = [cv2.cvtColor(p.image, cv2.COLOR_BGR2GRAY), cv2.cvtColor(c.image, cv2.COLOR_BGR2GRAY)]
        if small and a.looks_like_pointer(grays, x, y, w, h):
            kinds.append("pointer")
            edge_a += fa * area; edge_b += fb * area
        elif on_bg_a > 0.85 and on_bg_b < 0.7:
            kinds.append("addition")   # drawn onto empty background
        elif dyn_any is not None and float(dyn_any[y:y + h, x:x + w][mreg].mean()) > 0.8:
            kinds.append("moving")     # GIF seen moving in one capture only
        else:
            kinds.append("other")
    if "overlay" in kinds and all(k in ("pointer", "overlay", "moving", "edge") for k in kinds):
        return "pointer", dark_b < dark_a
    kinds = ["other" if k == "moving" else k for k in kinds]  # unchanged rule without an overlay
    if "edge" in kinds and all(k in ("pointer", "edge") for k in kinds):
        return "pointer", False                      # keep the earlier capture
    kinds = [k for k in kinds if k != "edge"]
    if not kinds or all(k == "pointer" for k in kinds):
        return "pointer", edge_b < edge_a
    if "other" not in kinds:
        return "addition", True
    return "different", True


def _edge_strip(a: "_Analyzer", x, y, w, h) -> bool:
    """Changed box lying entirely in the outer 6% band along one side of the
    region: e.g. a speaker's webcam edge caught inside the region (often in
    several detached pieces).  Slide text practically never sits that close
    to the slide edge, and a real slide change is bigger than this anyway."""
    W, H = a.ww, a.wh
    bx, by = max(4, int(W * 0.06)), max(4, int(H * 0.06))
    return x + w <= bx or x >= W - bx or y + h <= by or y >= H - by


def _overlay_box(sa: np.ndarray, sb: np.ndarray, a: "_Analyzer", x, y, w, h):
    """Is the changed box x,y,w,h a notification box of the meeting app
    (e.g. Zoom chat "... to everyone") that is shown in only one capture?

    It sits at the bottom edge, is small, and is a dark filled box in one
    capture and not in the other.  Returns (dark_a, dark_b) or None."""
    W, H = a.ww, a.wh
    if not (y >= H * 0.7 and y + h >= H * 0.9 and w <= W * 0.35 and h <= H * 0.22):
        return None
    def dark(s, x0, y0, x1, y1):
        return float((s[max(0, y0):min(H, y1), max(0, x0):min(W, x1)] < 70).mean())
    # a small colored part of the box (avatar circle) is not dark itself but
    # sits inside the dark box: also judge it with a thin ring around it
    pad = min(w, h) // 2 + 3
    da = max(dark(sa, x, y, x + w, y + h), dark(sa, x - pad, y - pad, x + w + pad, y + h + pad))
    db = max(dark(sb, x, y, x + w, y + h), dark(sb, x - pad, y - pad, x + w + pad, y + h + pad))
    if max(da, db) < 0.45:
        return None
    # the change may be only a part of the box (a text line, the avatar):
    # find the top of the dark box first, looking a bit wider than the
    # change; rows with text inside the box are still partly dark
    xa, xb = max(0, x - pad), min(W, x + w + pad)
    top, lim = y, int(H * 0.6)
    while top > lim and max(float((sa[top - 1, xa:xb] < 70).mean()),
                            float((sb[top - 1, xa:xb] < 70).mean())) > 0.15:
        top -= 1
    if top <= lim:
        return None
    # the box must be a dark island on a light slide: the strip just above
    # it is light in both captures (rules out dark-themed slides)
    t = max(0, top - max(4, h // 4))
    if t >= top:
        return None
    above_a = float((sa[t:top, xa:xb] < 70).mean())
    above_b = float((sb[t:top, xa:xb] < 70).mean())
    if max(above_a, above_b) > 0.25:
        return None
    # the box reaches the bottom edge (a dark picture on the slide usually
    # has a light margin or caption below it)
    s = sa if da >= db else sb
    for r in range(y + h, int(H * 0.97)):
        if float((s[r, xa:xb] < 70).mean()) <= 0.15:
            return None
    return da, db


def _learn_pointer_from_captures(caps: List[Capture], a: _Analyzer, sig, debug=None) -> int:
    """Learn the cursor's look from the captures themselves.

    A small shape that shows up between neighbouring captures at several
    different places is the cursor (an arrow drawn on a slide appears once
    and stays put).  Each such shape becomes a pointer template, so the merge
    below treats it like a cursor even when it was never seen moving."""
    if len(caps) < 4:
        return 0
    if sig is None:
        pw, ph = a.ww * 0.032, a.ww * 0.042
    else:
        pw, ph = sig[0], sig[1]
    items = []   # (descriptor, centre, capture index, box)
    for i in range(1, len(caps)):
        m = a.diff_aligned(caps[i - 1].small, caps[i].small)
        if m.mean() > 0.01:
            continue                       # a real slide change, not a cursor move
        md = cv2.dilate(m, a.kernel)
        n, _, stats, _ = cv2.connectedComponentsWithStats(md, connectivity=8)
        for k in range(1, n):
            x, y, w, h, area = stats[k]
            if area < a.tiny_blob or w > pw * 1.8 or h > ph * 1.8:
                continue
            # the capture where the shape is (more contrast inside the box)
            pa_ = caps[i - 1].small[y:y + h, x:x + w].astype(np.float32)
            pb_ = caps[i].small[y:y + h, x:x + w].astype(np.float32)
            j, patch = (i - 1, pa_) if pa_.std() > pb_.std() else (i, pb_)
            if patch.std() < 8:
                continue
            d = cv2.resize(patch, (12, 12), interpolation=cv2.INTER_AREA).ravel()
            d = d - d.mean()
            nrm = float(np.linalg.norm(d))
            if nrm < 1e-3:
                continue
            items.append((d / nrm, (x + w / 2.0, y + h / 2.0), j, (x, y, w, h)))
    if len(items) < 3:
        return 0
    D = np.stack([it[0] for it in items])
    sim = D @ D.T
    C = np.array([it[1] for it in items])
    dist = np.sqrt(((C[:, None, :] - C[None, :, :]) ** 2).sum(-1))
    far = dist > max(pw, ph)
    learned, done = 0, set()
    for k, it in enumerate(items):
        if int(((sim[k] > 0.85) & far[k]).sum()) < 2:
            continue                       # seen in fewer than 3 places
        j, (x, y, w, h) = it[2], it[3]
        key = (j, x // 4, y // 4)
        if key in done:
            continue
        done.add(key)
        g = cv2.cvtColor(caps[j].image, cv2.COLOR_BGR2GRAY)
        sx, sy = g.shape[1] / float(a.ww), g.shape[0] / float(a.wh)
        pad = 3
        X0, Y0 = max(0, int((x - pad) * sx)), max(0, int((y - pad) * sy))
        X1, Y1 = min(g.shape[1], int((x + w + pad) * sx)), min(g.shape[0], int((y + h + pad) * sy))
        before = a._tpl_i
        a.add_pointer_template(g[Y0:Y1, X0:X1])
        if a._tpl_i > before:
            learned += 1
        if learned >= 12:
            break
    if debug:
        debug("learned %d pointer templates from recurring small shapes" % learned)
    return learned


def merge_captures(caps: List[Capture], a: _Analyzer, s: Settings, sig,
                   debug=None) -> List[Capture]:
    _learn_pointer_from_captures(caps, a, sig, debug)
    out: List[Capture] = []
    for c in caps:
        if out:
            p = out[-1]
            kind, keep_c = _relation(p, c, a, sig)
            if debug:
                debug("t=%.2f merge-check vs %.2f: %s" % (c.time, p.time, kind))
            if kind == "pointer":
                if keep_c:
                    c.time, c.merged = p.time, p.merged
                    out[-1] = c
                continue
            if kind == "addition" and s.collapse_builds:
                c.time, c.merged = p.time, p.merged + 1
                out[-1] = c
                continue
        out.append(c)
    return out


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
