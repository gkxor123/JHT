"""pdf2ocr core: make image-only PDF pages searchable (Ctrl+F).

Each page without text is rendered, read with the OCR engine built into
Windows (Windows.Media.Ocr, offline) and the recognised words are written
back onto the page as invisible text (render mode 3) at the place where they
appear, so Ctrl+F finds and highlights them.  Pages that already carry text
are left alone, which also makes a second run a no-op.
"""
from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

import pymupdf as fitz

TEXT_MIN_CHARS = 10          # a page with fewer extracted chars counts as "image only"
TARGET_PX = 2600             # long side of the rendered page sent to OCR
MAX_ZOOM = 5.0
SUFFIX = "_ocr"

Word = Tuple[str, float, float, float, float]      # text, x, y, w, h (pixels)


class Cancelled(Exception):
    pass


class OcrUnavailable(RuntimeError):
    pass


# --------------------------------------------------------------------------
# file listing
# --------------------------------------------------------------------------

def is_pdf(p: str) -> bool:
    n = os.path.basename(p)
    return n.lower().endswith(".pdf") and not n.startswith("~$")


def list_pdfs(paths: Iterable[str], recursive: bool = True) -> List[str]:
    out, seen = [], set()

    def add(p):
        k = os.path.normcase(os.path.abspath(p))
        if k not in seen:
            seen.add(k)
            out.append(os.path.abspath(p))

    for p in paths:
        if os.path.isdir(p):
            if recursive:
                for root, dirs, files in os.walk(p):
                    dirs.sort()
                    for f in sorted(files):
                        if is_pdf(f):
                            add(os.path.join(root, f))
            else:
                for f in sorted(os.listdir(p)):
                    fp = os.path.join(p, f)
                    if os.path.isfile(fp) and is_pdf(f):
                        add(fp)
        elif os.path.isfile(p) and is_pdf(p):
            add(p)
    return out


# --------------------------------------------------------------------------
# Windows OCR
# --------------------------------------------------------------------------

def _init_mta():
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.ole32.CoInitializeEx(None, 0)   # COINIT_MULTITHREADED
        except Exception:
            pass


def ocr_languages() -> List[str]:
    """Language tags the Windows OCR engine can read on this PC."""
    try:
        _init_mta()
        from winrt.windows.media.ocr import OcrEngine
        return [l.language_tag for l in OcrEngine.available_recognizer_languages]
    except Exception:
        return []


AUTO = "auto"     # English first, Korean OCR for the lines that hold Hangul


def _pick(langs: Sequence[str], prefix: str) -> Optional[str]:
    for l in langs:
        if l.lower().startswith(prefix):
            return l
    return None


def default_language(langs: Sequence[str]) -> Optional[str]:
    return AUTO if langs else None


def describe(mode: str, langs: Sequence[str]) -> str:
    if mode != AUTO:
        return mode
    en, ko = _pick(langs, "en"), _pick(langs, "ko")
    if en and ko:
        return "%s+%s" % (en, ko)
    return en or ko or (langs[0] if langs else "")


def _hangul_ratio(s: str) -> float:
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if "가" <= c <= "힣" or "ㄱ" <= c <= "ㆎ") / len(letters)


def _line_box(line: Sequence[Word]):
    x0 = min(w[1] for w in line); y0 = min(w[2] for w in line)
    x1 = max(w[1] + w[3] for w in line); y1 = max(w[2] + w[4] for w in line)
    return x0, y0, x1, y1


def _overlap(a, b) -> float:
    """Part of box ``a`` covered by box ``b``."""
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    area = max((a[2] - a[0]) * (a[3] - a[1]), 1e-6)
    return ix * iy / area


def merge_lines(en: List[List[Word]], ko: List[List[Word]]) -> List[List[Word]]:
    """English lines, except where the Korean engine found Hangul: there the
    Korean line replaces the English lines it covers (the English engine
    turns Hangul into junk, the Korean engine misspells English)."""
    kor = [l for l in ko if _hangul_ratio("".join(w[0] for w in l)) >= 0.3]
    if not kor:
        return en
    kb = [_line_box(l) for l in kor]
    keep = []
    for l in en:
        b = _line_box(l)
        if any(_overlap(b, k) > 0.5 or _overlap(k, b) > 0.5 for k in kb):
            continue
        keep.append(l)
    out = keep + kor
    out.sort(key=lambda l: (_line_box(l)[1], _line_box(l)[0]))
    return out


class WindowsOcr:
    def __init__(self, lang: Optional[str] = None):
        _init_mta()
        try:
            from winrt.windows.media.ocr import OcrEngine
            from winrt.windows.globalization import Language
        except Exception as e:  # pragma: no cover
            raise OcrUnavailable("Windows OCR을 불러오지 못했습니다: %s" % e)
        langs = [l.language_tag for l in OcrEngine.available_recognizer_languages]
        if not langs:
            raise OcrUnavailable(
                "이 PC에 Windows OCR 언어가 없습니다.\n"
                "설정 → 시간 및 언어 → 언어 및 지역에서 English(또는 한국어)를 추가하세요.")
        lang = lang or AUTO
        if lang == AUTO:
            en, ko = _pick(langs, "en"), _pick(langs, "ko")
            tags = [t for t in (en, ko) if t] or [langs[0]]
        else:
            tags = [lang]
        self.engines = []
        for t in tags:
            eng = OcrEngine.try_create_from_language(Language(t))
            if eng is None:
                raise OcrUnavailable("OCR 언어 '%s'를 쓸 수 없습니다." % t)
            self.engines.append((t, eng))
        self.lang = "+".join(tags)
        self.english = any(t.lower().startswith("en") for t in tags)
        try:
            self.max_dim = int(OcrEngine.max_image_dimension)
        except Exception:
            self.max_dim = 10000
        self.loop = asyncio.new_event_loop()

    async def _rec(self, eng, bmp):
        return await eng.recognize_async(bmp)

    def _run(self, eng, bmp) -> List[List[Word]]:
        res = self.loop.run_until_complete(self._rec(eng, bmp))
        lines = []
        for line in res.lines:
            ws = []
            for wd in line.words:
                r = wd.bounding_rect
                ws.append((wd.text, float(r.x), float(r.y), float(r.width), float(r.height)))
            if ws:
                lines.append(ws)
        return lines

    def recognize(self, gray: bytes, w: int, h: int) -> List[List[Word]]:
        from winrt.windows.storage.streams import DataWriter
        from winrt.windows.graphics.imaging import SoftwareBitmap, BitmapPixelFormat
        dw = DataWriter()
        dw.write_bytes(gray)
        buf = dw.detach_buffer()
        bmp = SoftwareBitmap.create_copy_from_buffer(buf, BitmapPixelFormat.GRAY8, w, h)
        results = [self._run(eng, bmp) for _, eng in self.engines]
        if len(results) == 2:
            return merge_lines(results[0], results[1])
        return results[0]

    def close(self):
        try:
            self.loop.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------

_FONT = None


def _font():
    global _FONT
    if _FONT is None:
        _FONT = fitz.Font("cjk")       # built in: Korean + Latin glyphs
    return _FONT


def page_has_text(page) -> bool:
    return len("".join(page.get_text("text").split())) >= TEXT_MIN_CHARS


def render_gray(page, max_dim: int = 10000):
    r = page.rect
    long_side = max(r.width, r.height, 1.0)
    zoom = min(TARGET_PX / long_side, MAX_ZOOM, (max_dim - 8) / long_side)
    zoom = max(zoom, 0.5)
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csGRAY, alpha=False)
    w, h = pix.width, pix.height
    data = pix.samples
    if pix.stride != w:
        data = b"".join(data[i * pix.stride:i * pix.stride + w] for i in range(h))
    return bytes(data), w, h, zoom


def add_text_layer(page, lines: Sequence[Sequence[Word]], zoom: float) -> int:
    """Write OCR words as invisible text; returns the number of words."""
    font = _font()
    tw = fitz.TextWriter(page.rect)
    derot = page.derotation_matrix
    n = 0
    for line in lines:
        for k, (text, x, y, w, h) in enumerate(line):
            text = (text or "").strip()
            if not text or w <= 0 or h <= 0:
                continue
            x0, y0, ww, hh = x / zoom, y / zoom, w / zoom, h / zoom
            unit = font.text_length(text, fontsize=1)
            fs = ww / unit if unit > 0 else hh
            fs = min(max(fs, hh * 0.5), hh * 1.8)
            pos = fitz.Point(x0, y0 + hh * 0.85) * derot
            # trailing space keeps words apart for phrase search
            tw.append(pos, text + (" " if k < len(line) - 1 else ""), font=font, fontsize=fs)
            n += 1
    if n:
        tw.write_text(page, render_mode=3)
    return n


@dataclass
class Result:
    status: str          # "done" | "skip" | "exists"
    pages_ocr: int = 0
    pages: int = 0
    words: int = 0
    out: str = ""


def output_path(path: str, new_file: bool) -> str:
    if not new_file:
        return path
    stem, ext = os.path.splitext(path)
    return stem + SUFFIX + ext


def process_pdf(path: str, ocr, new_file: bool = False, force: bool = False,
                progress: Optional[Callable[[int, int], None]] = None,
                cancel=None) -> Result:
    out = output_path(path, new_file)
    if new_file and os.path.exists(out):
        return Result("exists", out=out)
    doc = fitz.open(path)
    try:
        if doc.needs_pass:
            raise ValueError("암호가 걸린 PDF입니다.")
        total = doc.page_count
        todo = [i for i in range(total) if force or not page_has_text(doc[i])]
        if not todo:
            return Result("skip", pages=total)
        words = 0
        for k, i in enumerate(todo):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            if progress:
                progress(k, len(todo))
            page = doc[i]
            data, w, h, zoom = render_gray(page, getattr(ocr, "max_dim", 10000))
            words += add_text_layer(page, ocr.recognize(data, w, h), zoom)
        if progress:
            progress(len(todo), len(todo))
        try:
            doc.subset_fonts()
        except Exception:
            pass
        tmp = out + ".pdf2ocr-tmp"
        doc.save(tmp, garbage=3, deflate=True)
    finally:
        doc.close()
    try:
        os.replace(tmp, out)
    except PermissionError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise PermissionError("파일이 다른 프로그램에서 열려 있습니다. 닫고 다시 시도하세요.")
    return Result("done", pages_ocr=len(todo), pages=total, words=words, out=out)


def dump_text(path: str) -> str:
    with fitz.open(path) as doc:
        return "\n".join("--- page %d ---\n%s" % (i + 1, p.get_text("text")) for i, p in enumerate(doc))
