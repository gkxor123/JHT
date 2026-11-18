"""Regression test on real lecture clips in samples/ (uploaded by the user).

samples/answers.txt holds the real slide count per clip, e.g.
    1_3. Orbit_01: 8장
A clip passes when SlideCap (auto region, auto camera detection) gives
between ``answer * LOW`` and ``answer * HIGH + SLACK`` slides: a few extra
near-duplicates are acceptable to the user, dozens are not.

Run:  python tests/test_real_samples.py
"""
import glob
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from slidecap.autoregion import auto_region_for_video  # noqa: E402
from slidecap.engine import detect_slides  # noqa: E402

LOW, HIGH, SLACK = 0.75, 1.3, 2


def answers():
    out = {}
    p = os.path.join(ROOT, "samples", "answers.txt")
    if not os.path.isfile(p):
        return out
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r"(.+?)\s*:\s*(\d+)", line)
            if m:
                key = m.group(1).strip()
                if os.path.splitext(key)[1].lower() in (".mp4", ".mov", ".avi", ".mkv"):
                    key = os.path.splitext(key)[0]   # "1_3. Orbit_01" has a dot but no extension
                out[key] = int(m.group(2))
    return out


def find_answer(ans, clip):
    base = os.path.splitext(os.path.basename(clip))[0]
    for k, v in sorted(ans.items(), key=lambda kv: -len(kv[0])):   # longest name first
        if base == k or base.startswith(k):
            return v
    return None


def main():
    ans = answers()
    clips = sorted(glob.glob(os.path.join(ROOT, "samples", "*.mp4")))
    bad = 0
    tested = 0
    for clip in clips:
        want = find_answer(ans, clip)
        if want is None:
            print("%-45s  (no answer in answers.txt, skipped)" % os.path.basename(clip))
            continue
        t = time.time()
        roi, sure, why, _ = auto_region_for_video(clip)
        caps = detect_slides(clip, roi)
        n = len(caps)
        lo, hi = int(want * LOW), int(want * HIGH) + SLACK
        ok = lo <= n <= hi
        tested += 1
        bad += not ok
        print("%-45s  %3d slides (answer %d, allowed %d-%d)  %.0fs  %s" % (
            os.path.basename(clip), n, want, lo, hi, time.time() - t, "ok" if ok else "FAIL"))
    print("real samples: %d tested, %s" % (tested, "passed" if not bad else "FAILED (%d)" % bad))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
