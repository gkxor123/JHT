"""PPT2PDF: convert many PowerPoint files to PDF at once.

GUI:           PPT2PDF.exe [files or folders ...]
               (files dropped on the exe icon start converting right away)
Command line:  PPT2PDF.exe --cli FILES_OR_FOLDERS... [--out DIR]
                   [--engine auto|powerpoint|libreoffice] [--quality print|screen]
                   [--overwrite] [--workers N]
"""
import argparse
import os
import queue
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core  # noqa: E402

APP = "PPT2PDF"


def cli(argv):
    ap = argparse.ArgumentParser(prog="PPT2PDF --cli")
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--out")
    ap.add_argument("--engine", default="auto", choices=["auto", "powerpoint", "libreoffice"])
    ap.add_argument("--quality", default="print", choices=["print", "screen"])
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--workers", type=int)
    a = ap.parse_args(argv)
    jobs = core.plan(core.collect(a.paths), a.out)
    if not jobs:
        print("변환할 PPT 파일이 없습니다.")
        return 2
    t0 = time.time()

    def rep(i, s, m):
        if s != core.RUNNING:
            print("[%s] %s %s" % (s, jobs[i][0], m), flush=True)
    try:
        c = core.convert(jobs, rep, engine=a.engine, quality=a.quality,
                         skip_existing=not a.overwrite, workers=a.workers)
    except RuntimeError as e:
        print("ERROR", e)
        return 3
    print("완료 %d, 건너뜀 %d, 실패 %d (%s)" % (c[core.DONE], c[core.SKIPPED], c[core.FAILED],
                                          core.fmt_time(time.time() - t0)))
    return 1 if c[core.FAILED] else 0


# ---------------------------------------------------------------- GUI
STATUS_TEXT = {core.RUNNING: "변환 중…", core.DONE: "완료", core.SKIPPED: "건너뜀",
               core.FAILED: "실패"}


class App:
    def __init__(self, paths, autostart):
        import tkinter as tk
        from tkinter import ttk
        try:
            from tkinterdnd2 import DND_FILES, TkinterDnD
            self.root = TkinterDnD.Tk()
            dnd = True
        except Exception:
            self.root = tk.Tk()
            dnd = False
        self.tk, self.ttk = tk, ttk
        r = self.root
        r.title(APP)
        r.geometry("820x520")
        r.minsize(600, 360)
        self.items = []          # [(src, tree iid)]
        self.q = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False

        top = ttk.Frame(r, padding=8)
        top.pack(fill="x")
        ttk.Button(top, text="파일 추가", command=self.add_files).pack(side="left")
        ttk.Button(top, text="폴더 추가", command=self.add_folder).pack(side="left", padx=4)
        ttk.Button(top, text="선택 삭제", command=self.remove_sel).pack(side="left")
        ttk.Button(top, text="목록 비우기", command=self.clear).pack(side="left", padx=4)
        self.start_btn = ttk.Button(top, text="▶ PDF로 변환", command=self.start)
        self.start_btn.pack(side="right")

        opt = ttk.Frame(r, padding=(8, 0))
        opt.pack(fill="x")
        engines = core.available_engines()
        self.engine = tk.StringVar(value="auto")
        ttk.Label(opt, text="엔진").pack(side="left")
        ttk.Combobox(opt, textvariable=self.engine, state="readonly", width=12,
                     values=["auto"] + engines).pack(side="left", padx=(4, 12))
        self.quality = tk.StringVar(value="print")
        ttk.Radiobutton(opt, text="원본 화질", value="print", variable=self.quality).pack(side="left")
        ttk.Radiobutton(opt, text="작은 용량", value="screen", variable=self.quality).pack(side="left", padx=(0, 12))
        self.skip = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt, text="이미 변환된 파일 건너뛰기", variable=self.skip).pack(side="left")

        outf = ttk.Frame(r, padding=(8, 6))
        outf.pack(fill="x")
        self.same_dir = tk.BooleanVar(value=True)
        self.out_dir = tk.StringVar(value="")
        ttk.Checkbutton(outf, text="원본과 같은 폴더에 저장", variable=self.same_dir,
                        command=self._out_toggle).pack(side="left")
        self.out_entry = ttk.Entry(outf, textvariable=self.out_dir, state="disabled")
        self.out_entry.pack(side="left", fill="x", expand=True, padx=4)
        self.out_btn = ttk.Button(outf, text="찾아보기", command=self.pick_out, state="disabled")
        self.out_btn.pack(side="left")

        mid = ttk.Frame(r, padding=(8, 0))
        mid.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(mid, columns=("file", "status"), show="headings", selectmode="extended")
        self.tree.heading("file", text="파일  (여기에 파일·폴더를 끌어다 놓으세요)" if dnd else "파일")
        self.tree.heading("status", text="상태")
        self.tree.column("file", width=560)
        self.tree.column("status", width=200, anchor="w")
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.tag_configure("failed", foreground="#c62828")
        self.tree.tag_configure("done", foreground="#2e7d32")

        bot = ttk.Frame(r, padding=8)
        bot.pack(fill="x")
        self.prog = ttk.Progressbar(bot, mode="determinate")
        self.prog.pack(fill="x")
        self.msg = tk.StringVar(value=self._engine_note(engines))
        ttk.Label(bot, textvariable=self.msg).pack(anchor="w", pady=(4, 0))

        if dnd:
            self.tree.drop_target_register(DND_FILES)
            self.tree.dnd_bind("<<Drop>>", lambda e: self.add(self.root.tk.splitlist(e.data)))
        r.protocol("WM_DELETE_WINDOW", self.on_close)
        self.add(paths)
        if autostart and self.items:
            r.after(200, self.start)

    @staticmethod
    def _engine_note(engines):
        if "powerpoint" in engines:
            return "PowerPoint로 변환합니다 (가장 정확)."
        if engines:
            return "PowerPoint가 없어 LibreOffice로 변환합니다 (병렬 처리)."
        return "⚠ PowerPoint 또는 LibreOffice가 필요합니다."

    def _out_toggle(self):
        st = "disabled" if self.same_dir.get() else "normal"
        self.out_entry.configure(state=st)
        self.out_btn.configure(state=st)

    def pick_out(self):
        from tkinter import filedialog
        d = filedialog.askdirectory(title="PDF 저장 폴더")
        if d:
            self.out_dir.set(os.path.normpath(d))

    def add_files(self):
        from tkinter import filedialog
        fs = filedialog.askopenfilenames(
            title="PPT 파일 선택",
            filetypes=[("PowerPoint", " ".join("*" + e for e in sorted(core.EXTS))), ("모든 파일", "*.*")])
        self.add(fs)

    def add_folder(self):
        from tkinter import filedialog
        d = filedialog.askdirectory(title="PPT가 들어 있는 폴더 (하위 폴더 포함)")
        if d:
            self.add([d])

    def add(self, paths):
        if self.busy:
            return
        have = {os.path.normcase(s) for s, _ in self.items}
        for f in core.collect(list(paths)):
            if os.path.normcase(f) not in have:
                iid = self.tree.insert("", "end", values=(f, "대기"))
                self.items.append((f, iid))
        self.msg.set("%d개 파일" % len(self.items))

    def remove_sel(self):
        if self.busy:
            return
        sel = set(self.tree.selection())
        self.tree.delete(*sel)
        self.items = [(s, i) for s, i in self.items if i not in sel]
        self.msg.set("%d개 파일" % len(self.items))

    def clear(self):
        if self.busy:
            return
        self.tree.delete(*self.tree.get_children())
        self.items = []
        self.msg.set("0개 파일")

    def start(self):
        from tkinter import messagebox
        if self.busy:
            self.cancel.set()
            self.start_btn.configure(text="중지하는 중…", state="disabled")
            return
        if not self.items:
            messagebox.showinfo(APP, "변환할 PPT 파일을 추가하세요.")
            return
        out = None
        if not self.same_dir.get():
            out = self.out_dir.get().strip()
            if not out:
                messagebox.showwarning(APP, "PDF 저장 폴더를 지정하세요.")
                return
        self.jobs = core.plan([s for s, _ in self.items], out)
        for _, iid in self.items:
            self.tree.item(iid, values=(self.tree.item(iid, "values")[0], "대기"), tags=())
        self.busy = True
        self.cancel.clear()
        self.done = 0
        self.t0 = time.time()
        self.prog.configure(maximum=len(self.jobs), value=0)
        self.start_btn.configure(text="■ 중지")
        args = (self.engine.get(), self.quality.get(), self.skip.get())
        threading.Thread(target=self._run, args=args, daemon=True).start()
        self.root.after(100, self._poll)

    def _run(self, engine, quality, skip):
        try:
            c = core.convert(self.jobs, lambda i, s, m: self.q.put((i, s, m)), self.cancel,
                             engine, quality, skip)
            self.q.put(("end", c, None))
        except Exception as e:
            self.q.put(("error", str(e), None))

    def _poll(self):
        try:
            while True:
                i, s, m = self.q.get_nowait()
                if i == "end":
                    return self._finish(s, None)
                if i == "error":
                    return self._finish(None, s)
                iid = self.items[i][1]
                txt = STATUS_TEXT.get(s, s) + (" - " + m if m and s == core.FAILED else "")
                self.tree.item(iid, values=(self.items[i][0], txt), tags=(s,))
                if s == core.RUNNING:
                    self.tree.see(iid)
                else:
                    self.done += 1
                    self.prog.configure(value=self.done)
                    self.msg.set("%d / %d  (%s)" % (self.done, len(self.jobs),
                                                   core.fmt_time(time.time() - self.t0)))
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _finish(self, c, err):
        from tkinter import messagebox
        self.busy = False
        self.start_btn.configure(text="▶ PDF로 변환", state="normal")
        el = core.fmt_time(time.time() - self.t0)
        if err:
            self.msg.set("오류: " + err)
            messagebox.showerror(APP, err)
            return
        s = "완료 %d · 건너뜀 %d · 실패 %d  (%s)" % (c[core.DONE], c[core.SKIPPED], c[core.FAILED], el)
        if self.cancel.is_set():
            s = "중지됨 · " + s
        self.msg.set(s)

    def on_close(self):
        if self.busy:
            from tkinter import messagebox
            if not messagebox.askyesno(APP, "변환 중입니다. 중지하고 닫을까요?"):
                return
            self.cancel.set()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    argv = sys.argv[1:]
    if argv and argv[0] == "--cli":
        if sys.stdout is None:  # windowed exe: keep output in a log file
            log = open(os.path.join(os.path.expanduser("~"), "ppt2pdf_cli.log"), "a", encoding="utf-8")
            sys.stdout = sys.stderr = log
        sys.exit(cli(argv[1:]))
    App(argv, autostart=bool(argv)).run()


if __name__ == "__main__":
    main()
