"""Generate a synthetic lecture video with a known list of expected slides.

The slide area is ROI=(40, 60, 960, 540) at scale 1 (1280x720).  Every tricky
case is included: moving and parked mouse cursor, laser pointer, bullet build
animations (one of them a two-letter word), fade transitions, a fast GIF, a
slow GIF, an embedded video, an in-slide picture swap, a small arrow
appearing, returning to an earlier slide, and quickly flipped slides.

``make(path, scale, crf, cam_inside)`` returns (roi, cam_rect, segments) where
each segment is a dict with start, end, slide (ground truth image), expect
(True if it must be captured), dynamic (rect of moving content, if any).
"""
import sys

import av
import cv2
import numpy as np

FPS = 30
RX, RY, RW, RH = 40, 60, 960, 540


def base(title, color=(250, 250, 250)):
    s = np.full((RH, RW, 3), color, np.uint8)
    cv2.rectangle(s, (0, 0), (RW, 70), (120, 60, 20), -1)
    cv2.putText(s, title, (30, 50), cv2.FONT_HERSHEY_SIMPLEX, 1.4, (255, 255, 255), 3, cv2.LINE_AA)
    return s


def bullets(s, lines, start=0):
    for i, ln in enumerate(lines):
        yy = 140 + (start + i) * 60
        cv2.circle(s, (50, yy - 10), 6, (40, 40, 40), -1)
        cv2.putText(s, ln, (70, yy), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (30, 30, 30), 2, cv2.LINE_AA)
    return s


def picture(s, seed, box=(560, 120, 360, 300)):
    x, y, w, h = box
    r = np.random.default_rng(seed)
    img = np.zeros((h, w, 3), np.uint8)
    for _ in range(12):
        c = tuple(int(v) for v in r.integers(0, 255, 3))
        cv2.circle(img, (int(r.integers(0, w)), int(r.integers(0, h))), int(r.integers(15, 80)), c, -1)
    s[y:y + h, x:x + w] = img
    return s


S1 = base("Lecture: Liver Imaging")
cv2.putText(S1, "Department of Radiology", (200, 300), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (60, 60, 60), 2, cv2.LINE_AA)
L = ["Hepatocellular carcinoma", "Arterial phase hyperenhancement", "Washout and capsule", "OK"]
S2a = bullets(base("LI-RADS major features"), L[:1])
S2b = bullets(S2a.copy(), L[1:2], 1)
S2c = bullets(S2b.copy(), L[2:3], 2)
S2d = bullets(S2c.copy(), L[3:4], 3)
S3 = bullets(base("Dynamic contrast (GIF)"), ["Animated figure ->"])
S4a = picture(bullets(base("Case 1"), ["Arterial phase"]), 1)
S4b = picture(S4a.copy(), 2)
S5 = bullets(base("Slow GIF"), ["changes every 1.5 s"])
S6 = base("Embedded video")
S7 = bullets(base("After the video"), ["Summary point"])
S8 = bullets(base("Conclusion"), ["Thank you"])
S9 = bullets(base("Quick 1"), ["fast flip a"])
S10 = bullets(base("Quick 2"), ["fast flip b"])
S11 = bullets(base("Quick 3"), ["fast flip c"])
S12a = picture(bullets(base("Case 2: arrow"), ["Look at the lesion"]), 7)
S12b = S12a.copy()
cv2.arrowedLine(S12b, (620, 400), (680, 360), (0, 255, 255), 3, tipLength=0.3)

GIF_BOX = (560, 120, 360, 300)
SLOW_BOX = (560, 160, 300, 260)
VID_BOX = (50, 100, 860, 380)


def fast_gif(s, t):
    s = s.copy()
    x, y = 560, 120
    s[y:y + 300, x:x + 360] = (30, 30, 30)
    cx = int(x + 180 + 140 * np.sin(t * 3)); cy = int(y + 150 + 110 * np.cos(t * 2.3))
    cv2.circle(s, (cx, cy), 40, (0, 200, 255), -1)
    return s


def slow_gif(s, t):
    s = s.copy()
    k = int(t / 1.5) % 4
    picture(s, 100 + k, SLOW_BOX)
    return s


_vrng = np.random.default_rng(5)


def emb_video(s, t):
    s = s.copy()
    v = _vrng.integers(0, 255, (VID_BOX[3], VID_BOX[2], 3), dtype=np.uint8)
    v = cv2.GaussianBlur(v, (15, 15), 0)
    x, y, w, h = VID_BOX
    s[y:y + h, x:x + w] = v
    return s


# (duration, slide, dynamic-func, dynamic box, expected-capture?)
# "build" = intermediate step of a build-up animation: captured only when
# collapse_builds is off.  "either" = acceptable both ways.
TIMELINE = [
    (6, S1, None, None, True),
    (4, S2a, None, None, "build"),
    (4, S2b, None, None, "build"),
    (4, S2c, None, None, "build"),
    (4, S2d, None, None, True),          # two-letter bullet "OK"
    (8, S3, fast_gif, GIF_BOX, True),
    (5, S4a, None, None, True),
    (5, S4b, None, None, True),          # picture swap
    (10, S5, slow_gif, SLOW_BOX, True),
    (8, S6, emb_video, VID_BOX, True),   # slide captured once, video ignored
    (5, S7, None, None, True),
    (4, S2d, None, None, False),         # back to an earlier slide -> duplicate
    (5, S8, None, None, True),           # cursor hidden, then reappears still
    (3, S9, None, None, True),
    (3, S10, None, None, True),          # one character differs
    (3, S11, None, None, True),
    (10, S12a, None, None, "either"),    # slow cursor parks; arrow added later
    (5, S12b, None, None, True),         # small arrow appears
]


def draw_cursor(img, x, y, sc):
    pts = np.array([[0, 0], [0, 22], [6, 17], [11, 26], [14, 24], [10, 16], [17, 16]], np.float32) * sc
    pts = (pts + [x, y]).astype(np.int32)
    cv2.fillPoly(img, [pts], (255, 255, 255))
    cv2.polylines(img, [pts], True, (0, 0, 0), 1)


def cursor_pos(t):
    """Cursor position, or None while hidden (PowerPoint hides an idle one)."""
    if 67 <= t < 69:
        return None
    if 69 <= t < 72:   # reappears with a short movement, then stays still
        k = min(1.0, (t - 69) / 0.6)
        return RX + 420 + 100 * k, RY + 280 + 50 * k
    cx = RX + RW / 2 + 380 * np.sin(t * 0.7); cy = RY + RH / 2 + 220 * np.sin(t * 1.1)
    if 81 <= t < 91:
        # slow drift, then parked from 84 to 89, then leaves
        k = min(1.0, (t - 81) / 3.0)
        cx = RX + 100 + 200 * k; cy = RY + 120 + 180 * k
        if t >= 89:
            cx += (t - 89) * 200; cy += (t - 89) * 60
    return cx, cy


def make(out, scale=1.0, crf=30, cam_inside=False, shake=0.0):
    """``shake`` > 0 simulates a hand-held / tripod camera filming the
    screen: the whole picture jitters by up to ``shake`` pixels (with an
    occasional bump twice as large), slight zoom/rotation wobble, the
    exposure drifts and flickers, and there is sensor noise."""
    W, H = int(1280 * scale) // 2 * 2, int(720 * scale) // 2 * 2
    sc = scale
    roi = tuple(int(round(v * sc)) for v in (RX, RY, RW, RH))
    cam = (int(1020 * sc), int(80 * sc), int(230 * sc), int(200 * sc))
    if cam_inside:  # webcam overlay in the slide's bottom-right corner
        cam = (int((RX + RW - 240) * sc), int((RY + RH - 200) * sc), int(230 * sc), int(190 * sc))
    container = av.open(out, "w")
    try:
        st = container.add_stream("libx264", rate=FPS)
        st.options = {"crf": str(crf), "preset": "veryfast"}
    except Exception:  # FFmpeg build without x264: lossy MPEG-4 instead
        st = container.add_stream("mpeg4", rate=FPS)
        st.options = {"qscale": str(max(2, min(31, crf // 4)))}
    st.width, st.height, st.pix_fmt = W, H, "yuv420p"
    segments = []
    t0 = 0.0
    prev_slide = None
    fidx = 0
    srng = np.random.default_rng(11)
    jx = jy = 0.0
    for dur, slide, dynf, dbox, exp in TIMELINE:
        segments.append(dict(start=t0, end=t0 + dur, slide=slide, expect=exp, dynamic=dbox))
        n = int(dur * FPS)
        for i in range(n):
            t = fidx / FPS
            lt = i / FPS
            sl = dynf(slide, t) if dynf else slide
            if prev_slide is not None and lt < 0.5:     # 0.5 s fade
                a = lt / 0.5
                sl = cv2.addWeighted(sl, a, prev_slide, 1 - a, 0)
            frame = np.full((H, W, 3), (45, 45, 45), np.uint8)
            x, y, w, h = roi
            frame[y:y + h, x:x + w] = cv2.resize(sl, (w, h), interpolation=cv2.INTER_AREA)
            cx_, cy_, cw_, ch_ = cam
            camimg = np.full((ch_, cw_, 3), (90, 120, 150), np.uint8)
            cv2.circle(camimg, (cw_ // 2 + int(20 * sc * np.sin(t * 2)), ch_ // 2 + int(8 * sc * np.cos(t * 3))),
                       int(50 * sc), (180, 200, 230), -1)
            frame[cy_:cy_ + ch_, cx_:cx_ + cw_] = camimg
            cv2.putText(frame, "%.1f" % t, (int(1050 * sc), int(400 * sc)), cv2.FONT_HERSHEY_SIMPLEX, sc, (255, 255, 255), 2)
            pos = cursor_pos(t)
            if pos is not None:
                draw_cursor(frame, int(pos[0] * sc), int(pos[1] * sc), sc)
            if int(t) % 7 < 3 and t < 81:   # laser pointer
                lx = RX + 300 + 200 * np.cos(t * 1.7); ly = RY + 300 + 120 * np.sin(t * 2.1)
                cv2.circle(frame, (int(lx * sc), int(ly * sc)), int(7 * sc), (0, 0, 255), -1)
            if shake > 0:
                # smooth random walk pulled back to the centre, plus bumps
                jx = 0.85 * jx + srng.normal(0, shake * 0.35)
                jy = 0.85 * jy + srng.normal(0, shake * 0.35)
                bump = srng.random() < 0.01
                dx = float(np.clip(jx, -shake, shake)) * (2 if bump else 1)
                dy = float(np.clip(jy, -shake, shake)) * (2 if bump else 1)
                ang = 0.15 * np.sin(t * 0.9) + srng.normal(0, 0.03)
                zm = 1.0 + 0.002 * np.sin(t * 0.4)
                M = cv2.getRotationMatrix2D((W / 2.0, H / 2.0), ang, zm)
                M[0, 2] += dx * sc; M[1, 2] += dy * sc
                frame = cv2.warpAffine(frame, M, (W, H), flags=cv2.INTER_LINEAR,
                                       borderMode=cv2.BORDER_REPLICATE)
                gain = 1.0 + 0.06 * np.sin(t * 0.5) + srng.normal(0, 0.01)
                off = 8 * np.sin(t * 0.23) + srng.normal(0, 1.5)
                noise = srng.normal(0, 3.0, frame.shape)
                frame = np.clip(frame.astype(np.float32) * gain + off + noise, 0, 255).astype(np.uint8)
            vf = av.VideoFrame.from_ndarray(frame, format="bgr24")
            for p in st.encode(vf):
                container.mux(p)
            fidx += 1
        prev_slide = slide if dynf is None else dynf(slide, fidx / FPS)
        t0 += dur
    for p in st.encode():
        container.mux(p)
    container.close()
    return roi, cam, segments


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "test_lecture.mp4"
    roi, cam, segs = make(out)
    print("roi", roi, "expected", [s["start"] for s in segs if s["expect"]])
