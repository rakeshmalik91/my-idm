"""Verify `pyproject.toml` and `requirements.txt` declare the same requirements.

Run in CI on all three platforms. The two files drifted before Phase 0, and the drift was not
cosmetic: `win10toast` was present only in `requirements.txt` and unmarked, which does not get
skipped off Windows - pip cannot resolve it at all, so `pip install -r requirements.txt` failed
outright. `psutil` was in only one file and `aiohttp-socks` in only the other, so whichever way a
user installed, something was silently missing.

Markers are compared as text after the name, with quote style normalised, because the two files
were written by hand and `'win32'` vs `"win32"` is not a difference worth failing a build over.
"""

from __future__ import annotations

import pathlib
import re
import sys


def _normalise(spec: str) -> str:
    """`name[extras]op>=v ; marker` -> `nameop>=v marker`, quotes normalised."""
    name_part, _, marker = spec.partition(";")
    name_part = re.split(r"[<>=!~\[ ]", name_part.strip(), maxsplit=1)[0]
    marker = marker.strip().replace("'", '"').replace(" ", "")
    return f"{name_part.lower()}{marker}"


def _from_pyproject(text: str) -> set[str]:
    block = text.split("dependencies = [", 1)[1].split("]", 1)[0]
    return {_normalise(d) for d in re.findall(r'"([^"]+)"', block)}


def _from_requirements(text: str) -> set[str]:
    out = set()
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if stripped:
            out.add(_normalise(stripped))
    return out


def main() -> int:
    root = pathlib.Path(__file__).resolve().parent.parent
    pyproject = _from_pyproject((root / "pyproject.toml").read_text(encoding="utf-8"))
    requirements = _from_requirements((root / "requirements.txt").read_text(encoding="utf-8"))

    only_pp = sorted(pyproject - requirements)
    only_req = sorted(requirements - pyproject)

    print(f"pyproject.toml   : {len(pyproject)} requirements")
    print(f"requirements.txt : {len(requirements)} requirements")
    if not only_pp and not only_req:
        print("OK: manifests agree, including markers")
        return 0

    if only_pp:
        print(f"ERROR in requirements.txt: {only_pp}")
    if only_req:
        print(f"ERROR in pyproject.toml  : {only_req}")
    print(
        "\nBoth files are documented install paths (README.md and docs/user-guide.md point at\n"
        "requirements.txt), so a requirement in only one of them is missing for whoever uses\n"
        "the other."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
