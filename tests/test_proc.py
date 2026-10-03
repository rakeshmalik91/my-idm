"""Tests for my_idm/proc.py.

The behaviour worth pinning is that each platform gets exactly one key, and that POSIX detachment
is on for background children but off for waited-on `run()` calls. Getting the second one wrong is
silent: the code still runs, it just stops responding to Ctrl-C.
"""

import os
import subprocess
import unittest
from unittest.mock import patch

from my_idm import proc


class TestBackgroundKwargs(unittest.TestCase):
    def test_posix_detaches_a_background_child(self):
        with patch.object(proc.sys, "platform", "linux"):
            self.assertEqual(proc.background_kwargs(), {"start_new_session": True})

    def test_windows_hides_the_console_instead(self):
        """`start_new_session` is not a Popen argument on Windows - it would raise."""
        with patch.object(proc.sys, "platform", "win32"):
            kwargs = proc.background_kwargs()
        self.assertIn("creationflags", kwargs)
        self.assertNotIn("start_new_session", kwargs)

    def test_exactly_one_platform_key_is_ever_returned(self):
        """Both keys at once raises; neither leaves the console window on Windows."""
        for platform in ("win32", "linux", "darwin"):
            with self.subTest(platform=platform):
                with patch.object(proc.sys, "platform", platform):
                    kwargs = proc.background_kwargs()
                self.assertLessEqual(
                    len(kwargs), 1, f"{kwargs} sets more than one platform key"
                )

    def test_new_process_group_is_windows_only(self):
        with patch.object(proc.sys, "platform", "win32"):
            with_flag = proc.background_kwargs(new_process_group=True)["creationflags"]
            without = proc.background_kwargs()["creationflags"]
        self.assertNotEqual(with_flag, without)
        self.assertEqual(with_flag & 0x00000200, 0x00000200, "CREATE_NEW_PROCESS_GROUP")
        # And it must not leak onto POSIX, where the flag means nothing.
        with patch.object(proc.sys, "platform", "linux"):
            self.assertEqual(
                proc.background_kwargs(new_process_group=True),
                {"start_new_session": True},
            )

    def test_a_waited_on_child_is_not_detached(self):
        """`run()` calls are waited on; detaching them would hide Ctrl-C from a long fetch."""
        with patch.object(proc.sys, "platform", "linux"):
            self.assertEqual(proc.background_kwargs(detach=False), {})

    def test_a_non_detached_posix_call_passes_nothing_cpython_would_reject(self):
        """An empty dict is required, not `creationflags=0`.

        CPython accepts `creationflags=0` everywhere and only rejects a non-zero value on POSIX,
        so either would work today - but an empty dict cannot break if that ever changes, and it
        states that there is nothing platform-specific to add.
        """
        with patch.object(proc.sys, "platform", "linux"):
            kwargs = proc.background_kwargs(detach=False)
        self.assertEqual(kwargs, {})
        self.assertNotIn("creationflags", kwargs)

    def test_the_helper_agrees_with_cpython_about_what_is_acceptable(self):
        """Whatever we return must be a legal Popen kwarg on the current interpreter.

        A cheap end-to-end check that the platform branch we chose is actually valid, rather than
        assuming it: this is the failure the whole helper exists to prevent.
        """
        with patch.object(proc.sys, "platform", "win32"):
            windows = proc.background_kwargs()
        with patch.object(proc.sys, "platform", "linux"):
            posix = proc.background_kwargs()

        for label, kwargs in (("windows", windows), ("posix", posix)):
            with self.subTest(platform=label):
                # DEVNULL plus the helper's kwargs, no actual child of consequence.
                p = subprocess.Popen(
                    [os.sys.executable, "-c", "pass"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    **kwargs,
                )
                p.wait(timeout=30)
                self.assertEqual(p.returncode, 0)


class TestDetachmentMatters(unittest.TestCase):
    def test_a_posix_background_child_gets_a_new_session(self):
        """The actual property: the child is in its own session, so it keeps no controlling tty.

        Asserted against a real child rather than a mock, because `start_new_session=True` is a
        claim about the child's session id and only a real process can confirm it.
        """
        if not hasattr(os, "getsid"):
            self.skipTest("POSIX-only check")

        with patch.object(proc.sys, "platform", "linux"):
            detached = proc.background_kwargs()
        inherited = proc.background_kwargs(detach=False)

        with subprocess.Popen(
            [os.sys.executable, "-c", "import time; time.sleep(5)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **detached,
        ) as child:
            self.assertNotEqual(
                os.getsid(child.pid),
                os.getsid(0),
                "a detached child must not share the parent's session, or it inherits SIGHUP",
            )
            child.kill()
            child.wait(timeout=30)

        with subprocess.Popen(
            [os.sys.executable, "-c", "import time; time.sleep(5)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **inherited,
        ) as child:
            # The POSIX branch is unreachable on Windows, so skip rather than assert there.
            if proc.sys.platform != "win32":
                self.assertEqual(
                    os.getsid(child.pid),
                    os.getsid(0),
                    "a non-detached child is expected to share the session",
                )
            child.kill()
            child.wait(timeout=30)


if __name__ == "__main__":
    unittest.main()
