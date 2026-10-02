"""General utilities for My-IDM."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional, Set

# Reserved device names on Windows that cannot be used as filenames.
_WINDOWS_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}

_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE_RUN = re.compile(r"\s+")
_MAX_FILENAME_STEM = 150
# A dotted tail longer than this is treated as part of the name, not an
# extension ("Arr. by J. Halvorsen [PIANO COVER]" must not look like ".Halvorsen [...]").
_MAX_EXTENSION_LENGTH = 12


def split_extension(name: str) -> tuple[str, str]:
    """Split *name* into ``(stem, extension)`` using a conservative extension rule.

    Only a short, trailing dotted token is treated as an extension, so titles
    containing dots are not truncated.
    """
    text = str(name or "")
    if "." not in text:
        return text, ""
    stem, _, ext = text.rpartition(".")
    if not stem or len(ext) > _MAX_EXTENSION_LENGTH:
        return text, ""
    return stem, ext


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


def sanitize_filename(name: str, max_length: int = 255, fallback: str = "download") -> str:
    """Return a filesystem-safe filename derived from *name*.

    Strips characters that are illegal on Windows or POSIX filesystems,
    collapses whitespace, avoids reserved device names, and truncates the
    stem while always preserving the extension.
    """
    raw = (name or "").strip()
    if not raw:
        return fallback

    cleaned = _INVALID_FILENAME_CHARS.sub("_", raw)
    cleaned = _WHITESPACE_RUN.sub(" ", cleaned).strip().strip(".")
    if not cleaned:
        return fallback

    stem, extension = split_extension(cleaned)

    if stem.lower() in _WINDOWS_RESERVED_NAMES:
        stem = f"_{stem}"

    max_stem = max(1, min(_MAX_FILENAME_STEM, max_length - (len(extension) + 1 if extension else 0)))
    if len(stem) > max_stem:
        stem = stem[:max_stem].rstrip(" .")

    if not stem:
        stem = fallback
    return f"{stem}.{extension}" if extension else stem


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


def create_color_swatch_icon(color: str, size: int = 18, radius: int = 4, letter: str = ""):
    """Create a high-DPI QIcon containing a rounded rectangle color swatch with optional letter."""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap

    scale = 2
    pix = QPixmap(size * scale, size * scale)
    pix.fill(Qt.GlobalColor.transparent)
    if not color:
        return QIcon(pix)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    p.setBrush(QColor(color))
    p.setPen(QPen(QColor(0, 0, 0, 110), 1 * scale))
    p.drawRoundedRect(
        QRectF(scale, scale, (size - 2) * scale, (size - 2) * scale),
        radius * scale,
        radius * scale,
    )

    char = (letter or "").strip()[:1].upper()
    if char:
        font = QFont("Segoe UI", int(size * scale * 0.52), QFont.Weight.Bold)
        p.setFont(font)
        qc = QColor(color)
        luminance = (0.299 * qc.red() + 0.587 * qc.green() + 0.114 * qc.blue()) / 255.0
        text_color = QColor(0, 0, 0, 220) if luminance > 0.65 else QColor(255, 255, 255, 240)
        p.setPen(text_color)
        rect = QRectF(0, 0, size * scale, size * scale)
        p.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), char)
    p.end()
    pix.setDevicePixelRatio(scale)
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


def get_free_disk_space(path: str | Path) -> int:
    """Free bytes on the volume that will hold *path*, or 0 if it cannot be determined.

    Walks up to the nearest existing ancestor, because the save directory is often created
    on demand and a fresh torrent folder may not exist yet - ``shutil.disk_usage`` on a
    missing path raises, and the answer we want is the volume's, not the directory's.

    Returns 0 for "unknown" rather than raising: a caller that cannot measure free space
    must not block a download, it must let it proceed. The same convention as the
    ``0 = unlimited`` speed limit.
    """
    import shutil

    if not path or not str(path).strip():
        # A blank target is unknown, not "the current directory" - Path("") is Path(".")
        # and quietly measuring the CWD would be a surprising thing for a blank argument
        # to do.
        return 0
    try:
        candidate = Path(path)
        if candidate.is_dir():
            return int(shutil.disk_usage(str(candidate)).free)
        # A file path, or one that does not exist yet: start at the parent and walk up
        # until there is something existing to measure.
        probe = candidate.parent if candidate.suffix else candidate
        for _ in range(64):
            if probe.exists():
                return int(shutil.disk_usage(str(probe)).free)
            parent = probe.parent
            if parent == probe:
                break
            probe = parent
    except Exception:
        return 0
    return 0


def check_disk_space(
    target_path: str | Path,
    needed_bytes: int,
    headroom_bytes: int = 0,
) -> tuple[bool, int, int]:
    """Is there room for *needed_bytes* more on the volume holding *target_path*?

    Returns ``(ok, free_bytes, shortfall_bytes)``. ``ok`` is True whenever free space cannot
    be measured: refusing a download because we could not read a number would be worse than
    letting it try. ``headroom_bytes`` is slack on top of the requirement, so a download that
    exactly fills the volume is not started - the page file, the recycle bin and whatever
    else shares the disk all want room, and a volume at 100% stalls unrelated work.
    """
    free = get_free_disk_space(target_path)
    if free <= 0:
        return True, 0, 0
    if needed_bytes <= 0:
        return True, free, 0
    shortfall = int(needed_bytes) + max(0, int(headroom_bytes)) - free
    return shortfall <= 0, free, max(0, shortfall)


def unlock_path(file_path: str | Path) -> None:
    """Attempt to unlock a file or directory on disk by clearing read-only/system flags and running GC."""
    if not file_path:
        return
    import gc
    import os
    import stat
    gc.collect()
    try:
        p = Path(file_path)
        if not p.exists():
            return
        try:
            os.chmod(str(p), stat.S_IWRITE | stat.S_IREAD)
        except Exception:
            pass
        if p.is_dir():
            for root, dirs, files in os.walk(str(p)):
                for d in dirs:
                    try:
                        os.chmod(os.path.join(root, d), stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
                    except Exception:
                        pass
                for f in files:
                    try:
                        os.chmod(os.path.join(root, f), stat.S_IWRITE | stat.S_IREAD)
                    except Exception:
                        pass
    except Exception:
        pass


def robust_move_download_files(src: str | Path, dst: str | Path) -> tuple[bool, str]:
    """Robustly moves a file or directory tree from src to dst.

    Handles:
      - File permission unlocking before and during move.
      - Partial moves from previous failed attempts (merges files into dst without duplicate errors).
      - Directory tree moves across drives or within the same filesystem.
      - Retries on transient Windows file lock errors (ERROR_SHARING_VIOLATION).
    Returns (success: bool, error_message: str).
    """
    import os
    import shutil
    import time
    if not src or not dst:
        return False, "Invalid source or destination path."
    sp = Path(src)
    dp = Path(dst)

    if not sp.exists():
        if dp.exists():
            # Already moved to destination
            return True, ""
        return False, f"Source path does not exist: {sp}"

    if sp.resolve() == dp.resolve():
        return True, ""

    unlock_path(sp)
    if dp.exists():
        unlock_path(dp)

    dp.parent.mkdir(parents=True, exist_ok=True)

    if sp.is_file():
        # Single file move
        last_exc = None
        for attempt in range(5):
            try:
                unlock_path(sp)
                if dp.exists():
                    unlock_path(dp)
                    dp.unlink()
                shutil.move(str(sp), str(dp))
                return True, ""
            except (OSError, PermissionError) as exc:
                last_exc = exc
                time.sleep(0.1 * (attempt + 1))
        # Fallback to copy + unlink
        try:
            shutil.copy2(str(sp), str(dp))
            unlock_path(sp)
            sp.unlink()
            return True, ""
        except Exception as exc:
            return False, f"Failed to move file from {sp} to {dp}: {last_exc or exc}"

    # Directory move (multi-file torrent or folder)
    dp.mkdir(parents=True, exist_ok=True)
    failed_files = []

    # Walk all files in source
    for root, _, files in os.walk(str(sp)):
        for f in files:
            src_f = Path(root) / f
            rel_f = src_f.relative_to(sp)
            dst_f = dp / rel_f
            dst_f.parent.mkdir(parents=True, exist_ok=True)

            # Check if destination file already exists and has identical size (e.g. from prior partial move)
            if dst_f.exists() and dst_f.is_file():
                try:
                    if dst_f.stat().st_size == src_f.stat().st_size:
                        # Same length is not proof of the same content: a previous attempt
                        # that died mid-copy can leave a target truncated to exactly the
                        # source's length. Deleting the source on size alone would then
                        # throw away the only good copy. Verify the bytes - sampled, so the
                        # check stays O(1) on a multi-gigabyte file - and only skip the move
                        # when the two really are the same file.
                        if _same_file_content(src_f, dst_f):
                            unlock_path(src_f)
                            src_f.unlink()
                            continue
                except Exception:
                    pass

            moved = False
            for attempt in range(5):
                try:
                    unlock_path(src_f)
                    if dst_f.exists():
                        unlock_path(dst_f)
                        dst_f.unlink()
                    shutil.move(str(src_f), str(dst_f))
                    moved = True
                    break
                except (OSError, PermissionError):
                    time.sleep(0.1 * (attempt + 1))

            if not moved:
                # Fallback to copy + unlink
                try:
                    shutil.copy2(str(src_f), str(dst_f))
                    unlock_path(src_f)
                    src_f.unlink()
                    moved = True
                except Exception as exc:
                    failed_files.append((str(rel_f), str(exc)))

    # Clean up empty directories in source
    for root, dirs, _ in os.walk(str(sp), topdown=False):
        for d in dirs:
            try:
                (Path(root) / d).rmdir()
            except Exception:
                pass
    try:
        sp.rmdir()
    except Exception:
        pass

    if failed_files:
        err_details = "; ".join(f"{f}: {err}" for f, err in failed_files[:3])
        return False, f"{len(failed_files)} file(s) failed to move: {err_details}"

    return True, ""


def _same_file_content(a: Path, b: Path, sample: int = 1 << 20) -> bool:
    """Cheap content check for two files of equal length.

    Compares the first and last *sample* bytes rather than the whole file: the realistic
    failure is a copy that was truncated or never finished, which changes the tail or leaves
    a run of zeros, and a full read of a 4 GB payload on every resume attempt would be far
    too expensive. Returns ``False`` on any I/O error so the caller falls back to a real
    move rather than skipping one.
    """
    try:
        size = a.stat().st_size
        if size == 0:
            return True
        with open(a, "rb") as fa, open(b, "rb") as fb:
            if fa.read(sample) != fb.read(sample):
                return False
            if size > sample:
                fa.seek(-min(sample, size), 2)
                fb.seek(-min(sample, size), 2)
                if fa.read() != fb.read():
                    return False
        return True
    except OSError:
        return False


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

    unlock_path(fp)

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
            unlock_path(fp)
            if fp.is_dir():
                shutil.rmtree(fp)
            else:
                fp.unlink()
            return True
        except OSError:
            time.sleep(0.1)

    return not fp.exists()


