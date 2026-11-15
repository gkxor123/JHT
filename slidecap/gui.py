"""Tkinter GUI: pick videos, select the slide area, convert to PPTX."""
from __future__ import annotations

import os
import queue
import sys
import threading
import time
import tkinter as tk
import traceback
from tkinter import filedialog, messagebox, ttk
from typing import Dict, List, Optional, Tuple

from PIL import Image, ImageTk

from . import __version__
from .engine import (Cancelled, Settings, VIDEO_EXTS, fmt_time, frame_at,
                     list_videos, process_video, video_info)

APP_TITLE = "SlideCap - 강의 영상 → PPT %s" % __version__

try:  # optional drag & drop support
    from tkinterdnd2 import DND_FILES, TkinterDnD  # type: ignore
    _BaseTk = TkinterDnD.Tk
except Exception:  # pragma: no cover
    DND_FILES = None
    _BaseTk = tk.Tk


def _set_dpi_awareness() -> None:
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass


# --------------------------------------------------------------------------
# region selection dialog
# --------------------------------------------------------------------------

class RegionDialog(tk.Toplevel):
    """Shows a video frame; the user drags the slide area and optional
    exclusion areas (e.g. a webcam picture lying over the slide)."""

    HANDLE = 8

    def __init__(self, master, path: str, info: dict, initial=None, index_text=""):
        super().__init__(master)
        self.title("영역 선택 %s- %s" % (index_text, os.path.basename(path)))
        self.path = path
        self.vw, self.vh = info["width"], info["height"]
        self.duration = info["duration"] or 0.0
        self.result = None  # (roi, excludes) or "skip"
        self.roi: Optional[List[int]] = list(initial[0]) if initial else None
        self.excludes: List[List[int]] = [list(e) for e in initial[1]] if initial else []
        self.mode = tk.StringVar(value="roi")
        self._drag = None
        self._img_tk = None

        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        maxw, maxh = int(sw * 0.85), int(sh * 0.70)
        self.scale = min(maxw / self.vw, maxh / self.vh, 1.0 if self.vw > 800 else 2.0)
        self.cw, self.ch = int(self.vw * self.scale), int(self.vh * self.scale)

        top = ttk.Frame(self, padding=(8, 6))
        top.pack(fill="x")
        ttk.Label(top, text="① 마우스로 PPT 화면 영역을 드래그하세요. 모서리를 끌어 크기 조절, 안쪽을 끌어 이동.",
                  font=("Malgun Gothic", 10, "bold")).pack(anchor="w")
        ttk.Label(top, text="② (선택) 강사 웹캠 등이 슬라이드 위에 겹쳐 있으면 '제외 영역'을 선택하고 드래그하세요. "
                            "움직이는 웹캠/GIF는 자동으로도 무시됩니다.").pack(anchor="w")

        self.canvas = tk.Canvas(self, width=self.cw, height=self.ch, bg="black",
                                highlightthickness=0, cursor="crosshair")
        self.canvas.pack(padx=8)

        tl = ttk.Frame(self, padding=(8, 4))
        tl.pack(fill="x")
        ttk.Label(tl, text="화면 시점:").pack(side="left")
        self.time_var = tk.DoubleVar(value=min(60.0, self.duration * 0.1))
        self.slider = ttk.Scale(tl, from_=0, to=max(1.0, self.duration), variable=self.time_var,
                                orient="horizontal", command=self._on_slide)
        self.slider.pack(side="left", fill="x", expand=True, padx=6)
        self.time_lbl = ttk.Label(tl, width=20)
        self.time_lbl.pack(side="left")
        self.slider.bind("<ButtonRelease-1>", lambda e: self._load_frame())

        bar = ttk.Frame(self, padding=(8, 4, 8, 8))
        bar.pack(fill="x")
        ttk.Radiobutton(bar, text="슬라이드 영역", value="roi", variable=self.mode).pack(side="left")
        ttk.Radiobutton(bar, text="제외 영역 (웹캠 등)", value="ex", variable=self.mode).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="제외 영역 모두 지우기", command=self._clear_ex).pack(side="left", padx=6)
        ttk.Button(bar, text="화면 전체", command=self._full).pack(side="left")
        self.info_lbl = ttk.Label(bar, foreground="#555")
        self.info_lbl.pack(side="left", padx=10)
        ttk.Button(bar, text="확인 (Enter)", command=self._ok).pack(side="right")
        ttk.Button(bar, text="이 영상 건너뛰기", command=self._skip).pack(side="right", padx=6)

        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._move)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Motion>", self._hover)
        self.canvas.bind("<ButtonPress-3>", self._right)
        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self._skip())
        self.protocol("WM_DELETE_WINDOW", self._skip)

        self.transient(master)
        self._load_frame()
        self.update_idletasks()
        x = max(0, (sw - self.winfo_width()) // 2)
        y = max(0, (sh - self.winfo_height()) // 2 - 30)
        self.geometry("+%d+%d" % (x, y))
        self.grab_set()
        self.focus_force()

    # ---- frame ----
    def _on_slide(self, _=None):
        self.time_lbl.config(text="%s / %s" % (fmt_time(self.time_var.get()), fmt_time(self.duration)))

    def _load_frame(self):
        self._on_slide()
        t = float(self.time_var.get())
        try:
            self.config(cursor="watch"); self.update_idletasks()
            bgr = frame_at(self.path, t)
        except Exception as e:  # pragma: no cover
            messagebox.showerror("오류", "프레임을 읽지 못했습니다:\n%s" % e, parent=self)
            return
        finally:
            self.config(cursor="")
        img = Image.fromarray(bgr[:, :, ::-1])
        if img.size != (self.vw, self.vh):
            self.vw, self.vh = img.size
        img = img.resize((self.cw, self.ch), Image.LANCZOS if self.scale < 1 else Image.NEAREST)
        self._img_tk = ImageTk.PhotoImage(img)
        self._redraw()

    # ---- coordinates ----
    def _to_video(self, x, y):
        return (min(self.vw, max(0, int(round(x / self.scale)))),
                min(self.vh, max(0, int(round(y / self.scale)))))

    def _to_canvas(self, r):
        x, y, w, h = r
        s = self.scale
        return x * s, y * s, (x + w) * s, (y + h) * s

    @staticmethod
    def _norm(x0, y0, x1, y1):
        return [min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0)]

    def _hit(self, cx, cy):
        """Which part of the ROI is under the canvas point."""
        if not self.roi:
            return None
        x0, y0, x1, y1 = self._to_canvas(self.roi)
        hs = self.HANDLE
        near_l, near_r = abs(cx - x0) <= hs, abs(cx - x1) <= hs
        near_t, near_b = abs(cy - y0) <= hs, abs(cy - y1) <= hs
        inside_x, inside_y = x0 - hs <= cx <= x1 + hs, y0 - hs <= cy <= y1 + hs
        edge = ""
        if inside_y and near_l: edge += "l"
        if inside_y and near_r: edge += "r"
        if inside_x and near_t: edge += "t"
        if inside_x and near_b: edge += "b"
        if edge:
            return edge
        if x0 < cx < x1 and y0 < cy < y1:
            return "move"
        return None

    # ---- mouse ----
    def _hover(self, e):
        if self.mode.get() != "roi":
            self.canvas.config(cursor="crosshair"); return
        h = self._hit(e.x, e.y)
        cur = {"l": "sb_h_double_arrow", "r": "sb_h_double_arrow", "t": "sb_v_double_arrow",
               "b": "sb_v_double_arrow", "move": "fleur"}.get(h or "", "crosshair")
        if h and len(h) == 2:
            cur = "sizing"
        self.canvas.config(cursor=cur)

    def _press(self, e):
        vx, vy = self._to_video(e.x, e.y)
        if self.mode.get() == "roi":
            h = self._hit(e.x, e.y)
            if h:
                self._drag = ("edit", h, vx, vy, list(self.roi))
                return
            self._drag = ("new_roi", vx, vy)
        else:
            self._drag = ("new_ex", vx, vy)
            self.excludes.append([vx, vy, 0, 0])

    def _move(self, e):
        if not self._drag:
            return
        vx, vy = self._to_video(e.x, e.y)
        kind = self._drag[0]
        if kind == "new_roi":
            self.roi = self._norm(self._drag[1], self._drag[2], vx, vy)
        elif kind == "new_ex":
            self.excludes[-1] = self._norm(self._drag[1], self._drag[2], vx, vy)
        elif kind == "edit":
            _, h, sx, sy, (rx, ry, rw, rh) = self._drag
            dx, dy = vx - sx, vy - sy
            x0, y0, x1, y1 = rx, ry, rx + rw, ry + rh
            if h == "move":
                dx = max(-x0, min(self.vw - x1, dx)); dy = max(-y0, min(self.vh - y1, dy))
                x0, x1, y0, y1 = x0 + dx, x1 + dx, y0 + dy, y1 + dy
            else:
                if "l" in h: x0 = min(x1 - 10, max(0, x0 + dx))
                if "r" in h: x1 = max(x0 + 10, min(self.vw, x1 + dx))
                if "t" in h: y0 = min(y1 - 10, max(0, y0 + dy))
                if "b" in h: y1 = max(y0 + 10, min(self.vh, y1 + dy))
            self.roi = [x0, y0, x1 - x0, y1 - y0]
        self._redraw()

    def _release(self, e):
        if self._drag and self._drag[0] == "new_ex":
            if self.excludes and (self.excludes[-1][2] < 4 or self.excludes[-1][3] < 4):
                self.excludes.pop()
        if self._drag and self._drag[0] == "new_roi":
            if self.roi and (self.roi[2] < 8 or self.roi[3] < 8):
                self.roi = None
        self._drag = None
        self._redraw()

    def _right(self, e):
        vx, vy = self._to_video(e.x, e.y)
        for i in range(len(self.excludes) - 1, -1, -1):
            x, y, w, h = self.excludes[i]
            if x <= vx <= x + w and y <= vy <= y + h:
                self.excludes.pop(i)
                break
        self._redraw()

    def _clear_ex(self):
        self.excludes.clear(); self._redraw()

    def _full(self):
        self.roi = [0, 0, self.vw, self.vh]; self._redraw()

    # ---- drawing ----
    def _redraw(self):
        c = self.canvas
        c.delete("all")
        if self._img_tk:
            c.create_image(0, 0, image=self._img_tk, anchor="nw")
        if self.roi:
            x0, y0, x1, y1 = self._to_canvas(self.roi)
            # dim outside
            for rect in ((0, 0, self.cw, y0), (0, y1, self.cw, self.ch), (0, y0, x0, y1), (x1, y0, self.cw, y1)):
                c.create_rectangle(*rect, fill="black", stipple="gray50", width=0)
            c.create_rectangle(x0, y0, x1, y1, outline="#00ff66", width=2)
            hs = 4
            for px, py in ((x0, y0), (x1, y0), (x0, y1), (x1, y1),
                           ((x0 + x1) / 2, y0), ((x0 + x1) / 2, y1), (x0, (y0 + y1) / 2), (x1, (y0 + y1) / 2)):
                c.create_rectangle(px - hs, py - hs, px + hs, py + hs, fill="#00ff66", outline="black")
        for ex in self.excludes:
            x0, y0, x1, y1 = self._to_canvas(ex)
            c.create_rectangle(x0, y0, x1, y1, outline="#ff3040", width=2, dash=(5, 3))
            c.create_line(x0, y0, x1, y1, fill="#ff3040")
            c.create_line(x0, y1, x1, y0, fill="#ff3040")
        txt = "영상 %dx%d" % (self.vw, self.vh)
        if self.roi:
            txt += "  |  선택 영역 %dx%d" % (self.roi[2], self.roi[3])
        if self.excludes:
            txt += "  |  제외 %d개 (우클릭으로 삭제)" % len(self.excludes)
        self.info_lbl.config(text=txt)

    # ---- result ----
    def _ok(self):
        if not self.roi or self.roi[2] < 32 or self.roi[3] < 32:
            messagebox.showwarning("영역 선택", "PPT 화면 영역을 드래그해서 선택해 주세요.", parent=self)
            return
        x, y, w, h = self.roi
        exs = []
        for ex, ey, ew, eh in self.excludes:
            # clip to roi
            x0, y0 = max(x, ex), max(y, ey)
            x1, y1 = min(x + w, ex + ew), min(y + h, ey + eh)
            if x1 - x0 > 2 and y1 - y0 > 2:
                exs.append((x0, y0, x1 - x0, y1 - y0))
        self.result = ((x, y, w, h), exs)
        self.grab_release()
        self.destroy()

    def _skip(self):
        self.result = "skip"
        self.grab_release()
        self.destroy()


# --------------------------------------------------------------------------
# main window
# --------------------------------------------------------------------------

class App(_BaseTk):
    def __init__(self, initial_paths=()):
        super().__init__()
        self.title(APP_TITLE)
        try:
            self.iconbitmap(default=_resource("icon.ico"))
        except Exception:
            pass
        default_font = ("Malgun Gothic", 10) if sys.platform == "win32" else None
        if default_font:
            self.option_add("*Font", default_font)
            style = ttk.Style(self)
            style.configure(".", font=default_font)
            style.configure("Treeview.Heading", font=("Malgun Gothic", 10, "bold"))
        self.minsize(760, 560)

        self.files: List[str] = []
        self.regions: Dict[str, Tuple] = {}
        self.queue: "queue.Queue" = queue.Queue()
        self.cancel = threading.Event()
        self.worker: Optional[threading.Thread] = None
        self.outputs: List[str] = []

        self._build()
        self.add_paths(initial_paths)
        self.after(100, self._poll)
        self.protocol("WM_DELETE_WINDOW", self._close)

    # ---- layout ----
    def _build(self):
        pad = dict(padx=10, pady=4)
        head = ttk.Frame(self)
        head.pack(fill="x", **pad)
        ttk.Label(head, text="강의 영상에서 PPT 슬라이드를 자동으로 캡처합니다.",
                  font=("Malgun Gothic", 12, "bold")).pack(anchor="w")
        hint = "영상 파일이나 폴더를 추가하세요"
        hint += " (이 창이나 exe 아이콘에 끌어다 놓아도 됩니다)." if DND_FILES else " (exe 아이콘에 끌어다 놓아도 됩니다)."
        ttk.Label(head, text=hint, foreground="#555").pack(anchor="w")

        btns = ttk.Frame(self)
        btns.pack(fill="x", **pad)
        ttk.Button(btns, text="영상 파일 추가", command=self._add_files).pack(side="left")
        ttk.Button(btns, text="폴더 추가", command=self._add_folder).pack(side="left", padx=6)
        ttk.Button(btns, text="선택 항목 삭제", command=self._remove_sel).pack(side="left")
        ttk.Button(btns, text="목록 비우기", command=self._clear).pack(side="left", padx=6)

        lf = ttk.Frame(self)
        lf.pack(fill="both", expand=True, **pad)
        cols = ("file", "region", "status")
        self.tree = ttk.Treeview(lf, columns=cols, show="headings", height=8)
        self.tree.heading("file", text="영상 파일")
        self.tree.heading("region", text="영역")
        self.tree.heading("status", text="상태")
        self.tree.column("file", width=380)
        self.tree.column("region", width=150, anchor="center")
        self.tree.column("status", width=200, anchor="center")
        sb = ttk.Scrollbar(lf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.tree.bind("<Double-1>", self._reselect_region)
        if DND_FILES:
            try:
                self.drop_target_register(DND_FILES)
                self.dnd_bind("<<Drop>>", self._on_drop)
            except Exception:
                pass

        opt = ttk.LabelFrame(self, text="옵션", padding=8)
        opt.pack(fill="x", **pad)
        self.same_region = tk.BooleanVar(value=True)
        self.save_images = tk.BooleanVar(value=False)
        self.dedupe = tk.BooleanVar(value=True)
        self.sens = tk.StringVar(value="normal")
        self.fmt = tk.StringVar(value="jpg")
        self.out_mode = tk.StringVar(value="same")
        self.out_dir = tk.StringVar(value="")

        r1 = ttk.Frame(opt); r1.pack(fill="x")
        ttk.Checkbutton(r1, text="여러 영상에 첫 영상의 영역을 똑같이 적용 (같은 강좌 시리즈일 때)",
                        variable=self.same_region).pack(side="left")
        r2 = ttk.Frame(opt); r2.pack(fill="x", pady=(4, 0))
        ttk.Label(r2, text="민감도:").pack(side="left")
        for txt, val in (("보통 (권장)", "normal"), ("높음 - 작은 변화도 캡처", "high"), ("낮음 - 큰 변화만", "low")):
            ttk.Radiobutton(r2, text=txt, value=val, variable=self.sens).pack(side="left", padx=4)
        r3 = ttk.Frame(opt); r3.pack(fill="x", pady=(4, 0))
        ttk.Checkbutton(r3, text="이전에 나온 슬라이드가 다시 나오면 건너뛰기", variable=self.dedupe).pack(side="left")
        ttk.Checkbutton(r3, text="슬라이드 이미지 파일도 따로 저장", variable=self.save_images).pack(side="left", padx=12)
        ttk.Label(r3, text="이미지 형식:").pack(side="left")
        ttk.Radiobutton(r3, text="JPG", value="jpg", variable=self.fmt).pack(side="left")
        ttk.Radiobutton(r3, text="PNG(무손실)", value="png", variable=self.fmt).pack(side="left")
        r4 = ttk.Frame(opt); r4.pack(fill="x", pady=(4, 0))
        ttk.Label(r4, text="저장 위치:").pack(side="left")
        ttk.Radiobutton(r4, text="영상과 같은 폴더", value="same", variable=self.out_mode).pack(side="left", padx=4)
        ttk.Radiobutton(r4, text="지정 폴더:", value="dir", variable=self.out_mode).pack(side="left")
        ttk.Entry(r4, textvariable=self.out_dir, width=40).pack(side="left", padx=4)
        ttk.Button(r4, text="찾기", command=self._pick_out).pack(side="left")

        run = ttk.Frame(self)
        run.pack(fill="x", **pad)
        self.start_btn = ttk.Button(run, text="▶  시작", command=self._start)
        self.start_btn.pack(side="left", ipadx=10, ipady=3)
        self.cancel_btn = ttk.Button(run, text="취소", command=self._cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=6)
        self.open_btn = ttk.Button(run, text="결과 폴더 열기", command=self._open_out, state="disabled")
        self.open_btn.pack(side="left")
        self.progress = ttk.Progressbar(run, maximum=1000)
        self.progress.pack(side="left", fill="x", expand=True, padx=10)
        self.status = ttk.Label(self, text="대기 중", foreground="#333")
        self.status.pack(fill="x", padx=10)

        logf = ttk.Frame(self)
        logf.pack(fill="both", expand=False, padx=10, pady=(4, 10))
        self.log = tk.Text(logf, height=7, wrap="word", state="disabled", bg="#f7f7f7", relief="flat")
        self.log.pack(fill="both", expand=True)

    # ---- file list ----
    def add_paths(self, paths):
        added = 0
        for p in paths:
            p = p.strip().strip('"').strip("{}")
            if not p:
                continue
            if os.path.isdir(p):
                vids = list_videos(p)
                if not vids:
                    self._log("폴더에 영상 파일이 없습니다: %s" % p)
            elif os.path.isfile(p):
                vids = [p]
                if os.path.splitext(p)[1].lower() not in VIDEO_EXTS:
                    self._log("영상 확장자가 아니지만 시도합니다: %s" % os.path.basename(p))
            else:
                continue
            for v in vids:
                v = os.path.abspath(v)
                if v in self.files:
                    continue
                self.files.append(v)
                self.tree.insert("", "end", iid=v, values=(os.path.basename(v), "-", "대기"))
                added += 1
        if added:
            self._log("%d개 영상을 추가했습니다." % added)

    def _add_files(self):
        exts = " ".join("*%s" % e for e in sorted(VIDEO_EXTS))
        paths = filedialog.askopenfilenames(title="영상 파일 선택",
                                            filetypes=[("영상 파일", exts), ("모든 파일", "*.*")])
        self.add_paths(paths)

    def _add_folder(self):
        d = filedialog.askdirectory(title="영상이 있는 폴더 선택")
        if d:
            self.add_paths([d])

    def _on_drop(self, event):
        self.add_paths(self.tk.splitlist(event.data))

    def _remove_sel(self):
        if self._busy():
            return
        for iid in self.tree.selection():
            self.tree.delete(iid)
            self.files.remove(iid)
            self.regions.pop(iid, None)

    def _clear(self):
        if self._busy():
            return
        self.tree.delete(*self.tree.get_children())
        self.files.clear(); self.regions.clear()

    def _pick_out(self):
        d = filedialog.askdirectory(title="저장할 폴더 선택")
        if d:
            self.out_dir.set(d); self.out_mode.set("dir")

    def _reselect_region(self, _e=None):
        if self._busy():
            return
        sel = self.tree.selection()
        if not sel:
            return
        p = sel[0]
        r = self._ask_region(p, self.regions.get(p))
        if r and r != "skip":
            self.regions[p] = r
            self._set_row(p, region=self._region_text(r))

    # ---- helpers ----
    def _busy(self):
        return self.worker is not None and self.worker.is_alive()

    def _log(self, msg):
        self.log.config(state="normal")
        self.log.insert("end", time.strftime("[%H:%M:%S] ") + msg + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def _set_row(self, p, region=None, status=None):
        if not self.tree.exists(p):
            return
        v = list(self.tree.item(p, "values"))
        if region is not None:
            v[1] = region
        if status is not None:
            v[2] = status
        self.tree.item(p, values=v)

    @staticmethod
    def _region_text(r):
        (x, y, w, h), ex = r
        return "%dx%d%s" % (w, h, (" (제외 %d)" % len(ex)) if ex else "")

    def _ask_region(self, path, initial=None, index_text=""):
        try:
            info = video_info(path)
        except Exception as e:
            self._log("열 수 없는 파일: %s (%s)" % (os.path.basename(path), e))
            self._set_row(path, status="열기 실패")
            return None
        dlg = RegionDialog(self, path, info, initial, index_text)
        self.wait_window(dlg)
        return dlg.result

    @staticmethod
    def _scale_region(r, src_info, dst_info):
        (x, y, w, h), exs = r
        sx = dst_info["width"] / float(src_info["width"])
        sy = dst_info["height"] / float(src_info["height"])

        def sc(a, b, c, d):
            return (int(round(a * sx)), int(round(b * sy)), int(round(c * sx)), int(round(d * sy)))
        return sc(x, y, w, h), [sc(*e) for e in exs]

    # ---- run ----
    def _start(self):
        if self._busy():
            return
        if not self.files:
            messagebox.showinfo(APP_TITLE, "먼저 영상 파일이나 폴더를 추가하세요.")
            return
        if self.out_mode.get() == "dir":
            od = self.out_dir.get().strip()
            if not od:
                messagebox.showwarning(APP_TITLE, "저장 폴더를 지정하세요."); return
            try:
                os.makedirs(od, exist_ok=True)
            except Exception as e:
                messagebox.showerror(APP_TITLE, "저장 폴더를 만들 수 없습니다:\n%s" % e); return

        # 1) collect regions for every file (all questions first, then unattended)
        jobs = []
        first = None
        n = len(self.files)
        for i, p in enumerate(list(self.files)):
            r = self.regions.get(p)
            if r is None and self.same_region.get() and first is not None:
                try:
                    r = self._scale_region(first[1], first[0], video_info(p))
                except Exception as e:
                    self._log("열 수 없는 파일: %s (%s)" % (os.path.basename(p), e))
                    self._set_row(p, status="열기 실패")
                    continue
            if r is None:
                r = self._ask_region(p, index_text="(%d/%d) " % (i + 1, n))
                if r is None:
                    continue
                if r == "skip":
                    self._set_row(p, status="건너뜀")
                    continue
            self.regions[p] = r
            self._set_row(p, region=self._region_text(r), status="대기")
            if first is None:
                try:
                    first = (video_info(p), r)
                except Exception:
                    pass
            jobs.append((p, r))
        if not jobs:
            return

        s = Settings.preset(self.sens.get())
        s.dedupe = bool(self.dedupe.get())
        s.image_format = self.fmt.get()
        out_dir = self.out_dir.get().strip() if self.out_mode.get() == "dir" else None
        self.cancel.clear()
        self.outputs = []
        self.start_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.open_btn.config(state="disabled")
        self.worker = threading.Thread(target=self._work, args=(jobs, s, out_dir, bool(self.save_images.get())),
                                       daemon=True)
        self.worker.start()

    def _work(self, jobs, settings, out_dir, save_images):
        q = self.queue
        total = len(jobs)
        for i, (p, (roi, exs)) in enumerate(jobs):
            if self.cancel.is_set():
                break
            name = os.path.basename(p)
            q.put(("row", p, "분석 중 0%"))
            q.put(("log", "(%d/%d) 시작: %s" % (i + 1, total, name)))
            t0 = time.time()

            def prog(frac, ncap, i=i, p=p, t0=t0):
                el = time.time() - t0
                eta = (el / frac - el) if frac > 0.01 else None
                q.put(("progress", (i + frac) / total, p, frac, ncap, eta, i, total))

            try:
                out, n = process_video(p, roi, exs, settings, out_dir, save_images,
                                       progress=prog, cancel=self.cancel)
                if out:
                    self.outputs.append(out)
                    q.put(("row", p, "완료 - %d장" % n))
                    q.put(("log", "완료: %s → %s (%d장, %.0f초)" % (name, os.path.basename(out), n, time.time() - t0)))
                else:
                    q.put(("row", p, "슬라이드 없음"))
                    q.put(("log", "슬라이드를 찾지 못했습니다: %s (영역을 다시 확인하세요)" % name))
            except Cancelled:
                q.put(("row", p, "취소됨"))
                q.put(("log", "취소되었습니다."))
                break
            except Exception as e:
                q.put(("row", p, "오류"))
                q.put(("log", "오류: %s\n%s" % (name, "".join(traceback.format_exception_only(type(e), e)).strip())))
                q.put(("trace", traceback.format_exc()))
        q.put(("done",))

    def _poll(self):
        try:
            while True:
                m = self.queue.get_nowait()
                kind = m[0]
                if kind == "log":
                    self._log(m[1])
                elif kind == "trace":
                    _write_error_log(m[1])
                elif kind == "row":
                    self._set_row(m[1], status=m[2])
                elif kind == "progress":
                    _, overall, p, frac, ncap, eta, i, total = m
                    self.progress["value"] = overall * 1000
                    self._set_row(p, status="분석 중 %d%% (%d장)" % (frac * 100, ncap))
                    txt = "(%d/%d) %s - %d%%, 슬라이드 %d장" % (i + 1, total, os.path.basename(p), frac * 100, ncap)
                    if eta is not None:
                        txt += ", 남은 시간 약 %s" % fmt_time(eta)
                    self.status.config(text=txt)
                elif kind == "done":
                    self.start_btn.config(state="normal")
                    self.cancel_btn.config(state="disabled")
                    if self.outputs:
                        self.open_btn.config(state="normal")
                        self.progress["value"] = 1000
                        self.status.config(text="완료: PPT %d개 생성" % len(self.outputs))
                        if not self.cancel.is_set():
                            if messagebox.askyesno(APP_TITLE, "완료되었습니다. PPT %d개를 만들었습니다.\n\n결과 폴더를 열까요?"
                                                   % len(self.outputs)):
                                self._open_out()
                    else:
                        self.status.config(text="종료")
        except queue.Empty:
            pass
        self.after(150, self._poll)

    def _cancel(self):
        self.cancel.set()
        self.status.config(text="취소 중...")

    def _open_out(self):
        if not self.outputs:
            return
        target = self.outputs[-1]
        try:
            if sys.platform == "win32":
                import subprocess
                subprocess.Popen(["explorer", "/select,", os.path.normpath(target)])
            elif sys.platform == "darwin":
                import subprocess
                subprocess.Popen(["open", "-R", target])
            else:
                import subprocess
                subprocess.Popen(["xdg-open", os.path.dirname(target)])
        except Exception as e:
            self._log("폴더를 열 수 없습니다: %s" % e)

    def _close(self):
        if self._busy():
            if not messagebox.askyesno(APP_TITLE, "작업 중입니다. 취소하고 종료할까요?"):
                return
            self.cancel.set()
        self.destroy()


def _resource(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def _write_error_log(text):
    try:
        p = os.path.join(os.path.expanduser("~"), "slidecap_error.log")
        with open(p, "a", encoding="utf-8") as f:
            f.write(time.strftime("\n==== %Y-%m-%d %H:%M:%S ====\n") + text)
    except Exception:
        pass


def run(paths=()):
    _set_dpi_awareness()
    app = App(paths)
    app.mainloop()
