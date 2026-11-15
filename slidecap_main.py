"""Entry point.

GUI (default):   SlideCap.exe [video or folder ...]
Automatic:       SlideCap.exe --auto FOLDER [FOLDER ...]
                 (finds every video below FOLDER that has no .pptx yet, asks
                 the slide area only for folders without a saved area, then
                 converts them all; used from a .bat file)
                 (dragging videos / a folder onto SlideCap.exe also works)
Command line:    SlideCap.exe --cli VIDEO_OR_FOLDER --roi X,Y,W,H
                     [--exclude X,Y,W,H ...] [--out DIR] [--sens normal|high|low]
                     [--images] [--png] [--keep-duplicates]
"""
import argparse
import os
import sys


def _rect(s):
    v = [int(float(x)) for x in s.split(",")]
    if len(v) != 4:
        raise argparse.ArgumentTypeError("X,Y,W,H 형식이어야 합니다")
    return tuple(v)


def cli(argv):
    from slidecap.engine import Settings, list_videos, process_video
    ap = argparse.ArgumentParser(prog="SlideCap --cli")
    ap.add_argument("path")
    ap.add_argument("--roi", type=_rect, required=True)
    ap.add_argument("--exclude", type=_rect, action="append", default=[])
    ap.add_argument("--out")
    ap.add_argument("--sens", default="normal", choices=["normal", "high", "low"])
    ap.add_argument("--images", action="store_true")
    ap.add_argument("--png", action="store_true")
    ap.add_argument("--keep-duplicates", action="store_true")
    ap.add_argument("--keep-build-steps", action="store_true")
    a = ap.parse_args(argv)
    s = Settings.preset(a.sens)
    s.dedupe = not a.keep_duplicates
    s.collapse_builds = not a.keep_build_steps
    s.image_format = "png" if a.png else "jpg"
    rc = 0
    for v in list_videos(a.path):
        def prog(f, n):
            sys.stdout.write("\r%s  %5.1f%%  %d slides" % (os.path.basename(v), f * 100, n)); sys.stdout.flush()
        try:
            out, n = process_video(v, a.roi, a.exclude, s, a.out, a.images, progress=prog)
            print("\n -> %s (%d slides)" % (out, n))
        except Exception as e:
            print("\nERROR %s: %s" % (v, e)); rc = 1
    return rc


def main():
    argv = sys.argv[1:]
    if sys.stdout is None or sys.stderr is None:
        # windowed exe started from a console: keep output in a log file
        log = open(os.path.join(os.path.expanduser("~"), "slidecap_cli.log"), "a", encoding="utf-8")
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log
    if argv and argv[0] == "--cli":
        sys.exit(cli(argv[1:]))
    if argv and argv[0] == "--selftest":
        # used by the build to verify the packaged exe works
        import av, cv2, numpy, pptx, PIL  # noqa: F401
        from slidecap import engine  # noqa: F401
        import tkinter  # noqa: F401
        print("selftest ok")
        sys.exit(0)
    from slidecap.gui import run
    if argv and argv[0] == "--auto":
        run(argv[1:], auto=True)
    else:
        run(argv)


if __name__ == "__main__":
    main()
