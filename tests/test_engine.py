"""End-to-end accuracy test on synthetic lecture videos.

Run:  python tests/test_engine.py            (all scenarios)
      python tests/test_engine.py basic      (one scenario)

For every capture the test finds which ground-truth slide it shows (by
pixel comparison), then checks that
  * every expected slide was captured exactly once,
  * nothing else was captured (allowance: <= 2 extra frames of the slow GIF,
    which changes only every 1.5 s and looks like manual picture swaps),
  * the captured image matches the ground-truth slide (no cursor, no
    half-finished transition, no stale content).
"""
import os
import sys
import tempfile

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import make_test_video as mtv  # noqa: E402
from slidecap.engine import Settings, detect_slides, write_pptx  # noqa: E402

SCENARIOS = {
    "basic": dict(scale=1.0, crf=30, cam_inside=False, exclude_cam=False),
    "hd_lowquality": dict(scale=1.5, crf=36, cam_inside=False, exclude_cam=False),
    "webcam_in_slide": dict(scale=1.0, crf=30, cam_inside=True, exclude_cam=False),
    "webcam_excluded": dict(scale=1.0, crf=30, cam_inside=True, exclude_cam=True),
    "keep_build_steps": dict(scale=1.0, crf=30, cam_inside=False, exclude_cam=False, collapse=False),
    # camera filming the screen: jitter, wobble, exposure flicker, noise
    "camera_shake": dict(scale=1.0, crf=30, cam_inside=False, exclude_cam=False, shake=3.0),
    "camera_shake_strong": dict(scale=1.5, crf=34, cam_inside=False, exclude_cam=False, shake=6.0),
}


def gray_small(img):
    g = cv2.cvtColor(cv2.resize(img, (mtv.RW, mtv.RH), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
    return cv2.GaussianBlur(g, (3, 3), 0)


def run(name, workdir):
    cfg = SCENARIOS[name]
    path = os.path.join(workdir, name + ".mp4")
    roi, cam, segs = mtv.make(path, cfg["scale"], cfg["crf"], cfg["cam_inside"], cfg.get("shake", 0.0))
    excludes = [cam] if cfg["exclude_cam"] else []
    st = Settings()
    st.collapse_builds = cfg.get("collapse", True)
    caps = detect_slides(path, roi, excludes, st)

    # mask of pixels to compare (exclude webcam inside slide, dynamic boxes)
    base_mask = np.ones((mtv.RH, mtv.RW), bool)
    if cfg["cam_inside"]:
        cx, cy, cw, ch = cam
        sc = cfg["scale"]
        x0, y0 = int(cx / sc) - mtv.RX - 4, int(cy / sc) - mtv.RY - 4
        base_mask[max(0, y0):y0 + int(ch / sc) + 8, max(0, x0):x0 + int(cw / sc) + 8] = False

    uniq = []        # distinct ground truth slides
    seg_uid = []
    for sg in segs:
        for i, u in enumerate(uniq):
            if u["slide"] is sg["slide"]:
                seg_uid.append(i)
                break
        else:
            seg_uid.append(len(uniq))
            uniq.append(dict(slide=sg["slide"], expect=sg["expect"], dynamic=sg["dynamic"],
                             start=sg["start"], g=gray_small(sg["slide"])))

    problems = []
    hits = [0] * len(uniq)
    rows = []
    for c in caps:
        g = gray_small(c.image)
        best, best_err, best_bad = None, 1e9, 1.0
        for i, u in enumerate(uniq):
            m = base_mask.copy()
            if u["dynamic"]:
                x, y, w, h = u["dynamic"]
                m[y - 4:y + h + 4, x - 4:x + w + 4] = False
            d = cv2.absdiff(g, u["g"])[m]
            err = float(d.mean())
            if err < best_err:
                best, best_err, best_bad = i, err, float((d > 60).mean())
        hits[best] += 1
        rows.append((c.time, best, best_err, best_bad))
        if best_err > 4.0 or best_bad > 0.0006:
            problems.append("capture at %.1fs looks wrong (slide %d, mean err %.2f, bad px %.3f%%)"
                            % (c.time, best, best_err, best_bad * 100))
    for u in uniq:
        e = u["expect"]
        if e == "build":
            e = not st.collapse_builds
        u["must"], u["may"] = (e is True), (e is True or e == "either")
    for i, u in enumerate(uniq):
        allowed = 3 if u["dynamic"] == mtv.SLOW_BOX else 1
        if u["must"] and hits[i] == 0:
            problems.append("MISSING slide %d (shown at %.0fs)" % (i, u["start"]))
        if hits[i] > allowed:
            problems.append("slide %d captured %d times" % (i, hits[i]))
        if not u["may"] and hits[i]:
            problems.append("slide %d should not be captured" % i)

    out = write_pptx(caps, os.path.join(workdir, name + ".pptx"))
    print("\n[%s] %d captures -> %s" % (name, len(caps), os.path.basename(out)))
    for t, b, e, bad in rows:
        print("   %6.1fs  slide %2d  err %.2f  bad %.3f%%" % (t, b, e, bad * 100))
    for p in problems:
        print("   PROBLEM:", p)
    return problems


def main():
    names = sys.argv[1:] or list(SCENARIOS)
    workdir = os.environ.get("SLIDECAP_TEST_DIR") or tempfile.mkdtemp(prefix="slidecap_")
    os.makedirs(workdir, exist_ok=True)
    total = []
    for n in names:
        total += ["%s: %s" % (n, p) for p in run(n, workdir)]
    print("\n%s (%d problems)" % ("FAILED" if total else "ALL PASSED", len(total)))
    sys.exit(1 if total else 0)


if __name__ == "__main__":
    main()
