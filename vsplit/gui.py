"""VSplit window: add videos / folders, cut them into pieces of N minutes."""
from __future__ import annotations

import os
import queue
import sys
import threading
import time
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .core import Cancelled, list_videos, split_video

APP_TITLE = "VSplit - 영상 자르기 %s" % __version__

try:  # optional drag & drop support
    from tkinterdnd2 import DND_FILES, TkinterDnD  # type: ignore
    _BaseTk = TkinterDnD.Tk
except Exception:  # pragma: no cover
    DND_FILES = None
    _BaseTk = tk.Tk


def _set_dpi_awareness():
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass


def write_error_log(text):
    try:
        p = os.path.join(os.path.expanduser("~"), "vsplit_error.log")
        with open(p, "a", encoding="utf-8") as f:
            f.write(time.strftime("\n==== %Y-%m-%d %H:%M:%S ====\n") + text)
    except Exception:
        pass


class App(_BaseTk):
    def __init__(self, paths=()):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("820x600")
        self.minsize(680, 480)
        self.files = []
        self.q = queue.Queue()
        self.cancel = threading.Event()
        self.worker = None
        self._build()
        self.add_paths(paths)
        self.after(100, self._poll)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build(self):
        top = ttk.Frame(self, padding=(8, 8, 8, 0))
        top.pack(fill="x")
        ttk.Label(top, text="긴 영상을 몇 분짜리 조각으로 자릅니다.", font=("", 11, "bold")).pack(anchor="w")
        hint = "영상 파일이나 폴더를 추가하세요"
        hint += " (이 창이나 exe 아이콘에 끌어다 놓아도 됩니다)." if DND_FILES else " (exe 아이콘에 끌어다 놓아도 됩니다)."
        ttk.Label(top, text=hint, foreground="#555").pack(anchor="w")

        bar = ttk.Frame(self, padding=(8, 4))
        bar.pack(fill="x")
        ttk.Button(bar, text="영상 추가", command=self._add_files).pack(side="left")
        ttk.Button(bar, text="폴더 추가", command=self._add_folder).pack(side="left", padx=4)
        ttk.Button(bar, text="목록 비우기", command=self._clear).pack(side="left", padx=4)
        self.count_lbl = ttk.Label(bar, text="")
        self.count_lbl.pack(side="right")

        lf = ttk.Frame(self, padding=(8, 0))
        lf.pack(fill="both", expand=True)
        self.lst = tk.Listbox(lf, height=10)
        self.lst.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(lf, command=self.lst.yview)
        sb.pack(side="right", fill="y")
        self.lst.config(yscrollcommand=sb.set)
        if DND_FILES:
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<Drop>>", lambda e: self.add_paths(self.tk.splitlist(e.data)))

        opt = ttk.LabelFrame(self, text="설정", padding=8)
        opt.pack(fill="x", padx=8, pady=6)
        r1 = ttk.Frame(opt); r1.pack(fill="x")
        ttk.Label(r1, text="조각 길이(분):").pack(side="left")
        self.minutes = tk.StringVar(value="5")
        ttk.Spinbox(r1, from_=1, to=120, width=5, textvariable=self.minutes).pack(side="left", padx=4)
        self.mode = tk.StringVar(value="share")
        r2 = ttk.Frame(opt); r2.pack(fill="x", pady=(6, 0))
        ttk.Radiobutton(r2, text="공유용 (작게: 조각마다", variable=self.mode, value="share").pack(side="left")
        self.max_mb = tk.StringVar(value="24")
        ttk.Spinbox(r2, from_=5, to=500, width=5, textvariable=self.max_mb).pack(side="left", padx=2)
        ttk.Label(r2, text="MB 이하, 소리 없음, 가로 1280)").pack(side="left")
        r3 = ttk.Frame(opt); r3.pack(fill="x")
        ttk.Radiobutton(r3, text="원본 그대로 (빠름, 화질·소리 그대로, 파일 큼)", variable=self.mode, value="copy").pack(side="left")
        ttk.Label(opt, text="결과는 영상 옆 '영상이름_조각' 폴더에 저장됩니다. "
                            "GitHub 웹 업로드는 파일당 25MB까지라서 공유용 24MB가 기본입니다.",
                  foreground="#555").pack(anchor="w", pady=(6, 0))

        run = ttk.Frame(self, padding=(8, 0))
        run.pack(fill="x")
        self.start_btn = ttk.Button(run, text="▶ 자르기 시작", command=self._start)
        self.start_btn.pack(side="left")
        self.cancel_btn = ttk.Button(run, text="취소", command=self.cancel.set, state="disabled")
        self.cancel_btn.pack(side="left", padx=4)
        self.open_btn = ttk.Button(run, text="결과 폴더 열기", command=self._open_out, state="disabled")
        self.open_btn.pack(side="left", padx=4)
        self.pb = ttk.Progressbar(self, maximum=1000)
        self.pb.pack(fill="x", padx=8, pady=6)
        self.status = ttk.Label(self, text="", padding=(8, 0))
        self.status.pack(fill="x")
        self.log = tk.Text(self, height=8, state="disabled")
        self.log.pack(fill="both", expand=False, padx=8, pady=(4, 8))
        self.last_out = None

    # ---- list ----
    def add_paths(self, paths):
        for p in list_videos(paths):
            if p not in self.files:
                self.files.append(p)
                self.lst.insert("end", p)
        self.count_lbl.config(text="영상 %d개" % len(self.files))

    def _add_files(self):
        fs = filedialog.askopenfilenames(title="영상 선택", filetypes=[
            ("영상", "*.mp4 *.mov *.avi *.mkv *.wmv *.m4v *.webm *.flv *.ts *.mts *.m2ts *.mpg *.mpeg *.3gp"),
            ("모든 파일", "*.*")])
        self.add_paths(fs)

    def _add_folder(self):
        d = filedialog.askdirectory(title="폴더 선택")
        if d:
            self.add_paths([d])

    def _clear(self):
        self.files = []
        self.lst.delete(0, "end")
        self.count_lbl.config(text="")

    def _log(self, s):
        self.log.config(state="normal")
        self.log.insert("end", s + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    # ---- run ----
    def _start(self):
        if not self.files:
            messagebox.showinfo(APP_TITLE, "영상을 먼저 추가하세요.")
            return
        try:
            minutes = float(self.minutes.get())
            max_mb = float(self.max_mb.get())
        except ValueError:
            messagebox.showerror(APP_TITLE, "숫자를 확인하세요.")
            return
        self.cancel.clear()
        self.start_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.worker = threading.Thread(target=self._work, args=(list(self.files), minutes, self.mode.get(), max_mb),
                                       daemon=True)
        self.worker.start()

    def _work(self, files, minutes, mode, max_mb):
        q = self.q
        n = len(files)
        done = 0
        for i, p in enumerate(files):
            if self.cancel.is_set():
                break
            q.put(("log", "(%d/%d) %s" % (i + 1, n, os.path.basename(p))))

            def prog(f, i=i):
                q.put(("prog", (i + f) / n, "%d/%d  %s  %d%%" % (i + 1, n, os.path.basename(p), f * 100)))
            try:
                outs = split_video(p, minutes, mode, max_mb, progress=prog, cancel=self.cancel)
                done += 1
                if outs:
                    q.put(("out", os.path.dirname(outs[0])))
                for o in outs:
                    q.put(("log", "   → %s  (%.1f MB)" % (os.path.basename(o), os.path.getsize(o) / 2 ** 20)))
            except Cancelled:
                q.put(("log", "취소했습니다."))
                break
            except Exception as e:
                write_error_log(traceback.format_exc())
                q.put(("log", "   실패: %s" % e))
        q.put(("end", "완료: 영상 %d개 / %d개" % (done, n)))

    def _poll(self):
        try:
            while True:
                m = self.q.get_nowait()
                if m[0] == "log":
                    self._log(m[1])
                elif m[0] == "prog":
                    self.pb["value"] = m[1] * 1000
                    self.status.config(text=m[2])
                elif m[0] == "out":
                    self.last_out = m[1]
                    self.open_btn.config(state="normal")
                elif m[0] == "end":
                    self.pb["value"] = 1000
                    self.status.config(text=m[1])
                    self._log(m[1])
                    self.start_btn.config(state="normal")
                    self.cancel_btn.config(state="disabled")
        except queue.Empty:
            pass
        self.after(150, self._poll)

    def _open_out(self):
        if self.last_out and os.path.isdir(self.last_out):
            try:
                os.startfile(self.last_out)  # type: ignore[attr-defined]
            except Exception:
                pass

    def _on_close(self):
        self.cancel.set()
        self.destroy()


def run(paths=()):
    _set_dpi_awareness()
    try:
        App(paths).mainloop()
    except Exception:
        write_error_log(traceback.format_exc())
        raise
