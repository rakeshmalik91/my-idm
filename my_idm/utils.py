"""General utilities for My-IDM."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Set


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
