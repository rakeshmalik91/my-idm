"""General utilities for My-IDM."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional, Set


def to_int(value: Any, default: int = 0) -> int:
    """Safely coerce *value* to int.

    Lists, tuples, sets, and dicts evaluate to their length.
    Invalid strings or non-numeric types return `default`.
    """
    if isinstance(value, int):
        return value
    if isinstance(value, (list, tuple, set, dict)):
        return len(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def get_unique_filename(
    directory: str | Path,
    filename: str,
    reserved_names: Optional[Set[str]] = None,
) -> str:
    """Generate a unique filename by appending (1), (2), etc. if the filename already exists.

    Checks:
      1. If candidate is in reserved_names (case-insensitive on Windows).
      2. If candidate physically exists on disk in `directory`.

    Preserves compound extensions such as .tar.gz, .tar.bz2, .tar.xz, .tar.zst.
    If the filename already ends in (N), increments from N+1 instead of nesting parentheses.
    """
    if not filename:
        return filename

    directory = Path(directory)
    reserved = {n.lower() for n in reserved_names} if reserved_names else set()

    def is_taken(name: str) -> bool:
        if name.lower() in reserved:
            return True
        try:
            return (directory / name).exists()
        except OSError:
            return False

    if not is_taken(filename):
        return filename

    # Identify compound extensions
    lower_fn = filename.lower()
    ext = ""
    for compound in (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst"):
        if lower_fn.endswith(compound):
            ext = filename[-len(compound):]
            stem = filename[:-len(compound)]
            break
    else:
        path_obj = Path(filename)
        ext = path_obj.suffix
        stem = path_obj.stem

    # If stem already has a trailing (N) like "file (1)", continue from N+1
    match = re.match(r"^(.*?)\s*\((\d+)\)$", stem)
    if match:
        base_stem = match.group(1).rstrip()
        counter = int(match.group(2)) + 1
    else:
        base_stem = stem
        counter = 1

    while True:
        candidate = f"{base_stem} ({counter}){ext}"
        if not is_taken(candidate):
            return candidate
        counter += 1


def normalize_path(path: str | Path | None) -> str:
    """Standardize file and directory paths with forward slash separators."""
    if not path:
        return ""
    p_str = str(path).strip()
    if not p_str:
        return ""
    return p_str.replace("\\", "/")


def create_emoji_icon(emoji: str, size: int = 32):
    """Create a high-DPI QIcon containing the specified emoji."""
    from PySide6.QtCore import Qt, QRect
    from PySide6.QtGui import QIcon, QPixmap, QPainter, QFont

    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    font = QFont(["Segoe UI Emoji", "Noto Color Emoji", "Apple Color Emoji", "sans-serif"])
    font.setPixelSize(int(size * 0.65))
    p.setFont(font)
    p.drawText(QRect(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, emoji)
    p.end()
    return QIcon(pix)


def extract_source_domain(url: str) -> str:
    """Extract clean source website domain from a download URL or magnet link."""
    if not url:
        return ""
    url_clean = url.strip()
    if url_clean.startswith("magnet:"):
        try:
            from urllib.parse import parse_qs, urlparse
            parsed = urlparse(url_clean)
            qs = parse_qs(parsed.query)
            for key in ("ws", "as"):
                for direct_url in qs.get(key, []):
                    direct_host = urlparse(direct_url).hostname
                    if direct_host:
                        return direct_host.removeprefix("www.")
            for tr in qs.get("tr", []):
                tr_host = urlparse(tr).hostname
                if tr_host and not tr_host.startswith("127.") and tr_host != "localhost":
                    return tr_host.removeprefix("www.")
        except Exception:
            pass
        return ""

    if "://" in url_clean:
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url_clean)
            host = parsed.hostname or parsed.netloc
            if host:
                return host.removeprefix("www.")
        except Exception:
            pass
    return ""


def send_to_trash(file_path: str | Path) -> bool:
    """Move a file or directory to the system trash / recycle bin.

    Attempts to move the target to trash using:
      1. Qt's native QFile.moveToTrash (supported cross-platform on Windows, macOS, Linux).
      2. send2trash library (if available).
      3. Fallback to permanent deletion if trash mechanisms are unsupported on the filesystem.

    Returns True if the target path no longer exists at file_path, False otherwise.
    """
    if not file_path:
        return False
    fp = Path(file_path)
    if not fp.exists():
        return True

    # 1. Try PySide6 / Qt native QFile.moveToTrash
    try:
        from PySide6.QtCore import QFile
        path_str = str(fp)
        # Try both native and normalized string representations
        if QFile.moveToTrash(path_str) or QFile.moveToTrash(path_str.replace("/", "\\")):
            if not fp.exists():
                return True
    except Exception:
        pass

    # 2. Try send2trash if available
    try:
        import send2trash
        send2trash.send2trash(str(fp))
        if not fp.exists():
            return True
    except Exception:
        pass

    # 3. Fallback to permanent deletion if trash is not supported
    import shutil
    import time
    for attempt in range(5):
        try:
            if fp.is_dir():
                shutil.rmtree(fp)
            else:
                fp.unlink()
            return True
        except OSError:
            time.sleep(0.1)

    return not fp.exists()

