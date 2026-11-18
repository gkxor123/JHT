"""VSplit: cut a synthetic lecture video into pieces (both modes).

Run:  python tests/test_vsplit.py [workdir]
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import av  # noqa: E402
import numpy as np  # noqa: E402

from vsplit.core import duration_of, split_video  # noqa: E402


def make(path, seconds=70, fps=25, codec="libx264"):
    c = av.open(path, "w")
    try:
        st = c.add_stream(codec, rate=fps)
    except Exception:
        st = c.add_stream("mpeg4", rate=fps)
    st.width, st.height, st.pix_fmt = 1920, 1080, "yuv420p"
    rng = np.random.default_rng(0)
    for i in range(seconds * fps):
        img = np.full((1080, 1920, 3), 240, np.uint8)
        k = i // (fps * 7)                       # a new "slide" every 7 s
        img[100 + 40 * (k % 10):160 + 40 * (k % 10), 200:1600] = 30
        img[900:1000, (i * 7) % 1800:(i * 7) % 1800 + 100] = rng.integers(0, 255, 3)  # motion
        for p in st.encode(av.VideoFrame.from_ndarray(img, format="bgr24")):
            c.mux(p)
    for p in st.encode():
        c.mux(p)
    c.close()


def main():
    work = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="vsplit_")
    os.makedirs(work, exist_ok=True)
    bad = 0
    for name, codec in (("강의 영상.mp4", "libx264"), ("old lecture.avi", "mpeg4")):
        src = os.path.join(work, name)
        make(src, codec=codec)
        total = duration_of(src)
        for mode in ("share", "copy"):
            out_dir = os.path.join(work, "%s_%s" % (os.path.splitext(name)[0], mode))
            outs = split_video(src, 0.5, mode, max_mb=1.0, out_dir=out_dir)   # 30 s pieces
            durs = [duration_of(o) for o in outs]
            sizes = [os.path.getsize(o) / 2 ** 20 for o in outs]
            ok = len(outs) == 3 and abs(sum(durs) - total) < 3.0
            if mode == "share":
                ok = ok and max(sizes) <= 1.0
            print("%-16s %-5s %d pieces  %s  total %.1fs of %.1fs  max %.2f MB  %s" % (
                name, mode, len(outs), [round(d, 1) for d in durs], sum(durs), total, max(sizes),
                "ok" if ok else "FAIL"))
            bad += not ok
    print("vsplit tests %s" % ("passed" if not bad else "FAILED (%d)" % bad))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
