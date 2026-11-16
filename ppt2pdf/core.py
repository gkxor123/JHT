"""PPT -> PDF conversion engines.

PowerPoint (COM): best fidelity. One PowerPoint process is reused for every
file (PowerPoint is single-instance, so files go one after another, but the
start-up cost is paid only once).

LibreOffice (soffice): used when PowerPoint is not installed. Several soffice
processes run in parallel, each with its own profile, and each converts a
batch of files per launch.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

EXTS = {".ppt", ".pptx", ".pptm", ".pps", ".ppsx", ".ppsm",
        ".pot", ".potx", ".potm", ".odp"}
ZIP_EXTS = {".pptx", ".pptm", ".ppsx", ".ppsm", ".potx", ".potm", ".odp"}

# status values reported to the progress callback
RUNNING, DONE, SKIPPED, FAILED = "running", "done", "skipped", "failed"


# ---------------------------------------------------------------- files
def is_ppt(path):
    name = os.path.basename(path)
    return (os.path.splitext(name)[1].lower() in EXTS
            and not name.startswith("~$"))  # Office lock files


def collect(paths):
    """Files and folders (searched recursively) -> sorted unique PPT files."""
    seen, out = set(), []
    for p in paths:
        p = os.path.abspath(p.strip().strip('"'))
        if os.path.isdir(p):
            found = []
            for root, dirs, files in os.walk(p):
                dirs.sort()
                found += [os.path.join(root, f) for f in sorted(files) if is_ppt(f)]
        elif os.path.isfile(p) and is_ppt(p):
            found = [p]
        else:
            found = []
        for f in found:
            k = os.path.normcase(f)
            if k not in seen:
                seen.add(k)
                out.append(f)
    return out


def plan(files, out_dir=None):
    """[(src, dst)] with unique destinations ("a.ppt" + "a.pptx" -> 2 PDFs)."""
    rank = [".pptx", ".ppt", ".pptm"]
    order = sorted(range(len(files)), key=lambda i: rank.index(os.path.splitext(files[i])[1].lower())
                   if os.path.splitext(files[i])[1].lower() in rank else len(rank))
    dsts, used = {}, set()
    for i in order:  # .pptx gets the plain name when names clash
        src = files[i]
        d = out_dir or os.path.dirname(src)
        stem, ext = os.path.splitext(os.path.basename(src))
        dst = os.path.join(d, stem + ".pdf")
        if os.path.normcase(dst) in used:
            dst = os.path.join(d, "%s (%s).pdf" % (stem, ext[1:].lower()))
        used.add(os.path.normcase(dst))
        dsts[i] = dst
    return [(files[i], dsts[i]) for i in range(len(files))]


def up_to_date(src, dst):
    try:
        return os.path.getsize(dst) > 0 and os.path.getmtime(dst) >= os.path.getmtime(src)
    except OSError:
        return False


def encrypted(src):
    """Password-protected .pptx is an OLE file instead of a zip. Opening it
    would pop up a password box and stall the batch, so it is skipped."""
    if os.path.splitext(src)[1].lower() not in ZIP_EXTS:
        return False
    try:
        with open(src, "rb") as f:
            return f.read(8) == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    except OSError:
        return False


# ---------------------------------------------------------------- engines
def powerpoint_installed():
    if sys.platform != "win32":
        return False
    try:
        import winreg
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"PowerPoint.Application\CLSID"))
        import win32com.client  # noqa: F401
        return True
    except Exception:
        return False


def find_soffice():
    for name in ("soffice", "soffice.exe", "libreoffice"):
        p = shutil.which(name)
        if p:
            return p
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
        p = os.path.join(base, "LibreOffice", "program", "soffice.exe")
        if os.path.isfile(p):
            return p
    return None


def available_engines():
    e = []
    if powerpoint_installed():
        e.append("powerpoint")
    if find_soffice():
        e.append("libreoffice")
    return e


def pick_engine(choice="auto"):
    have = available_engines()
    if choice != "auto":
        if choice not in have:
            raise RuntimeError({"powerpoint": "PowerPoint가 설치되어 있지 않습니다.",
                                "libreoffice": "LibreOffice를 찾을 수 없습니다."}[choice])
        return choice
    if not have:
        raise RuntimeError("PowerPoint 또는 LibreOffice가 필요합니다. 둘 다 찾지 못했습니다.")
    return have[0]


class PowerPoint:
    # COM errors that mean PowerPoint died or hung up: restart it and retry
    DEAD = {-2147023174, -2147417848, -2147023170, -2147417851, -2147418111}

    def __init__(self):
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        self._client = win32com.client
        try:
            win32com.client.GetActiveObject("PowerPoint.Application")
            self.was_running = True   # user's own PowerPoint: never quit it
        except Exception:
            self.was_running = False
        self.app = None

    def _start(self):
        # dynamic dispatch: no makepy cache to build or write
        self.app = self._client.dynamic.Dispatch("PowerPoint.Application")
        try:
            self.app.DisplayAlerts = 1  # ppAlertsNone
        except Exception:
            pass

    def convert(self, src, dst, quality="print"):
        for attempt in (0, 1):
            if self.app is None:
                self._start()
            try:
                return self._convert(src, dst, quality)
            except Exception as e:
                code = getattr(e, "hresult", None) or (e.args[0] if e.args else None)
                if attempt == 0 and code in self.DEAD:
                    self.app = None
                    continue
                raise

    def _convert(self, src, dst, quality):
        # Open(FileName, ReadOnly, Untitled, WithWindow)
        pres = self.app.Presentations.Open(os.path.normpath(src), -1, 0, 0)
        try:
            if quality == "screen":
                pres.ExportAsFixedFormat(os.path.normpath(dst), 2, 1)  # PDF, screen intent
            else:
                pres.SaveAs(os.path.normpath(dst), 32)  # ppSaveAsPDF
        finally:
            try:
                pres.Close()
            except Exception:
                pass

    def close(self):
        try:
            if self.app is not None and not self.was_running and self.app.Presentations.Count == 0:
                self.app.Quit()
        except Exception:
            pass
        self.app = None


def _lo_profile(i):
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".cache")
    p = os.path.join(base, "PPT2PDF", "lo_profile_%d" % i)
    os.makedirs(p, exist_ok=True)
    return "file:///" + p.replace("\\", "/").lstrip("/")


def _lo_batch(soffice, worker, srcs, tmp, quality, timeout):
    filt = "pdf"
    if quality == "screen":
        filt = 'pdf:impress_pdf_Export:{"ReduceImageResolution":{"type":"boolean","value":"true"},' \
               '"MaxImageResolution":{"type":"long","value":"150"},"Quality":{"type":"long","value":"80"}}'
    cmd = [soffice, "-env:UserInstallation=" + _lo_profile(worker), "--headless", "--invisible",
           "--norestore", "--nolockcheck", "--nodefault", "--nologo",
           "--convert-to", filt, "--outdir", tmp] + srcs
    kw = {}
    if sys.platform == "win32":
        kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=timeout, **kw)


def convert_libreoffice(jobs, report, cancel, quality="print", workers=None, batch=4):
    soffice = find_soffice()
    if workers is None:
        workers = max(1, min(os.cpu_count() or 2, 6))
    workers = max(1, min(workers, len(jobs)))
    # small batches so that all workers stay busy until the end
    batch = max(1, min(batch, -(-len(jobs) // workers)))
    pending = list(enumerate(jobs))
    lock = threading.Lock()

    def take():
        with lock:
            got, stems = [], set()
            rest = []
            for item in pending:
                stem = os.path.splitext(os.path.basename(item[1][0]))[0].lower()
                if len(got) < batch and stem not in stems:
                    got.append(item)
                    stems.add(stem)
                else:
                    rest.append(item)
            pending[:] = rest
            return got

    def work(w):
        while not cancel.is_set():
            got = take()
            if not got:
                return
            tmp = tempfile.mkdtemp(prefix="ppt2pdf_")
            try:
                for i, _ in got:
                    report(i, RUNNING, "")
                srcs = [s for _, (s, _) in got]
                try:
                    _lo_batch(soffice, w, srcs, tmp, quality, timeout=300 + 120 * len(srcs))
                except subprocess.TimeoutExpired:
                    pass
                for i, (src, dst) in got:
                    out = os.path.join(tmp, os.path.splitext(os.path.basename(src))[0] + ".pdf")
                    if os.path.isfile(out) and os.path.getsize(out) > 0:
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        if os.path.exists(dst):
                            os.remove(dst)
                        shutil.move(out, dst)
                        report(i, DONE, "")
                    else:
                        report(i, FAILED, "LibreOffice 변환 실패")
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

    threads = [threading.Thread(target=work, args=(w,), daemon=True) for w in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def convert_powerpoint(jobs, report, cancel, quality="print"):
    pp = PowerPoint()
    try:
        for i, (src, dst) in enumerate(jobs):
            if cancel.is_set():
                break
            report(i, RUNNING, "")
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                pp.convert(src, dst, quality)
                report(i, DONE, "")
            except Exception as e:
                report(i, FAILED, _err(e))
    finally:
        pp.close()


def _err(e):
    a = getattr(e, "excepinfo", None)
    if a and len(a) > 2 and a[2]:
        return str(a[2]).strip()
    return str(e) or e.__class__.__name__


def convert(jobs, report=None, cancel=None, engine="auto", quality="print",
            skip_existing=True, workers=None):
    """Convert [(src, dst)]. report(index, status, message) is called from
    worker threads. Returns {status: count}."""
    report = report or (lambda i, s, m: None)
    cancel = cancel or threading.Event()
    counts = {DONE: 0, SKIPPED: 0, FAILED: 0}
    lock = threading.Lock()

    def rep(i, s, m):
        if s in counts:
            with lock:
                counts[s] += 1
        report(i, s, m)

    todo = []
    for i, (src, dst) in enumerate(jobs):
        if skip_existing and up_to_date(src, dst):
            rep(i, SKIPPED, "이미 PDF 있음")
        elif encrypted(src):
            rep(i, FAILED, "암호가 걸린 파일")
        else:
            todo.append(i)
    if todo:
        name = pick_engine(engine)
        sub = [jobs[i] for i in todo]
        sub_rep = lambda k, s, m: rep(todo[k], s, m)  # noqa: E731
        if name == "powerpoint":
            convert_powerpoint(sub, sub_rep, cancel, quality)
        else:
            convert_libreoffice(sub, sub_rep, cancel, quality, workers)
    return counts


def fmt_time(sec):
    sec = int(round(sec))
    return "%d분 %d초" % divmod(sec, 60) if sec >= 60 else "%d초" % sec


if __name__ == "__main__":  # quick manual run: python core.py FILES...
    t = time.time()
    js = plan(collect(sys.argv[1:]))
    print(convert(js, lambda i, s, m: print(s, js[i][0], m)), fmt_time(time.time() - t))
