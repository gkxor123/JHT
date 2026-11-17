r"""pdf2ocr entry point.

GUI (default):  pdf2ocr.exe [PDF or folder ...]   (dragging onto the exe works too)
Command line:   pdf2ocr.exe --cli PATH [PATH ...] [--no-sub] [--new-file] [--force] [--lang ko]
                (log: %USERPROFILE%\pdf2ocr_cli.log when started without a console)
Check text:     pdf2ocr.exe --dump-text PDF OUT.txt
"""
import argparse
import os
import sys


def cli(argv):
    from pdf2ocr.core import WindowsOcr, list_pdfs, process_pdf
    ap = argparse.ArgumentParser(prog="pdf2ocr --cli")
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--no-sub", action="store_true", help="하위 폴더 제외")
    ap.add_argument("--new-file", action="store_true", help="이름_ocr.pdf로 저장")
    ap.add_argument("--force", action="store_true", help="글자 있는 페이지도 OCR")
    ap.add_argument("--lang")
    a = ap.parse_args(argv)
    files = list_pdfs(a.paths, not a.no_sub)
    print("PDF %d개" % len(files))
    ocr = WindowsOcr(a.lang)
    print("OCR language:", ocr.lang)
    rc = 0
    for f in files:
        try:
            r = process_pdf(f, ocr, new_file=a.new_file, force=a.force)
            print("%s\t%s\t%d/%d pages\t%d words" % (r.status, f, r.pages_ocr, r.pages, r.words))
        except Exception as e:
            print("ERROR\t%s\t%s" % (f, e))
            rc = 1
    ocr.close()
    return rc


def main():
    argv = sys.argv[1:]
    if sys.stdout is None or sys.stderr is None:
        log = open(os.path.join(os.path.expanduser("~"), "pdf2ocr_cli.log"), "a", encoding="utf-8")
        sys.stdout = sys.stderr = log
    if argv[:1] == ["--cli"]:
        sys.exit(cli(argv[1:]))
    if argv[:1] == ["--dump-text"] and len(argv) >= 3:
        from pdf2ocr.core import dump_text
        with open(argv[2], "w", encoding="utf-8") as f:
            f.write(dump_text(argv[1]))
        sys.exit(0)
    from pdf2ocr.gui import run
    run(argv)


if __name__ == "__main__":
    main()
