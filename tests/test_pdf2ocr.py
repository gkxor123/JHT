"""pdf2ocr: an image-only PDF becomes searchable, looks the same, pages that
already have text are left alone and a second run changes nothing.

Part 1 uses a fake OCR (known word boxes) so it runs everywhere.
Part 2 uses the real Windows OCR when this PC has an OCR language.

Run:  python tests/test_pdf2ocr.py [OUTDIR]
"""
import os
import shutil
import sys
import tempfile

import pymupdf as fitz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pdf2ocr.core import (WindowsOcr, list_pdfs, merge_lines, ocr_languages,  # noqa: E402
                          page_has_text, process_pdf)

PW, PH = 960, 540
LINES = [("Myocarditis and late gadolinium enhancement", 60, 120, 34),
         ("심근병증 영상 소견 정리", 60, 220, 40),
         ("Cardiac MRI protocol for cardiomyopathy", 60, 320, 30),
         ("Pulmonary embolism: filling defect in segmental arteries", 60, 400, 20)]
FONT = fitz.Font("cjk")


def make_pdf(path):
    src = fitz.open()
    pg = src.new_page(width=PW, height=PH)
    tw = fitz.TextWriter(pg.rect)
    for text, x, y, fs in LINES:
        tw.append((x, y), text, font=FONT, fontsize=fs)
    pg.draw_rect(fitz.Rect(600, 430, 900, 520), color=(0.2, 0.2, 0.6), fill=(0.8, 0.85, 1))
    tw.write_text(pg)
    pix = pg.get_pixmap(matrix=fitz.Matrix(2, 2))
    out = fitz.open()
    p1 = out.new_page(width=PW, height=PH)
    p1.insert_image(p1.rect, pixmap=pix)                   # picture only
    p2 = out.new_page(width=PW, height=PH)
    p2.insert_text((60, 100), "This page already has selectable text content.", fontsize=20)
    out.save(path)


class FakeOcr:
    """Returns the true word boxes, in rendered-image pixels."""
    max_dim = 10000

    def recognize(self, gray, w, h):
        z = w / PW
        lines = []
        for text, x, y, fs in LINES:
            ws, cx = [], x
            for word in text.split(" "):
                wl = FONT.text_length(word, fontsize=fs)
                ws.append((word, cx * z, (y - fs * 0.8) * z, wl * z, fs * 0.95 * z))
                cx += wl + FONT.text_length(" ", fontsize=fs)
            lines.append(ws)
        return lines


def render(path, i=0):
    with fitz.open(path) as d:
        return d[i].get_pixmap(matrix=fitz.Matrix(1, 1)).samples


def main():
    base = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp()
    d = os.path.join(base, "강의록 OCR 테스트", "하위 폴더")
    os.makedirs(d, exist_ok=True)
    src = os.path.join(d, "심장 강의록.pdf")
    make_pdf(src)
    ok = True

    def check(cond, msg):
        nonlocal ok
        print(("ok    " if cond else "FAIL  ") + msg)
        ok = ok and bool(cond)

    top = os.path.join(base, "강의록 OCR 테스트")
    check(list_pdfs([top], True) == [src], "folder + subfolders finds the PDF")
    check(list_pdfs([top], False) == [], "without subfolders finds nothing at top level")

    with fitz.open(src) as doc:
        check(not page_has_text(doc[0]) and page_has_text(doc[1]), "page 1 picture only, page 2 has text")
    size0 = os.path.getsize(src)
    before = [render(src, 0), render(src, 1)]

    # ---- fake OCR, overwrite mode ----
    f1 = os.path.join(d, "fake.pdf")
    shutil.copy(src, f1)
    r = process_pdf(f1, FakeOcr())
    check(r.status == "done" and r.pages_ocr == 1, "only the picture page is OCRed (%s, %d)" % (r.status, r.pages_ocr))
    with fitz.open(f1) as doc:
        t = doc[0].get_text("text")
        check("Myocarditis" in t and "심근병증" in t and "gadolinium" in t, "text layer has English and Korean")
        hits = doc[0].search_for("late gadolinium")
        x_exp = 60 + FONT.text_length("Myocarditis and ", fontsize=34)
        check(hits and abs(hits[0].x0 - x_exp) < 12 and abs(hits[0].y1 - 120) < 20,
              "Ctrl+F phrase found at the right place %s (expected x %.0f)" % (hits[:1], x_exp))
        check(len(doc[0].search_for("심근병증")) == 1, "Korean word found")
        check(doc[1].get_text("text").count("already") == 1, "page with text untouched")
    check(render(f1, 0) == before[0] and render(f1, 1) == before[1], "pages look exactly the same")
    grow = os.path.getsize(f1) - size0
    print("      size %d -> %d bytes (+%d)" % (size0, os.path.getsize(f1), grow))
    check(grow < 1500000, "font embedding stays small")
    r2 = process_pdf(f1, FakeOcr())
    check(r2.status == "skip", "second run skips the file (%s)" % r2.status)

    # ---- new-file mode ----
    f2 = os.path.join(d, "newfile.pdf")
    shutil.copy(src, f2)
    r = process_pdf(f2, FakeOcr(), new_file=True)
    check(r.status == "done" and r.out.endswith("newfile_ocr.pdf") and os.path.getsize(f2) == size0,
          "new-file mode keeps the original")
    check(process_pdf(f2, FakeOcr(), new_file=True).status == "exists", "existing _ocr file is not overwritten")

    # ---- English/Korean merge ----
    en = [[("Cardiac", 10, 10, 80, 20), ("MRI", 95, 10, 40, 20)],
          [("A@#", 10, 50, 120, 22)]]                              # Hangul read by the English engine
    ko = [[("Cardiac", 10, 10, 80, 20), ("얜RI", 95, 10, 40, 20)],
          [("심근병증", 10, 50, 120, 22)]]
    m = merge_lines(en, ko)
    check([[w[0] for w in l] for l in m] == [["Cardiac", "MRI"], ["심근병증"]],
          "merge keeps English lines and takes Korean lines (%s)" % m)

    # ---- real Windows OCR ----
    langs = ocr_languages()
    print("Windows OCR languages:", langs)
    if langs:
        f3 = os.path.join(d, "real.pdf")
        shutil.copy(src, f3)
        ocr = WindowsOcr()
        r = process_pdf(f3, ocr)
        ocr.close()
        with fitz.open(f3) as doc:
            t = doc[0].get_text("text")
        print("      OCR (%s) text: %r" % (ocr.lang, " ".join(t.split())[:300]))
        low = t.lower()
        words = ("myocarditis", "gadolinium", "enhancement", "cardiac", "protocol", "cardiomyopathy",
                 "pulmonary", "embolism", "filling", "defect", "segmental", "arteries")
        found = [w for w in words if w in low]
        if ocr.english:
            check(len(found) == len(words), "English OCR reads every English word (%d/%d, missing %s)" % (
                len(found), len(words), [w for w in words if w not in found]))
        else:
            check(len(found) >= 6, "real OCR reads the English words (%s)" % found)
        if "ko" in ocr.lang.lower():
            check("심근병증" in t.replace(" ", ""), "real OCR reads Korean")
        check(render(f3, 0) == before[0], "real OCR page looks the same")
    else:
        print("      (no OCR language on this PC: real OCR part skipped)")

    print("pdf2ocr test %s" % ("passed" if ok else "FAILED"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
