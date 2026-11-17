"""pdf2ocr window: add PDFs / folders, then OCR them all."""
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
from .core import (Cancelled, OcrUnavailable, WindowsOcr, default_language,
                   list_pdfs, ocr_languages, process_pdf)

APP_TITLE = "pdf2ocr - PDF 글자 검색(Ctrl+F) 만들기 %s" % __version__

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
        p = os.path.join(os.path.expanduser("~"), "pdf2ocr_error.log")
        with open(p, "a", encoding="utf-8") as f:
            f.write(time.strftime("\n==== %Y-%m-%d %H:%M:%S ====\n") + text)
    except Exception:
        pass


class App(_BaseTk):
    def __init__(self, paths=()):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("860x640")
        self.minsize(700, 520)
        self.inputs = []          # what the user added (files and folders)
        self.files = []           # PDFs found from the inputs
        self.q = queue.Queue()
        self.cancel = threading.Event()
        self.worker = None
        self._build()
        self.add_paths(paths)
        self.after(100, self._poll)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---- layout ----
    def _build(self):
        pad = dict(padx=8, pady=4)
        top = ttk.Frame(self, padding=(8, 8, 8, 0))
        top.pack(fill="x")
        ttk.Label(top, text="그림으로 된 PDF 글자를 읽어서 Ctrl+F로 찾을 수 있게 만듭니다.",
                  font=("", 11, "bold")).pack(anchor="w")
        hint = "PDF 파일이나 폴더를 추가하세요"
        hint += " (이 창이나 exe 아이콘에 끌어다 놓아도 됩니다)." if DND_FILES else " (exe 아이콘에 끌어다 놓아도 됩니다)."
        ttk.Label(top, text=hint, foreground="#555").pack(anchor="w")

        bar = ttk.Frame(self, padding=(8, 4))
        bar.pack(fill="x")
        ttk.Button(bar, text="PDF 파일 추가", command=self._add_files).pack(side="left")
        ttk.Button(bar, text="폴더 추가", command=self._add_folder).pack(side="left", padx=4)
        ttk.Button(bar, text="선택 항목 삭제", command=self._remove_sel).pack(side="left", padx=4)
        ttk.Button(bar, text="목록 비우기", command=self._clear).pack(side="left", padx=4)
        self.count_lbl = ttk.Label(bar, text="")
        self.count_lbl.pack(side="right")

        lf = ttk.Frame(self)
        lf.pack(fill="both", expand=True, **pad)
        cols = ("file", "folder", "status")
        self.tree = ttk.Treeview(lf, columns=cols, show="headings", height=10)
        self.tree.heading("file", text="PDF 파일")
        self.tree.heading("folder", text="폴더")
        self.tree.heading("status", text="상태")
        self.tree.column("file", width=330)
        self.tree.column("folder", width=280)
        self.tree.column("status", width=190, anchor="center")
        sb = ttk.Scrollbar(lf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        if DND_FILES:
            try:
                self.drop_target_register(DND_FILES)
                self.dnd_bind("<<Drop>>", lambda e: self.add_paths(self.tk.splitlist(e.data)))
            except Exception:
                pass

        opt = ttk.LabelFrame(self, text="옵션", padding=8)
        opt.pack(fill="x", **pad)
        self.recursive = tk.BooleanVar(value=True)
        self.new_file = tk.BooleanVar(value=False)
        self.force = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="하위 폴더까지 포함 (폴더 안의 폴더에 있는 PDF도 모두 찾기)",
                        variable=self.recursive, command=self._refresh).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(opt, text="저장:").grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Radiobutton(opt, text="원래 PDF에 검색 글자 추가 (보이는 모양은 그대로)",
                        variable=self.new_file, value=False).grid(row=1, column=1, sticky="w", pady=(4, 0))
        ttk.Radiobutton(opt, text="새 파일로 저장 (이름_ocr.pdf)",
                        variable=self.new_file, value=True).grid(row=1, column=2, sticky="w", pady=(4, 0))
        ttk.Checkbutton(opt, text="글자가 이미 있는 페이지도 다시 OCR (보통은 끄기)",
                        variable=self.force).grid(row=2, column=0, columnspan=3, sticky="w")
        ttk.Label(opt, text="OCR 언어:").grid(row=3, column=0, sticky="w", pady=(4, 0))
        self.langs = ocr_languages()
        self.lang = tk.StringVar(value=default_language(self.langs) or "")
        cb = ttk.Combobox(opt, textvariable=self.lang, values=self.langs, width=10, state="readonly")
        cb.grid(row=3, column=1, sticky="w", pady=(4, 0))
        note = "(한국어 ko는 영어 알파벳도 함께 읽습니다)" if self.langs else \
            "이 PC에 OCR 언어가 없습니다 - Windows 설정에서 한국어/English 언어 팩 설치 필요"
        ttk.Label(opt, text=note, foreground="#555" if self.langs else "#c00").grid(
            row=3, column=2, sticky="w", pady=(4, 0))

        run = ttk.Frame(self, padding=(8, 4))
        run.pack(fill="x")
        self.start_btn = ttk.Button(run, text="▶ 시작", command=self._start)
        self.start_btn.pack(side="left")
        self.cancel_btn = ttk.Button(run, text="취소", command=self._cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=4)
        self.pb = ttk.Progressbar(run, maximum=1.0)
        self.pb.pack(side="left", fill="x", expand=True, padx=8)
        self.status = ttk.Label(self, text="", padding=(8, 0))
        self.status.pack(fill="x")
        self.log = tk.Text(self, height=7, state="disabled", wrap="word")
        self.log.pack(fill="both", padx=8, pady=(4, 8))

    # ---- list ----
    def add_paths(self, paths):
        if self._busy():
            return
        for p in paths:
            p = os.path.abspath(p)
            if p not in self.inputs and (os.path.isdir(p) or os.path.isfile(p)):
                self.inputs.append(p)
        self._refresh()

    def _refresh(self):
        if self._busy():
            return
        self.files = list_pdfs(self.inputs, self.recursive.get())
        self.tree.delete(*self.tree.get_children())
        for f in self.files:
            self.tree.insert("", "end", iid=f, values=(os.path.basename(f), os.path.dirname(f), "대기"))
        self.count_lbl.config(text="PDF %d개" % len(self.files))

    def _add_files(self):
        fs = filedialog.askopenfilenames(title="PDF 파일 선택", filetypes=[("PDF", "*.pdf"), ("모든 파일", "*.*")])
        if fs:
            self.add_paths(fs)

    def _add_folder(self):
        d = filedialog.askdirectory(title="PDF가 있는 폴더 선택")
        if d:
            self.add_paths([d])

    def _remove_sel(self):
        if self._busy():
            return
        for iid in self.tree.selection():
            if iid in self.inputs:
                self.inputs.remove(iid)
            else:                       # a file found inside an added folder
                self.files.remove(iid)
            self.tree.delete(iid)
        self.count_lbl.config(text="PDF %d개" % len(self.tree.get_children()))

    def _clear(self):
        if self._busy():
            return
        self.inputs.clear()
        self._refresh()

    # ---- run ----
    def _busy(self):
        return self.worker is not None and self.worker.is_alive()

    def _start(self):
        if self._busy():
            return
        files = list(self.tree.get_children())
        if not files:
            messagebox.showinfo("pdf2ocr", "PDF 파일이나 폴더를 먼저 추가하세요.")
            return
        if not self.langs:
            messagebox.showerror("pdf2ocr", "이 PC에 Windows OCR 언어가 없습니다.\n"
                                 "설정 → 시간 및 언어 → 언어 및 지역에서 한국어(또는 English)를 추가하세요.")
            return
        self.cancel.clear()
        self.start_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        for f in files:
            self.tree.set(f, "status", "대기")
        opts = (self.lang.get(), self.new_file.get(), self.force.get())
        self.worker = threading.Thread(target=self._work, args=(files, opts), daemon=True)
        self.worker.start()

    def _cancel(self):
        self.cancel.set()
        self.status.config(text="취소하는 중... (지금 페이지까지 하고 멈춥니다)")

    def _work(self, files, opts):
        lang, new_file, force = opts
        q = self.q
        try:
            ocr = WindowsOcr(lang)
        except OcrUnavailable as e:
            q.put(("error", str(e)))
            q.put(("end", None))
            return
        total = len(files)
        done = skip = err = 0
        t_all = time.time()
        try:
            for i, p in enumerate(files):
                name = os.path.basename(p)
                q.put(("row", p, "진행 중"))

                def prog(k, n, i=i, p=p):
                    q.put(("progress", (i + (k / n if n else 1)) / total, p,
                           "(%d/%d) %s - %d/%d쪽" % (i + 1, total, os.path.basename(p), k, n)))
                t0 = time.time()
                try:
                    r = process_pdf(p, ocr, new_file=new_file, force=force, progress=prog, cancel=self.cancel)
                except Cancelled:
                    q.put(("row", p, "취소됨"))
                    q.put(("log", "취소되었습니다."))
                    break
                except Exception as e:
                    err += 1
                    q.put(("row", p, "오류"))
                    q.put(("log", "오류: %s - %s" % (name, e)))
                    write_error_log("file: %s\n%s" % (p, traceback.format_exc()))
                    continue
                if r.status == "done":
                    done += 1
                    q.put(("row", p, "완료 - %d쪽 OCR" % r.pages_ocr))
                    q.put(("log", "완료: %s (%d/%d쪽, 단어 %d개, %.0f초)" % (
                        os.path.basename(r.out), r.pages_ocr, r.pages, r.words, time.time() - t0)))
                elif r.status == "exists":
                    skip += 1
                    q.put(("row", p, "건너뜀 - _ocr 파일 있음"))
                else:
                    skip += 1
                    q.put(("row", p, "건너뜀 - 이미 글자 있음"))
        finally:
            ocr.close()
        q.put(("log", "끝: OCR %d개, 건너뜀 %d개, 오류 %d개 (%.0f초)" % (done, skip, err, time.time() - t_all)))
        q.put(("end", None))

    def _poll(self):
        try:
            while True:
                m = self.q.get_nowait()
                kind = m[0]
                if kind == "row":
                    if self.tree.exists(m[1]):
                        self.tree.set(m[1], "status", m[2])
                        self.tree.see(m[1])
                elif kind == "progress":
                    self.pb["value"] = m[1]
                    self.status.config(text=m[3])
                elif kind == "log":
                    self._log(m[1])
                elif kind == "error":
                    messagebox.showerror("pdf2ocr", m[1])
                elif kind == "end":
                    self.start_btn.config(state="normal")
                    self.cancel_btn.config(state="disabled")
                    self.pb["value"] = 1.0 if not self.cancel.is_set() else self.pb["value"]
                    self.status.config(text="끝났습니다.")
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _log(self, msg):
        self.log.config(state="normal")
        self.log.insert("end", time.strftime("[%H:%M:%S] ") + msg + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def _on_close(self):
        if self._busy():
            if not messagebox.askyesno("pdf2ocr", "OCR 중입니다. 멈추고 닫을까요?\n(지금 파일은 바뀌지 않습니다)"):
                return
            self.cancel.set()
            self.worker.join(timeout=30)
        self.destroy()


def run(paths=()):
    _set_dpi_awareness()
    App(paths).mainloop()
