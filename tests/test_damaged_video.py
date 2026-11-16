"""A video with a damaged spot in the middle must still be read to the end
(the damaged part is skipped) instead of failing the whole lecture.

Run:  python tests/test_damaged_video.py
"""
import os
import sys
import tempfile

import av
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from slidecap.engine import _frames, _open  # noqa: E402

W, H, FPS, N = 320, 240, 10, 300


def make_video(path):
    out = av.open(path, "w")
    st = out.add_stream("mpeg4", rate=FPS)
    st.width, st.height, st.pix_fmt = W, H, "yuv420p"
    st.codec_context.gop_size = 10
    for i in range(N):
        img = np.full((H, W, 3), 240, np.uint8)
        k = i // 50                       # six "slides"
        img[40 + 20 * k:70 + 20 * k, 30:290] = (30 * k, 60, 200 - 30 * k)
        fr = av.VideoFrame.from_ndarray(img, format="bgr24")
        for p in st.encode(fr):
            out.mux(p)
    for p in st.encode():
        out.mux(p)
    out.close()


def damage(path):
    data = bytearray(open(path, "rb").read())
    rng = np.random.default_rng(1)
    a = int(len(data) * 0.45)
    data[a:a + 4000] = rng.integers(0, 256, 4000, dtype=np.uint8).tobytes()
    open(path, "wb").write(bytes(data))


def main():
    d = tempfile.mkdtemp()
    path = os.path.join(d, "damaged.ts")
    make_video(path)
    damage(path)

    plain_ok = True
    c, s = _open(path)
    try:
        for _ in c.decode(s):
            pass
    except av.error.FFmpegError as e:
        plain_ok = False
        print("plain decode stops:", e)
    finally:
        c.close()

    stats = {}
    c, s = _open(path)
    last, n = None, 0
    for f in _frames(c, s, stats):
        n += 1
        last = f.time
    c.close()
    print("plain decode ok:", plain_ok, "| robust frames:", n, "last t:", last,
          "bad packets:", stats.get("bad", 0))
    ok = last is not None and last > (N / FPS) * 0.9 and n > N * 0.7

    print("damaged video test %s" % ("passed" if ok else "FAILED"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
