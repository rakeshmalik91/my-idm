"""Platform-correct keyword arguments for launching detached background processes.

Background children here are long-lived and outlive the operation that started them: the Tor
proxy, the AnimePahe scraper, the AnimePahe GUI. What they must *not* do is inherit the launching
process's fate by accident.

On Windows that is already handled and always was: the callers pass ``creationflags``, and CPython
accepts the keyword on every platform, raising only for a non-zero value on POSIX. So the Windows
path was never at risk and the ``if os.name == "nt"`` guards around it were harmless.

On POSIX nothing set the equivalent. ``start_new_session`` was absent everywhere, so every one of
these children sat in My-IDM's own process group and shared its terminal:

* closing the terminal My-IDM was started from delivered SIGHUP to the Tor process, so the proxy
  died with the shell - silently, mid-download;
* Ctrl-C reached children that were never asked to be interruptible.

Neither is a hypothetical on a desktop, where launching from a terminal is normal.

One function rather than a package: this is a single question with a two-line answer, and the
callers are four ``Popen`` calls. ``my_idm/autostart.py`` sets the precedent for a flat,
self-contained module - but note that it owns a *registration* mechanism per platform, which
genuinely differs, whereas this is one flag with no per-platform nuance.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any, Dict


def background_kwargs(
    *, detach: bool = True, new_process_group: bool = False
) -> Dict[str, Any]:
    """Kwargs for launching a silent process, optionally detached from the session.

    Args:
        detach: POSIX-only. ``setsid()`` gives the child its own session, so it has no controlling
            terminal and does not receive SIGHUP when one closes. Right for a process that must
            outlive the terminal - the Tor proxy, the AnimePahe scraper - and wrong for a
            ``run()`` call that is waited on: there, losing the terminal means Ctrl-C no longer
            reaches a long fetch, which is more surprising than inheriting the signal.
        new_process_group: Windows-only. Requests ``CREATE_NEW_PROCESS_GROUP``, which changes how
            Ctrl-C is delivered to the child. Set it for children the user is not expected to
            interrupt with the keyboard; leave it off for ones they are.

    Returns:
        A dict to splat into ``subprocess.Popen`` or ``subprocess.run``. Always contains at most
        one platform key (one on Windows, at most one on POSIX depending on ``detach``), so a
        caller can never end up passing conflicting platform flags.
    """
    if sys.platform == "win32":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        if new_process_group:
            flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        return {"creationflags": flags}
    if detach:
        return {"start_new_session": True}
    # Nothing platform-specific to add: CREATE_NO_WINDOW has no POSIX analogue and
    # `creationflags` is only rejected by CPython when non-zero.
    return {}
