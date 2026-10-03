"""Assert the antivirus scanner fails *closed*: no scanner means no clean verdict.

Run in CI on Linux and macOS. This is the single security property that cannot be verified from
Windows, because on Windows Defender is present and the interesting branch is never taken.

`scan_file` used to return `True` ("clean") for every failure mode: no scanner installed, a
misconfigured custom scanner path, a timeout, a crash, an unrecognised exit code. On any machine
without Defender - that is, every Linux and macOS machine - every post-download scan therefore
reported success for a file nothing had inspected, and the Details Panel showed a green
"Clean (Scanned)".

The verdict must be `None`, and specifically:

* not `True`  - that is the bug this guards;
* not `False` - a missing scanner is not a threat. `None` is falsy, so a call site written as
  `if not verdict:` rather than `if verdict is False:` would route it into quarantine and, with
  `action_on_threat == "delete"`, delete the user's downloads.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from my_idm.security import SecurityConfig, scan_file


def _verdict(config: SecurityConfig) -> tuple[object, str]:
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "payload.bin"
        target.write_bytes(b"data")
        with patch("my_idm.security.find_windows_defender_path", return_value=None):
            return scan_file(str(target), config)


def main() -> int:
    failures: list[str] = []

    # 1. No scanner at all - the Defender default on a POSIX host.
    verdict, report = _verdict(SecurityConfig(scan_after_download=True))
    if verdict is not None:
        failures.append(
            f"missing Defender: expected None, got {verdict!r} (report: {report!r})"
        )
    if "not scanned" not in report.lower():
        failures.append(f"missing Defender: report does not say it was not scanned: {report!r}")

    # 2. A configured custom scanner that is not there - the misconfiguration case.
    verdict, report = _verdict(
        SecurityConfig(
            scan_after_download=True,
            scanner_type="custom",
            custom_scanner_path="/nonexistent/clamscan",
        )
    )
    if verdict is not None:
        failures.append(
            f"missing custom scanner: expected None, got {verdict!r} (report: {report!r})"
        )

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1

    print("OK: a scanner that cannot run yields no verdict (None), not a clean one")
    return 0


if __name__ == "__main__":
    sys.exit(main())
