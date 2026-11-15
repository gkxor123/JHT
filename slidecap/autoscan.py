"""Which videos the automatic mode (--auto) should convert.

Kept free of heavy imports (cv2, av) so it can be tested anywhere.

Rules
- A video that already has "<same name>.pptx" next to it is done.
- A *course folder* that already contains lecture notes (PPT, PDF, ...) is
  skipped as a whole. The course folder is the folder `course_depth` levels
  below the --auto root (default 2: <root>\\<subspecialty>\\<course>); a video
  closer to the root uses its own folder.
- A .pptx/.ppt that has the same name as a video in its folder is SlideCap's
  own output and does not count as lecture notes.
- Documents whose name says they are a program, notice or table of contents
  (프로그램, 안내, 목차, 일정, 시간표, program, schedule, agenda, ...) do not
  count as lecture notes either.
- A course folder containing a marker file named slidecap_변환.txt (or
  slidecap_convert.txt) is handled per video instead (for courses whose notes
  cover only some of the lectures): a video is converted unless a lecture
  note matches it by name (see matched_videos).
"""
import os
import re
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Sequence

VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".m4v", ".flv", ".ts",
    ".mts", ".m2ts", ".mpg", ".mpeg", ".3gp", ".asf", ".vob",
}
HANDOUT_EXTS = {".pdf", ".ppt", ".pptx", ".pps", ".ppsx", ".key", ".doc", ".docx", ".hwp", ".hwpx"}
DEFAULT_COURSE_DEPTH = 2
NOT_HANDOUT_WORDS = ("프로그램", "안내", "목차", "일정", "시간표", "초청", "등록",
                     "program", "schedule", "agenda", "timetable", "invitation", "brochure")
FORCE_FILES = ("slidecap_변환.txt", "slidecap_convert.txt")


def is_not_handout_name(name: str) -> bool:
    n = name.lower()
    return any(w in n for w in NOT_HANDOUT_WORDS)


def has_force_marker(folder: str) -> bool:
    try:
        names = {f.lower() for f in os.listdir(folder)}
    except OSError:
        return False
    return any(f.lower() in names for f in FORCE_FILES)


STOP_WORDS = {"of", "the", "and", "in", "a", "an", "for", "with", "to", "on", "at", "by",
              "final", "ver", "v", "pdf", "ppt", "pptx", "배포용", "강의록", "제출용"}


def _tokens(name: str) -> set:
    n = os.path.splitext(name)[0].lower()
    out = set()
    for t in re.split(r"[^0-9a-z가-힣]+", n):
        if not t or t.isdigit() or t in STOP_WORDS:
            continue
        if re.fullmatch(r"[가-힣]{2,4}", t):
            continue  # speaker name such as _김채리
        if len(t) > 3 and t.endswith("s"):
            t = t[:-1]
        out.add(t)
    return out


def _same(a: str, b: str) -> bool:
    return a == b or (len(a) >= 5 and len(b) >= 5 and SequenceMatcher(None, a, b).ratio() >= 0.85)


def _score(video_tokens: set, doc_tokens: set) -> float:
    """Share of the video title's words found in the document name."""
    if not video_tokens:
        return 0.0
    hit = sum(1 for t in video_tokens if any(_same(t, d) for d in doc_tokens))
    return hit / float(len(video_tokens))


def load_handout_log(path: Optional[str], root: str) -> Dict[str, List[str]]:
    """Lecture notes known from 01_Radiology (handout_seen.json, a list of
    "<sub>/<course>/<file>", or lecture_copy_log.json, keys "...|<target>").  Returns
    {course folder: [virtual paths of the notes]} so a course keeps counting as
    "has notes" even after its original notes were deleted."""
    if not path or not os.path.isfile(path):
        return {}
    import json
    try:
        with open(path, encoding="utf-8") as fh:
            log = json.load(fh)
    except (OSError, ValueError):
        return {}
    out: Dict[str, List[str]] = {}
    for key in log:              # dict (copy log) or list (handout_seen.json)
        rel = key.split("|")[0]
        parts = rel.split("/")
        if len(parts) < 3:
            continue
        name = parts[-1]
        stem, ext = os.path.splitext(name)
        if ext.lower() not in HANDOUT_EXTS or is_not_handout_name(stem):
            continue
        course = os.path.normcase(os.path.abspath(os.path.join(root, parts[0], parts[1])))
        out.setdefault(course, []).append(os.path.join(root, *parts))
    return out


def matched_videos(course: str, extra_docs: Sequence[str] = ()) -> Dict[str, str]:
    """Videos of a course that already have their own lecture note.

    Returns {video path: matching document path}.  A video and a document
    match when they are each other's best match by title words (at least
    60% of the video's words), or when a folder holds exactly one video
    plus documents (one lecture per folder)."""
    vids, docs = [], []
    for d, dirs, files in os.walk(course):
        dirs[:] = sorted(x for x in dirs if not _skip_dir(x))
        dv = [os.path.join(d, f) for f in sorted(files) if os.path.splitext(f)[1].lower() in VIDEO_EXTS]
        stems = {os.path.splitext(f)[0].lower() for f in files if os.path.splitext(f)[1].lower() in VIDEO_EXTS}
        dd = [os.path.join(d, f) for f in sorted(files)
              if os.path.splitext(f)[1].lower() in HANDOUT_EXTS and not f.startswith("~$")
              and not is_not_handout_name(os.path.splitext(f)[0])
              and not (os.path.splitext(f)[1].lower() in (".pptx", ".ppt") and os.path.splitext(f)[0].lower() in stems)]
        vids += dv
        docs += dd
    docs += [x for x in extra_docs if x not in docs]
    out: Dict[str, str] = {}
    by_dir: Dict[str, List[str]] = {}
    for v in vids:
        by_dir.setdefault(os.path.dirname(v), []).append(v)
    for d, vs in by_dir.items():
        if len(vs) == 1 and os.path.normcase(d) != os.path.normcase(course):
            same = [x for x in docs if os.path.dirname(x) == d]
            if same:
                out[vs[0]] = same[0]
    vt = {v: _tokens(os.path.basename(v)) for v in vids}
    dt = {x: _tokens(os.path.basename(x)) for x in docs}
    sc = {(v, x): _score(vt[v], dt[x]) for v in vids for x in docs}
    for v in vids:
        if v in out or not docs:
            continue
        best = max(docs, key=lambda x: sc[(v, x)])
        s = sc[(v, best)]
        if s < 0.6:
            continue
        # mutual best: no other video fits this document better
        if any(sc[(w, best)] > s for w in vids if w != v):
            continue
        out[v] = best
    return out


def _skip_dir(name: str) -> bool:
    return name.startswith((".", "$"))


def course_folder(video: str, root: str, depth: int = DEFAULT_COURSE_DEPTH) -> str:
    """Folder `depth` levels below root that contains the video."""
    v = os.path.abspath(video)
    r = os.path.abspath(root)
    rel = os.path.relpath(os.path.dirname(v), r)
    if rel == "." or rel.startswith(".."):
        return os.path.dirname(v)
    parts = rel.split(os.sep)
    return os.path.join(r, *parts[:depth])


def has_handout(folder: str) -> Optional[str]:
    """First lecture-notes file found below folder (not SlideCap output), else None."""
    for d, dirs, files in os.walk(folder):
        dirs[:] = sorted(x for x in dirs if not _skip_dir(x))
        stems = {os.path.splitext(f)[0].lower() for f in files
                 if os.path.splitext(f)[1].lower() in VIDEO_EXTS}
        for f in sorted(files):
            stem, ext = os.path.splitext(f)
            ext = ext.lower()
            if ext not in HANDOUT_EXTS or f.startswith("~$"):
                continue
            if ext in (".pptx", ".ppt") and stem.lower() in stems:
                continue  # our own output
            if is_not_handout_name(stem):
                continue  # program / notice / contents, not lecture notes
            return os.path.join(d, f)
    return None


def has_output(video: str) -> bool:
    return os.path.exists(os.path.splitext(os.path.abspath(video))[0] + ".pptx")


def scan(root: str, depth: int = DEFAULT_COURSE_DEPTH, handout_log: Optional[str] = None):
    """Return (videos_to_convert, skipped_courses {course: handout path}).

    handout_log: optional lecture_copy_log.json; notes listed there count
    even when the original files were deleted from the course folder."""
    logged = load_handout_log(handout_log, root)
    todo: List[str] = []
    skipped: Dict[str, str] = {}
    checked: Dict[str, Optional[str]] = {}
    covered: Dict[str, Dict[str, str]] = {}
    for d, dirs, files in os.walk(root):
        dirs[:] = sorted(x for x in dirs if not _skip_dir(x))
        for name in sorted(files):
            if os.path.splitext(name)[1].lower() not in VIDEO_EXTS:
                continue
            v = os.path.join(d, name)
            if has_output(v):
                continue
            c = course_folder(v, root, depth)
            if c not in checked:
                extra = logged.get(os.path.normcase(os.path.abspath(c)), [])
                if has_force_marker(c):
                    checked[c] = None
                    covered[c] = matched_videos(c, extra)
                else:
                    checked[c] = has_handout(c) or (extra[0] if extra else None)
                    if checked[c]:
                        skipped[c] = checked[c]
            if checked[c]:
                continue
            if v in covered.get(c, {}):
                continue  # this lecture has its own notes
            todo.append(os.path.abspath(v))
    return todo, skipped
