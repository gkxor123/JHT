"""End-to-end test: builds PPT files (Korean names, sub folders, same names
with different extensions, a password-protected stand-in, an Office lock
file), converts them and checks every PDF and its page count.

    python ppt2pdf/tests/test_convert.py [--engine libreoffice] [--exe PATH]
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import core  # noqa: E402


def make_pptx(path, n):
    from pptx import Presentation
    from pptx.util import Inches
    p = Presentation()
    for k in range(n):
        s = p.slides.add_slide(p.slide_layouts[1])
        s.shapes.title.text = "슬라이드 %d - %s" % (k + 1, os.path.basename(path))
        s.placeholders[1].text = "한글 본문 테스트\nLine two"
        s.shapes.add_shape(1, Inches(1), Inches(5), Inches(2), Inches(1))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    p.save(path)


def pages(pdf):
    with open(pdf, "rb") as f:
        data = f.read()
    return len(re.findall(rb"/Type\s*/Page(?![s\w])", data))


def main():
    args = sys.argv[1:]
    engine = args[args.index("--engine") + 1] if "--engine" in args else "auto"
    exe = args[args.index("--exe") + 1] if "--exe" in args else None
    root = os.environ.get("PPT2PDF_TEST_DIR") or tempfile.mkdtemp(prefix="ppt2pdf_test_")
    shutil.rmtree(root, ignore_errors=True)
    want = {
        os.path.join(root, "흉부 영상의학 1강.pptx"): 3,
        os.path.join(root, "a.pptx"): 2,
        os.path.join(root, "하위 폴더", "복부 CT (2026).pptx"): 5,
        os.path.join(root, "하위 폴더", "깊은 폴더", "MRI.pptx"): 1,
    }
    for f, n in want.items():
        make_pptx(f, n)
    shutil.copy(os.path.join(root, "a.pptx"), os.path.join(root, "a.ppsx"))  # same name, other ext
    want[os.path.join(root, "a.ppsx")] = 2
    with open(os.path.join(root, "~$흉부 영상의학 1강.pptx"), "wb") as f:  # lock file: ignored
        f.write(b"x" * 100)
    locked = os.path.join(root, "암호.pptx")  # encrypted pptx is an OLE file
    with open(locked, "wb") as f:
        f.write(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 1000)

    files = core.collect([root])
    assert len(files) == len(want) + 1, files
    jobs = core.plan(files)
    dst = dict(jobs)
    assert dst[os.path.join(root, "a.ppsx")].endswith("a (ppsx).pdf"), dst

    t = time.time()
    if exe:
        r = subprocess.run([exe, "--cli", root, "--engine", engine])
        print("exe exit", r.returncode)
        assert r.returncode == 1, "exe should report the one failed (encrypted) file"
        c = None
    else:
        log = []
        c = core.convert(jobs, lambda i, s, m: log.append((s, jobs[i][0], m)), engine=engine)
        for x in log:
            print(x)
        print(c)
        assert c == {core.DONE: len(want), core.SKIPPED: 0, core.FAILED: 1}, c
    print("convert time %.1fs" % (time.time() - t))
    bad = []
    for src, n in want.items():
        pdf = dst[src]
        got = pages(pdf) if os.path.isfile(pdf) else -1
        print("%-50s %d pages (want %d)" % (os.path.relpath(pdf, root), got, n))
        if got != n:
            bad.append(pdf)
    assert not bad, bad
    assert not os.path.exists(dst[locked])

    if not exe:  # second run: everything is up to date
        c = core.convert(jobs, engine=engine)
        assert c == {core.DONE: 0, core.SKIPPED: len(want), core.FAILED: 1}, c
    print("OK")


if __name__ == "__main__":
    main()
