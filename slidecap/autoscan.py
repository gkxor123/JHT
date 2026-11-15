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
"""
import os
from typing import Dict, List, Optional

VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".m4v", ".flv", ".ts",
    ".mts", ".m2ts", ".mpg", ".mpeg", ".3gp", ".asf", ".vob",
}
HANDOUT_EXTS = {".pdf", ".ppt", ".pptx", ".pps", ".ppsx", ".key", ".doc", ".docx", ".hwp", ".hwpx"}
DEFAULT_COURSE_DEPTH = 2


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
            return os.path.join(d, f)
    return None


def has_output(video: str) -> bool:
    return os.path.exists(os.path.splitext(os.path.abspath(video))[0] + ".pptx")


def scan(root: str, depth: int = DEFAULT_COURSE_DEPTH):
    """Return (videos_to_convert, skipped_courses {course: handout path})."""
    todo: List[str] = []
    skipped: Dict[str, str] = {}
    checked: Dict[str, Optional[str]] = {}
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
                checked[c] = has_handout(c)
                if checked[c]:
                    skipped[c] = checked[c]
            if checked[c]:
                continue
            todo.append(os.path.abspath(v))
    return todo, skipped
