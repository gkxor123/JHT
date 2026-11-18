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
    # projector screen filmed from the side: trapezoid, straightened by the engine
    "camera_tilted": dict(scale=1.0, crf=30, cam_inside=False, exclude_cam=False, shake=2.0, tilt=True),
}


def gray_small(img):
    g = cv2.cvtColor(cv2.resize(img, (mtv.RW, mtv.RH), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
    return cv2.GaussianBlur(g, (3, 3), 0)


def match_camera(g, ref, mask):
    """Undo camera shift and exposure of capture ``g`` relative to ``ref``
    (fitted on the pixels of ``mask``)."""
    (dx, dy), _ = cv2.phaseCorrelate(ref.astype(np.float32), g.astype(np.float32))
    if abs(dx) < 20 and abs(dy) < 20:
        warp = np.float32([[1, 0, dx], [0, 1, dy]])
        try:   # also undo a slight zoom / rotation / keystone left over
            _, warp = cv2.findTransformECC(ref, g, warp, cv2.MOTION_AFFINE,
                                           (cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS, 60, 1e-5),
                                           mask.astype(np.uint8), 3)
        except cv2.error:
            pass
        g = cv2.warpAffine(g, warp, (g.shape[1], g.shape[0]), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                           borderMode=cv2.BORDER_REPLICATE)
    a, b = g.astype(np.float32), ref.astype(np.float32)
    gain, off = np.polyfit(a[mask], b[mask], 1)
    return np.clip(a * gain + off, 0, 255).astype(np.uint8)


def run(name, workdir):
    cfg = SCENARIOS[name]
    path = os.path.join(workdir, name + ".mp4")
    roi, cam, segs = mtv.make(path, cfg["scale"], cfg["crf"], cfg["cam_inside"], cfg.get("shake", 0.0),
                              cfg.get("tilt", False))
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

    if cfg.get("tilt"):
        # the straightened screen keeps a small margin of the room around it
        # (so the slide is never cut): leave that border out of the scoring
        by, bx = int(mtv.RH * 0.05), int(mtv.RW * 0.05)
        base_mask[:by] = False; base_mask[-by:] = False
        base_mask[:, :bx] = False; base_mask[:, -bx:] = False

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
            if cfg.get("shake"):
                # undo shift / exposure; keep whichever comparison is better
                # (the alignment itself can be fooled by big plain areas)
                d2 = cv2.absdiff(match_camera(g, u["g"], m), u["g"])[m]
                if float(d2.mean()) < float(d.mean()):
                    d = d2
            err = float(d.mean())
            if err < best_err:
                best, best_err, best_bad = i, err, float((d > 60).mean())
        hits[best] += 1
        rows.append((c.time, best, best_err, best_bad))
        lim_err, lim_bad = (4.0, 0.0006) if not cfg.get("shake") else (6.0, 0.01)
        if best_err > lim_err or best_bad > lim_bad:
            problems.append("capture at %.1fs looks wrong (slide %d, mean err %.2f, bad px %.3f%%)"
                            % (c.time, best, best_err, best_bad * 100))
    # a filmed screen may miss a change of one or two letters (user's
    # choice: fewer duplicates matter more).  S2d adds "OK", S10/S11 change
    # one letter: either neighbour may stand for the pair.
    pairs = []
    if cfg.get("shake"):
        lenient = {id(mtv.S2c): id(mtv.S2d), id(mtv.S9): id(mtv.S10),
                   id(mtv.S10): id(mtv.S9), id(mtv.S11): id(mtv.S10)}
        by_id = {id(u["slide"]): i for i, u in enumerate(uniq)}
        for a_, b_ in lenient.items():
            if a_ in by_id and b_ in by_id:
                pairs.append((by_id[a_], by_id[b_]))
                uniq[by_id[a_]]["expect"] = "either"
        if id(mtv.S2d) in by_id:
            uniq[by_id[id(mtv.S2d)]]["expect"] = "either"
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

    if cfg.get("shake"):
        i2c = next(i for i, u in enumerate(uniq) if u["slide"] is mtv.S2c)
        i2d = next(i for i, u in enumerate(uniq) if u["slide"] is mtv.S2d)
        if hits[i2c] + hits[i2d] == 0:
            problems.append("MISSING bullet slide (neither last nor second-to-last step)")
        if hits[i2c] + hits[i2d] > 2:
            problems.append("bullet slide captured %d times" % (hits[i2c] + hits[i2d]))
        # the three quick slides differ by one or two letters only: any of
        # them may stand for another, but at least one must be there and
        # none may be saved more often than the three are shown
        quick = [i for i, u in enumerate(uniq) if any(u["slide"] is q for q in (mtv.S9, mtv.S10, mtv.S11))]
        nq = sum(hits[i] for i in quick)
        problems = [p for p in problems if not any(("slide %d " % i) in p for i in quick)]
        if nq == 0:
            problems.append("MISSING quick slides (none of the three)")
        if nq > 3:
            problems.append("quick slides captured %d times" % nq)
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
