"""Browser extension packager utility for Firefox (.xpi) and Chromium (.zip)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional
import zipfile

log = logging.getLogger(__name__)


def get_default_extension_dir() -> Path:
    """Return the absolute path to the bundled browser_extension directory."""
    return Path(__file__).resolve().parent.parent / "browser_extension"


def package_firefox_extension(
    extension_dir: Optional[Path] = None,
    output_path: Optional[Path] = None,
) -> Path:
    """
    Package the browser extension into a ready-to-install Firefox .xpi archive.

    If output_path is not specified, writes to <extension_dir>/my-idm-firefox.xpi.
    Uses manifest.firefox.json if available (written into the archive as manifest.json),
    ensuring standard Firefox MV3 compliance.
    """
    if extension_dir is None:
        extension_dir = get_default_extension_dir()

    if not extension_dir.is_dir():
        raise FileNotFoundError(f"Extension directory not found: {extension_dir}")

    if output_path is None:
        output_path = extension_dir / "my-idm-firefox.xpi"
    else:
        output_path = Path(output_path)

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Determine manifest source
    firefox_manifest = extension_dir / "manifest.firefox.json"
    fallback_manifest = extension_dir / "manifest.json"

    if firefox_manifest.is_file():
        manifest_src = firefox_manifest
    elif fallback_manifest.is_file():
        manifest_src = fallback_manifest
    else:
        raise FileNotFoundError(f"No manifest file found in {extension_dir}")

    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        # Write Firefox manifest as manifest.json at root
        zf.write(manifest_src, arcname="manifest.json")

        for file_path in extension_dir.iterdir():
            if file_path.is_file():
                if file_path.name in ("manifest.json", "manifest.firefox.json"):
                    continue
                if file_path.suffix.lower() in (".xpi", ".zip"):
                    continue
                zf.write(file_path, arcname=file_path.name)
            elif file_path.is_dir() and file_path.name == "icons":
                for icon_path in file_path.iterdir():
                    if icon_path.is_file():
                        zf.write(icon_path, arcname=f"icons/{icon_path.name}")

    log.info("Packaged Firefox extension (.xpi) at %s", output_path)
    return output_path
