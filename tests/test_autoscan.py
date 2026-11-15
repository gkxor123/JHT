"""Tests for the --auto selection rules (no video libraries needed)."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from slidecap.autoscan import scan  # noqa: E402


def touch(root, rel):
    p = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "wb").close()
    return p


def main():
    with tempfile.TemporaryDirectory() as root:
        # course with a handout PDF -> skipped
        touch(root, "01_Chest/2026-01 강좌A/1강.mp4")
        touch(root, "01_Chest/2026-01 강좌A/강의록.pdf")
        # handout in a sub folder of the course -> skipped
        touch(root, "02_GI/2026-02 강좌B/영상/1강.mov")
        touch(root, "02_GI/2026-02 강좌B/자료/강의록.pptx")
        # no handout -> both videos
        new1 = touch(root, "03_GU/2026-11-15 새 강좌/1-1.mp4")
        new2 = touch(root, "03_GU/2026-11-15 새 강좌/sub/1-2.mp4")
        # SlideCap output alone does not count as a handout, and its video is done
        touch(root, "04_MSK/2026-03 강좌C/1강.mp4")
        touch(root, "04_MSK/2026-03 강좌C/1강.pptx")
        new3 = touch(root, "04_MSK/2026-03 강좌C/2강.mp4")
        # Office lock file is ignored
        touch(root, "05_Neuro/2026-04 강좌D/~$x.pptx")
        new4 = touch(root, "05_Neuro/2026-04 강좌D/1강.wmv")
        # video directly in a subspecialty folder: its own folder is the course
        new5 = touch(root, "06_H&N/loose.mp4")

        # program / notice PDF is not a handout
        touch(root, "07_INT/2026-05 강좌E/2026 연수강좌 프로그램.pdf")
        touch(root, "07_INT/2026-05 강좌E/안내장_ver. June.PDF")
        new6 = touch(root, "07_INT/2026-05 강좌E/1강.mp4")
        # handouts for some lectures only + marker: convert only lectures without notes
        touch(root, "08_Heart/2026-06 강좌F/slidecap_변환.txt")
        touch(root, "08_Heart/2026-06 강좌F/1. Basic MRI sequence_김채리.pdf")
        touch(root, "08_Heart/2026-06 강좌F/1. Basic MRI sequence.mp4")
        touch(root, "08_Heart/2026-06 강좌F/5. The Thick myocardium_part I_강세리.pdf")
        touch(root, "08_Heart/2026-06 강좌F/5. The Thick myocardium_part I.mp4")
        new7 = touch(root, "08_Heart/2026-06 강좌F/5. The Thick myocardium_part II.mp4")
        touch(root, "08_Heart/2026-06 강좌F/2-1.Functrional anatomy.pdf")
        touch(root, "08_Heart/2026-06 강좌F/2-1.Functional anatomy.mp4")
        touch(root, "08_Heart/2026-06 강좌F/Subaxial Cervical Spine Trauma.pdf")
        touch(root, "08_Heart/2026-06 강좌F/1-2.subaxial cervical spine trauma.mp4")
        new8 = touch(root, "08_Heart/2026-06 강좌F/1-1.upper cervical spine trauma.mp4")
        # one lecture per sub folder: video + any document there
        touch(root, "08_Heart/2026-06 강좌F/03 k/03. k-space in MRI_Sjin.pdf")
        touch(root, "08_Heart/2026-06 강좌F/03 k/20220607_mr physics 3.mp4")

        todo, skipped = scan(root)
        want = sorted(os.path.abspath(p) for p in (new1, new2, new3, new4, new5, new6, new7, new8))
        assert sorted(todo) == want, (todo, want)
        assert len(skipped) == 2, skipped
    # notes deleted from the course folder but known from the 01_Radiology list
    import json
    with tempfile.TemporaryDirectory() as root:
        v = touch(root, "05_Neuro/2023-09-10 강좌G/session 1.mp4")
        new9 = touch(root, "05_Neuro/2024-01-01 강좌H/session 1.mp4")
        lst = os.path.join(root, "seen.json")
        with open(lst, "w", encoding="utf-8") as fh:
            json.dump(["05_Neuro/2023-09-10 강좌G/강의록.pdf", "05_Neuro/2024-01-01 강좌H/2024 프로그램.pdf"], fh)
        todo, skipped = scan(root, handout_log=lst)
        assert todo == [os.path.abspath(new9)], todo
        assert len(skipped) == 1, skipped
    print("autoscan tests passed")

    real = os.environ.get("SLIDECAP_REAL_ROOT")
    if real:
        todo, skipped = scan(real)
        courses = sorted({os.path.relpath(os.path.dirname(v), real).split(os.sep)[1] for v in todo
                          if len(os.path.relpath(os.path.dirname(v), real).split(os.sep)) > 1})
        print("real: %d videos to convert in %d courses, %d courses skipped" % (len(todo), len(courses), len(skipped)))
        for c in courses:
            print("  ", c)


if __name__ == "__main__":
    main()
