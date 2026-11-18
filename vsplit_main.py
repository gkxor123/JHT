r"""VSplit entry point.

GUI (default):  VSplit.exe [video or folder ...]   (dragging onto the exe works too)
Command line:   VSplit.exe --cli PATH [PATH ...] [--minutes 5] [--copy] [--max-mb 24] [--out DIR]
                (log: %USERPROFILE%\vsplit_cli.log when started without a console)
"""
import argparse
import os
import sys


def cli(argv):
    from vsplit.core import list_videos, split_video
    ap = argparse.ArgumentParser(prog="VSplit --cli")
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--minutes", type=float, default=5.0)
    ap.add_argument("--copy", action="store_true", help="다시 인코딩하지 않고 자르기")
    ap.add_argument("--max-mb", type=float, default=24.0)
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    rc = 0
    for v in list_videos(a.paths):
        try:
            outs = split_video(v, a.minutes, "copy" if a.copy else "share", a.max_mb, out_dir=a.out)
            for o in outs:
                print("%s\t%.1f MB" % (o, os.path.getsize(o) / 2 ** 20))
        except Exception as e:
            print("ERROR\t%s\t%s" % (v, e))
            rc = 1
    return rc


def main():
    argv = sys.argv[1:]
    if sys.stdout is None or sys.stderr is None:
        log = open(os.path.join(os.path.expanduser("~"), "vsplit_cli.log"), "a", encoding="utf-8")
        sys.stdout = sys.stderr = log
    if argv[:1] == ["--cli"]:
        sys.exit(cli(argv[1:]))
    from vsplit.gui import run
    run(argv)


if __name__ == "__main__":
    main()
