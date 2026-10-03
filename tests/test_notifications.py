"""Tests for the notification backend chain in my_idm/notifications.py.

Two properties matter and neither is "a notification appeared":

* **No shell.** Title and message carry a download filename, which a crafted torrent controls.
  Both backends must build argv, and the AppleScript one must additionally escape for the
  language - an argv list stops the shell but not AppleScript.
* **A backend failure is never fatal.** The callers are download-completion paths; an exception
  escaping here would abort completion over a courtesy message.

`shutil.which` and `subprocess.run` are patched rather than allowed to run, so nothing reaches the
host's notification daemon.
"""

import unittest
from unittest.mock import patch

from my_idm import notifications as n


class TestPlatformDispatch(unittest.TestCase):
    def test_linux_uses_notify_send(self):
        with patch.object(n.sys, "platform", "linux"):
            key = "default"
            self.assertEqual(
                [b.__name__ for b in n._PLATFORM_FALLBACKS[key]],
                ["_notify_via_notify_send"],
            )

    def test_macos_uses_osascript(self):
        with patch.object(n.sys, "platform", "darwin"):
            self.assertEqual(
                [b.__name__ for b in n._PLATFORM_FALLBACKS["darwin"]],
                ["_notify_via_osascript"],
            )

    def test_windows_has_no_posix_fallback(self):
        """`notify-send` and `osascript` do not exist there.

        Trying them would spawn a process that cannot run and log a warning on every notification.
        """
        self.assertEqual(n._PLATFORM_FALLBACKS["win32"], ())

    def test_a_registered_handler_still_wins(self):
        """The tray is the primary path on every platform, and must stay first."""
        seen = []

        def handler(title, message, duration, icon_path):
            seen.append(title)
            return True

        n.register_notification_handler(handler)
        self.addCleanup(n.unregister_notification_handler)
        with patch.object(n, "_PLATFORM_FALLBACKS", {"default": (), "darwin": (), "win32": ()}):
            self.assertTrue(n.show_notification("t", "m"))
        self.assertEqual(seen, ["t"])


class TestNotifySend(unittest.TestCase):
    def _run(self, *, which="/usr/bin/notify-send", returncode=0, icon=None):
        with patch.object(n.shutil, "which", return_value=which), \
             patch.object(n.subprocess, "run") as run:
            run.return_value.returncode = returncode
            ok = n._notify_via_notify_send("Title", "Body", 5, icon)
        return ok, run

    def test_it_reports_success_on_zero_exit(self):
        ok, run = self._run()
        self.assertTrue(ok)
        self.assertTrue(run.called)

    def test_it_reports_failure_on_nonzero_exit(self):
        ok, _ = self._run(returncode=1)
        self.assertFalse(ok)

    def test_it_is_a_noop_when_notify_send_is_absent(self):
        """No `which` means no process and no warning - it is not an error."""
        ok, run = self._run(which=None)
        self.assertFalse(ok)
        run.assert_not_called()

    def test_it_never_uses_a_shell(self):
        """The message contains an attacker-influenced filename."""
        _, run = self._run()
        kwargs = run.call_args.kwargs
        self.assertNotIn("shell", kwargs)
        self.assertIsInstance(run.call_args.args[0], list)

    def test_a_hostile_filename_stays_one_argument(self):
        hostile = 'evil" $(id) `id` ; rm -rf /'
        with patch.object(n.shutil, "which", return_value="/usr/bin/notify-send"), \
             patch.object(n.subprocess, "run") as run:
            run.return_value.returncode = 0
            n._notify_via_notify_send("Title", hostile, 5, None)
        argv = run.call_args.args[0]
        self.assertIn(hostile, argv)
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_a_missing_icon_is_omitted_rather_than_passed(self):
        """notify-send treats a nonexistent icon path as a hard error, losing the notification."""
        with patch.object(n.shutil, "which", return_value="/usr/bin/notify-send"), \
             patch.object(n.subprocess, "run") as run:
            run.return_value.returncode = 0
            n._notify_via_notify_send("T", "B", 5, "/nonexistent/icon.png")
        self.assertFalse(
            [a for a in run.call_args.args[0] if a.startswith("--icon")]
        )

    def test_an_existing_icon_is_passed(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            icon = Path(tmp) / "i.png"
            icon.write_bytes(b"")
            with patch.object(n.shutil, "which", return_value="/usr/bin/notify-send"), \
                 patch.object(n.subprocess, "run") as run:
                run.return_value.returncode = 0
                n._notify_via_notify_send("T", "B", 5, str(icon))
        self.assertTrue(
            [a for a in run.call_args.args[0] if a.startswith("--icon")]
        )

    def test_a_timeout_is_contained(self):
        import subprocess as sp

        with patch.object(n.shutil, "which", return_value="/usr/bin/notify-send"), \
             patch.object(n.subprocess, "run", side_effect=sp.TimeoutExpired("x", 10)):
            self.assertFalse(n._notify_via_notify_send("T", "B", 5, None))


class TestOsascript(unittest.TestCase):
    def _run(self, *, which="/usr/bin/osascript", returncode=0):
        with patch.object(n.shutil, "which", return_value=which), \
             patch.object(n.subprocess, "run") as run:
            run.return_value.returncode = returncode
            ok = n._notify_via_osascript("Title", "Body", 5, None)
        return ok, run

    def test_it_reports_success_on_zero_exit(self):
        ok, _ = self._run()
        self.assertTrue(ok)

    def test_it_is_a_noop_when_osascript_is_absent(self):
        ok, run = self._run(which=None)
        self.assertFalse(ok)
        run.assert_not_called()

    def test_it_never_uses_a_shell(self):
        _, run = self._run()
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertIsInstance(run.call_args.args[0], list)

    def test_quotes_in_the_message_are_escaped(self):
        """argv blocks the shell but not AppleScript.

        The message is interpolated into AppleScript source, so an unescaped quote ends the string
        early and the remainder is parsed as code. A download filename can contain a quote.
        """
        hostile = 'evil" & (do shell script "id") & "'
        with patch.object(n.shutil, "which", return_value="/usr/bin/osascript"), \
             patch.object(n.subprocess, "run") as run:
            run.return_value.returncode = 0
            n._notify_via_osascript("Title", hostile, 5, None)
        script = run.call_args.args[0][2]
        self.assertIn('\\"', script)
        # Only the delimiters of the three literals survive unescaped: the notification body,
        # the title, and the "My-IDM" subtitle. Every quote the attacker supplied was escaped.
        self.assertEqual(
            script.count('"') - script.count('\\"'),
            6,
            "exactly three AppleScript string literals must remain delimited",
        )

    def test_backslashes_are_escaped(self):
        hostile = "path\\to\\file"
        with patch.object(n.shutil, "which", return_value="/usr/bin/osascript"), \
             patch.object(n.subprocess, "run") as run:
            run.return_value.returncode = 0
            n._notify_via_osascript("T", hostile, 5, None)
        script = run.call_args.args[0][2]
        self.assertIn("\\\\", script)

    def test_newlines_are_sanitized(self):
        hostile = "first line\r\nsecond line\nthird line\rend"
        with patch.object(n.shutil, "which", return_value="/usr/bin/osascript"), \
             patch.object(n.subprocess, "run") as run:
            run.return_value.returncode = 0
            n._notify_via_osascript("Title", hostile, 5, None)
        script = run.call_args.args[0][2]
        self.assertNotIn("\n", script)
        self.assertNotIn("\r", script)
        self.assertIn("first line second line third line end", script)


class TestFailureIsolation(unittest.TestCase):
    def test_a_raising_backend_does_not_propagate(self):
        """The callers are completion paths; a courtesy message must not abort one."""
        def boom(*_a, **_k):
            raise RuntimeError("backend exploded")

        n.register_notification_handler(None)
        with patch.object(n, "_PLATFORM_FALLBACKS", {"default": (boom,)}), \
             patch.object(n, "_HAS_WIN10TOAST", False), \
             patch.object(n, "_toaster", None):
            self.assertFalse(n.show_notification("T", "M"))


if __name__ == "__main__":
    unittest.main()
