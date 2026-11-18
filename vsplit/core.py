"""Cut videos into pieces of N minutes.

Two modes:
* "share" (default): every piece is re-encoded small enough to upload
  (``max_mb``, GitHub's web upload takes files up to 25 MB): scaled to
  ``width`` pixels, H.264, no sound.  Plenty for slide analysis.
* "copy": no re-encoding (fast, original quality and size).  Pieces start
  at a key frame, so they can be a few seconds longer or shorter.
"""
from __future__ import annotations

import os
import threading
from fractions import Fraction
from typing import Callable, List, Optional

import av

VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".m4v", ".flv", ".ts",
    ".mts", ".m2ts", ".mpg", ".mpeg", ".3gp", ".asf", ".vob",
}
# containers that take the copied streams of most inputs
COPY_EXT = {".avi": ".avi", ".wmv": ".wmv", ".asf": ".wmv", ".webm": ".mkv", ".flv": ".mkv",
            ".mpg": ".mkv", ".mpeg": ".mkv", ".vob": ".mkv", ".ts": ".ts", ".mts": ".ts",
            ".m2ts": ".ts"}


class Cancelled(Exception):
    pass


def list_videos(paths, recursive: bool = True) -> List[str]:
    out = []
    for p in paths:
        if os.path.isdir(p):
            for d, dirs, files in os.walk(p):
                dirs[:] = sorted(x for x in dirs if not x.startswith((".", "$")) and not x.endswith("_조각"))
                for name in sorted(files):
                    if os.path.splitext(name)[1].lower() in VIDEO_EXTS:
                        out.append(os.path.join(d, name))
                if not recursive:
                    break
        elif os.path.isfile(p) and os.path.splitext(p)[1].lower() in VIDEO_EXTS:
            out.append(p)
    seen, uniq = set(), []
    for p in out:
        k = os.path.normcase(os.path.abspath(p))
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    return uniq


def duration_of(path: str) -> float:
    c = av.open(path)
    try:
        v = c.streams.video[0] if c.streams.video else None
        if v is not None and v.duration and v.time_base:
            return float(v.duration * v.time_base)
        if c.duration:
            return c.duration / av.time_base
        return 0.0
    finally:
        c.close()


def fmt(t: float) -> str:
    t = max(0, int(t))
    return "%02d-%02d-%02d" % (t // 3600, (t % 3600) // 60, t % 60)


def out_folder(path: str, out_dir: Optional[str] = None) -> str:
    base = os.path.splitext(os.path.basename(path))[0]
    d = out_dir or os.path.join(os.path.dirname(os.path.abspath(path)), base + "_조각")
    os.makedirs(d, exist_ok=True)
    return d


def piece_name(folder: str, base: str, i: int, start: float, ext: str) -> str:
    return os.path.join(folder, "%s_%02d (%s)%s" % (base, i, fmt(start), ext))


def _pick_encoder():
    for name in ("libx264", "h264_mf", "libopenh264", "mpeg4"):
        try:
            av.codec.Codec(name, "w")
            return name
        except Exception:
            continue
    raise RuntimeError("영상 인코더를 찾을 수 없습니다.")


def split_video(path: str, minutes: float = 5.0, mode: str = "share", max_mb: float = 24.0,
                width: int = 1280, out_dir: Optional[str] = None,
                progress: Optional[Callable[[float], None]] = None,
                cancel: Optional[threading.Event] = None) -> List[str]:
    if mode == "copy":
        return _split_copy(path, minutes, out_dir, progress, cancel)
    return _split_share(path, minutes, max_mb, width, out_dir, progress, cancel)


# --------------------------------------------------------------------------
# share: re-encode small
# --------------------------------------------------------------------------

def _split_share(path, minutes, max_mb, width, out_dir, progress, cancel) -> List[str]:
    seg = max(1.0, minutes * 60.0)
    total = duration_of(path)
    base = os.path.splitext(os.path.basename(path))[0]
    folder = out_folder(path, out_dir)
    enc_name = _pick_encoder()
    # bitrate that keeps a full piece under max_mb (15% headroom: the encoder overshoots a little)
    bitrate = int(max_mb * 0.85 * 8 * 1024 * 1024 / seg)
    bitrate = max(150_000, min(bitrate, 4_000_000))
    fps_out = 15                                    # plenty for slides, halves the size

    src = av.open(path)
    outputs: List[str] = []
    cur = None          # dict: oc, st, piece, name, last_pts
    try:
        vs = src.streams.video[0]
        vs.thread_type = "AUTO"
        start0 = float(vs.start_time * vs.time_base) if vs.start_time is not None and vs.time_base else 0.0
        next_t = None
        for frame in src.decode(vs):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            if frame.time is None:
                continue
            t = float(frame.time) - start0
            if next_t is not None and t + 1e-6 < next_t:
                continue                                # fewer frames: fps_out
            next_t = (t if next_t is None else next_t) + 1.0 / fps_out
            piece = int(max(0.0, t) // seg)
            if cur is None or piece != cur["piece"]:
                if cur is not None:
                    _close_enc(cur)
                    outputs.append(cur["name"])
                name = piece_name(folder, base, len(outputs) + 1, piece * seg, ".mp4")
                oc, st = _open_enc(name, enc_name, frame, width, bitrate, fps_out)
                cur = dict(oc=oc, st=st, piece=piece, name=name, last_pts=-1)
            st = cur["st"]
            f2 = frame.reformat(width=st.width, height=st.height, format="yuv420p")
            pts = max(cur["last_pts"] + 1, int(round((t - piece * seg) * fps_out)))
            cur["last_pts"] = pts
            f2.pts = pts
            f2.time_base = Fraction(1, fps_out)
            for pkt in st.encode(f2):
                cur["oc"].mux(pkt)
            if progress and total > 0:
                progress(min(1.0, t / total))
        if cur is not None:
            _close_enc(cur)
            outputs.append(cur["name"])
            cur = None
    except BaseException:
        if cur is not None:
            try:
                _close_enc(cur)
            except Exception:
                pass
            try:
                os.remove(cur["name"])         # unfinished piece
            except OSError:
                pass
        raise
    finally:
        src.close()
    return outputs


def _open_enc(name, enc_name, frame, width, bitrate, fps_out):
    sw, sh = frame.width, frame.height
    w = min(width, sw)
    h = int(round(sh * w / float(sw)))
    w, h = w // 2 * 2, max(2, h // 2 * 2)
    oc = av.open(name, "w")
    st = oc.add_stream(enc_name, rate=fps_out)
    st.width, st.height, st.pix_fmt = w, h, "yuv420p"
    st.bit_rate = bitrate
    if enc_name == "libx264":
        st.options = {"preset": "veryfast", "maxrate": str(int(bitrate * 1.2)),
                      "bufsize": str(int(bitrate * 2)), "g": str(fps_out * 4)}
    elif enc_name == "mpeg4":
        st.options = {"g": str(fps_out * 4)}
    st.codec_context.time_base = Fraction(1, fps_out)
    return oc, st


def _close_enc(cur):
    for pkt in cur["st"].encode():
        cur["oc"].mux(pkt)
    cur["oc"].close()


# --------------------------------------------------------------------------
# copy: no re-encoding
# --------------------------------------------------------------------------

def _split_copy(path, minutes, out_dir, progress, cancel) -> List[str]:
    seg = max(1.0, minutes * 60.0)
    total = duration_of(path)
    base, ext = os.path.splitext(os.path.basename(path))
    out_ext = COPY_EXT.get(ext.lower(), ext.lower() if ext.lower() in (".mp4", ".mov", ".m4v", ".mkv") else ".mkv")
    folder = out_folder(path, out_dir)
    src = av.open(path)
    outputs: List[str] = []
    oc = None
    try:
        streams = [s for s in src.streams if s.type in ("video", "audio")]
        vs = src.streams.video[0]
        t0 = None
        idx = 0
        omap = {}
        seg_start = 0.0
        offsets = {}
        for pkt in src.demux(streams):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            if pkt.dts is None or pkt.size == 0:
                continue
            tb = pkt.stream.time_base
            t = float(pkt.dts * tb)
            if t0 is None:
                if pkt.stream is not vs:
                    continue                     # start with the video
                t0 = t
            rel = t - t0
            new_piece = (pkt.stream is vs and pkt.is_keyframe and
                         (oc is None or rel >= seg_start + seg))
            if new_piece:
                if oc is not None:
                    oc.close()
                    outputs.append(name)
                idx += 1
                seg_start = (rel // seg) * seg if oc is not None else 0.0
                name = piece_name(folder, base, idx, seg_start, out_ext)
                oc = av.open(name, "w")
                omap = {s.index: oc.add_stream_from_template(s) for s in streams}
                offsets = {}
            if oc is None:
                continue
            o = omap[pkt.stream.index]
            if pkt.stream.index not in offsets:
                offsets[pkt.stream.index] = pkt.dts
            off = offsets[pkt.stream.index]
            pkt.dts -= off
            if pkt.pts is not None:
                pkt.pts -= off
            if pkt.dts < 0:
                continue
            pkt.stream = o
            oc.mux(pkt)
            if progress and total > 0:
                progress(min(1.0, rel / total))
        if oc is not None:
            oc.close()
            outputs.append(name)
            oc = None
    except BaseException:
        if oc is not None:
            try:
                oc.close()
            except Exception:
                pass
        raise
    finally:
        src.close()
    return outputs
