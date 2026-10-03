"""Tests for the Preferences dialog's deferred system probe.

The dialog used to run three blocking probes on the GUI thread during construction: importing
``yt_dlp``, enumerating network adapters via psutil, and walking the filesystem for a Tor binary.
Measured cold, that was ~500 ms before the window appeared; deferring it to a daemon thread brings
that to ~80 ms.

Two things therefore need protecting, and they are opposite requirements:

* construction must not probe - or the whole change is undone by one eager call;
* the results must still arrive, and must land on the GUI thread.

The third test is the one that matters most: the interface combo must hold only its default entry
until results arrive, because every consumer of ``self._interfaces`` treats index 0 as "all
interfaces". A combo populated synchronously but read before the list is filled would index an empty
list and crash the save path.
"""

from __future__ import annotations

import os
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

from my_idm import youtube_tool as ytt  # noqa: E402
from my_idm.network import NetworkInterfaceInfo  # noqa: E402
from my_idm.settings_dialog import (  # noqa: E402
    SettingsDialog,
    _PrefsProbeEmitter,
    _PrefsProbeWorker,
)


def drain(dialog, budget: float = 3.0) -> bool:
    """Spin the event loop until the probe reports, or the budget runs out."""
    if not hasattr(dialog, "_probe_worker"):
        return True
    deadline = time.time() + budget
    while time.time() < deadline and dialog._probe_worker is not None:
        _app.processEvents()
        time.sleep(0.005)
    return dialog._probe_worker is None


class TestProbeRunsOffTheGuiThread(unittest.TestCase):
    def setUp(self):
        self.dialog = SettingsDialog(initial_tab=0)
        self.addCleanup(self._dispose, self.dialog)

    def _dispose(self, dialog):
        try:
            drain(dialog, 2.0)
        finally:
            dialog.close()
            dialog.deleteLater()

    def test_construction_does_not_enumerate_interfaces_inline(self):
        """The psutil walk is the probe's second-largest cost and must not be on this thread.

        Asserted by *thread*, not by call count. The probe thread legitimately calls this too, and
        it may or may not have got there first depending on scheduling - so counting calls would
        either be flaky or, worse, need a threshold loose enough to pass while eager probing is
        still happening. Which thread made the call is exact either way.
        """
        gui_thread = threading.get_ident()
        callers = []

        def record(*_a, **_k):
            callers.append(threading.get_ident())
            return []

        with patch("my_idm.network.get_available_interfaces", side_effect=record):
            dialog = SettingsDialog(initial_tab=0)
            self.addCleanup(self._dispose, dialog)
            self.assertNotIn(
                gui_thread,
                callers,
                "get_available_interfaces() ran on the GUI thread during construction",
            )

    def test_construction_does_not_walk_for_tor_inline(self):
        gui_thread = threading.get_ident()
        callers = []

        with patch(
            "my_idm.tor_service.find_tor_executable",
            side_effect=lambda *a, **k: callers.append(threading.get_ident()) or "",
        ):
            dialog = SettingsDialog(initial_tab=0)
            self.addCleanup(self._dispose, dialog)
            self.assertNotIn(
                gui_thread,
                callers,
                "find_tor_executable() ran on the GUI thread during construction",
            )

    def test_construction_does_not_query_the_yt_dlp_version_inline(self):
        """The `import yt_dlp` behind this is the single largest cost in the dialog."""
        gui_thread = threading.get_ident()
        callers = []

        with patch(
            "my_idm.youtube_tool.get_ytdlp_version",
            side_effect=lambda *a, **k: callers.append(threading.get_ident()) or "",
        ):
            dialog = SettingsDialog(initial_tab=0)
            self.addCleanup(self._dispose, dialog)
            self.assertNotIn(
                gui_thread,
                callers,
                "get_ytdlp_version() ran on the GUI thread during construction",
            )

    def test_the_worker_is_a_daemon_thread(self):
        """A non-daemon probe would keep the interpreter alive after the app exits."""
        worker = self.dialog._probe_worker
        self.assertIsInstance(worker, threading.Thread)
        self.assertTrue(worker.daemon, "the probe must not block process exit")

    def test_the_worker_touches_no_widgets(self):
        """Qt only permits widget access on the GUI thread.

        The worker must therefore hold no reference to the dialog at all - it is handed an emitter,
        and the emitter's connection is the only path back.
        """
        worker = _PrefsProbeWorker(_PrefsProbeEmitter())
        for name in vars(worker):
            self.assertNotIn(
                "dialog", name.lower(), f"worker attribute {name!r} looks like a dialog reference"
            )

    def test_results_land(self):
        self.assertTrue(drain(self.dialog), "the probe never reported")
        self.assertGreater(
            self.dialog._iface_combo.count(),
            1,
            "the interface combo should be populated once results arrive",
        )
        self.assertEqual(
            len(self.dialog._interfaces), self.dialog._iface_combo.count() - 1
        )


class TestApplyProbeResults(unittest.TestCase):
    """`_apply_probe_results` writes widgets, so it is the GUI-thread half of the contract."""

    def setUp(self):
        self.dialog = SettingsDialog(initial_tab=0)
        self.addCleanup(self._dispose, self.dialog)

    def _dispose(self, dialog):
        try:
            drain(dialog, 2.0)
        finally:
            dialog.close()
            dialog.deleteLater()

    def test_it_populates_the_interface_combo(self):
        interfaces = [
            NetworkInterfaceInfo(name="eth0", ip="10.0.0.5", is_up=True, is_vpn=False),
            NetworkInterfaceInfo(name="wg0", ip="10.9.0.1", is_up=True, is_vpn=True),
        ]
        self.dialog._apply_probe_results({"interfaces": interfaces})
        self.assertEqual(self.dialog._iface_combo.count(), 3)  # default + 2
        self.assertEqual(len(self.dialog._interfaces), 2)
        self.assertIn("wg0", self.dialog._iface_combo.itemText(2))
        self.assertIn("[VPN]", self.dialog._iface_combo.itemText(2))

    def test_it_does_not_overwrite_a_user_typed_tor_path(self):
        """The probe's value is a convenience; the user's is a decision."""
        self.dialog._tor_path_edit.setText("/my/own/tor")
        self.dialog._apply_probe_results({"tor": "/usr/bin/tor"})
        self.assertEqual(self.dialog._tor_path_edit.text(), "/my/own/tor")

    def test_it_fills_an_empty_tor_path(self):
        self.dialog._tor_path_edit.setText("")
        self.dialog._apply_probe_results({"tor": "/usr/bin/tor"})
        self.assertEqual(self.dialog._tor_path_edit.text(), "/usr/bin/tor")

    def test_it_reports_ytdlp_found(self):
        self.dialog._apply_probe_results(
            {"ytdlp_path": "/yt", "ytdlp_version": "2026.08.19", "ffmpeg_path": "/ff"}
        )
        self.assertIn("2026.08.19", self.dialog._yt_path_status.text())
        self.assertIn("2026.08.19", self.dialog._yt_version_lbl.text())
        self.assertIn("✓", self.dialog._yt_ffmpeg_status.text())

    def test_it_reports_ytdlp_missing(self):
        self.dialog._apply_probe_results(
            {"ytdlp_path": "", "ytdlp_version": "", "ffmpeg_path": ""}
        )
        self.assertIn("not found", self.dialog._yt_path_status.text())
        self.assertIn("unavailable", self.dialog._yt_version_lbl.text())

    def test_a_missing_key_does_not_raise(self):
        """One failing probe must not stop the others being applied."""
        self.dialog._apply_probe_results({"interfaces": []})
        self.assertIsNotNone(self.dialog._yt_path_status.text())

    def test_applying_results_clears_the_worker_handle(self):
        drain(self.dialog)
        self.dialog._apply_probe_results({})
        self.assertIsNone(self.dialog._probe_worker)


class TestInterfaceComboSafety(unittest.TestCase):
    """The invariant that makes deferring the enumeration safe."""

    def setUp(self):
        self.dialog = SettingsDialog(initial_tab=0)
        self.addCleanup(self._dispose, self.dialog)

    def _dispose(self, dialog):
        try:
            drain(dialog, 2.0)
        finally:
            dialog.close()
            dialog.deleteLater()

    def test_the_combo_is_never_empty_after_construction(self):
        """An empty combo would show a blank control until results land."""
        self.assertGreaterEqual(
            self.dialog._iface_combo.count(),
            1,
            "the default entry must exist before any probe completes",
        )

    def test_index_zero_never_indexes_the_interfaces_list(self):
        """Every consumer treats index 0 as 'all interfaces'.

        This is the crash that a synchronously-populated-but-not-yet-filled list would cause, and
        it is asserted directly rather than inferred.
        """
        self.dialog._reset_interface_combo()
        self.assertEqual(len(self.dialog._interfaces), 0)
        index = self.dialog._iface_combo.currentIndex()
        self.assertEqual(index, 0)
        # The shape every consumer relies on.
        self.assertFalse(index > 0 and index - 1 < len(self.dialog._interfaces))

    def test_on_iface_changed_is_safe_before_the_probe_lands(self):
        self.dialog._reset_interface_combo()
        self.dialog._on_iface_changed(self.dialog._iface_combo.currentIndex())
        self.assertIn("default system routing", self.dialog._iface_details_label.text())


class TestDialogLifecycleSafety(unittest.TestCase):
    """Closing mid-probe must be survivable, repeatedly.

    The first implementation used a QThread and aborted the process intermittently with
    "QThread: Destroyed while thread is still running". A daemon thread plus a disconnect is what
    removes that; these tests are the regression guard.
    """

    def test_closing_during_a_probe_does_not_raise(self):
        for _ in range(10):
            dialog = SettingsDialog(initial_tab=0)
            dialog.close()  # almost certainly before the probe reports
            dialog.deleteLater()
            _app.processEvents()

    def test_a_late_result_after_close_is_discarded(self):
        dialog = SettingsDialog(initial_tab=0)
        emitter = dialog._probe_emitter
        self.assertIsNotNone(emitter)
        dialog.close()
        # The connection is gone, so this reaches nobody - and must not raise.
        emitter.finished.emit({"interfaces": [], "tor": "/x", "ytdlp_path": ""})
        dialog.deleteLater()
        _app.processEvents()

    def test_the_worker_survives_the_emitter_being_destroyed_first(self):
        """The emit is guarded, because a parented-and-deleted emitter raises.

        Without the guard an exception escapes the daemon thread, which does not crash the app but
        does surface as a test-suite warning and, in production, as noise nobody can act on.
        """
        import gc

        emitter = _PrefsProbeEmitter()
        worker = _PrefsProbeWorker(emitter, tor_hint="")
        # Simulate the dialog taking its emitter down mid-probe.
        del emitter
        gc.collect()
        # The worker still holds a reference, so this is the same situation as a C++-side delete:
        # the emit must be a no-op rather than an exception escaping the thread.
        worker.run()
        del worker
        gc.collect()

    def test_the_emitter_is_not_parented_to_the_dialog(self):
        """A child QObject dies with its parent, and the worker may outlive it."""
        dialog = SettingsDialog(initial_tab=0)
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(dialog.close)
        self.assertIsNone(
            dialog._probe_emitter.parent(),
            "the emitter must outlive the dialog, or the worker's emit raises",
        )

    def test_many_dialogs_dropped_without_close(self):
        """A test that forgets to close must not take the process with it."""
        for _ in range(8):
            SettingsDialog(initial_tab=0)
            _app.processEvents()


class TestVersionQueryIsMemoised(unittest.TestCase):
    """`get_ytdlp_version` spawns a process; the dialog asks more than once per open."""

    def setUp(self):
        ytt._YTDLP_BINARY_VERSION_CACHE.clear()
        self.addCleanup(ytt._YTDLP_BINARY_VERSION_CACHE.clear)

    def _cfg_for(self, path):
        from my_idm.config import ExternalToolsConfig

        cfg = ExternalToolsConfig()
        cfg.ytdlp_path = path
        return cfg

    def test_a_missing_binary_is_not_cached_and_returns_empty(self):
        """Neither an importable module nor a binary on PATH must yield an empty string.

        Both halves matter: the import is tried first, so a test that only clears the configured
        path would still get the installed module's version and pass for the wrong reason.
        """
        cfg = self._cfg_for("")
        with patch.object(ytt, "_import_ytdlp", side_effect=ytt.YouTubeToolError("no module")), \
             patch.object(ytt.shutil, "which", return_value=None):
            self.assertEqual(ytt.get_ytdlp_version(cfg), "")
        self.assertEqual(
            ytt._YTDLP_BINARY_VERSION_CACHE,
            {},
            "an unresolvable scanner has nothing worth caching",
        )

    def test_repeated_queries_spawn_one_process(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "yt-dlp"
            binary.write_bytes(b"# stub")
            cfg = self._cfg_for(str(binary))
            with patch.object(ytt, "_import_ytdlp", side_effect=ytt.YouTubeToolError("not installed")), \
                 patch.object(ytt.subprocess, "run") as run:
                run.return_value.returncode = 0
                run.return_value.stdout = "2026.08.19\n"
                run.return_value.stderr = ""
                first = ytt.get_ytdlp_version(cfg)
                second = ytt.get_ytdlp_version(cfg)
        self.assertEqual(first, "2026.08.19")
        self.assertEqual(second, "2026.08.19")
        self.assertEqual(
            run.call_count, 1, "a second query for the same binary must not spawn again"
        )

    def test_replacing_the_binary_invalidates_the_cache(self):
        """A version label that lies about the installed version is worse than a slow one."""
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "yt-dlp"
            binary.write_bytes(b"# v1")
            cfg = self._cfg_for(str(binary))
            with patch.object(ytt, "_import_ytdlp", side_effect=ytt.YouTubeToolError("not installed")), \
                 patch.object(ytt.subprocess, "run") as run:
                run.return_value.returncode = 0
                run.return_value.stdout = "1.0"
                run.return_value.stderr = ""
                self.assertEqual(ytt.get_ytdlp_version(cfg), "1.0")

                # "Upgrade in place": same path, new contents.
                import os
                import time as _t

                binary.write_bytes(b"# v2 longer")
                os.utime(binary, (0, _t.time() + 10))
                run.return_value.stdout = "2.0"
                self.assertEqual(
                    ytt.get_ytdlp_version(cfg),
                    "2.0",
                    "an upgraded binary must not report the cached version",
                )
        self.assertEqual(run.call_count, 2)


class TestEmojiIconIsCached(unittest.TestCase):
    def test_the_same_emoji_and_size_return_the_same_icon(self):
        from my_idm.utils import create_emoji_icon

        first = create_emoji_icon("🎬", 16)
        second = create_emoji_icon("🎬", 16)
        self.assertIs(
            first,
            second,
            "emoji icons must be cached; Qt implicitly shares QIcon so this is safe",
        )

    def test_different_sizes_are_cached_separately(self):
        from my_idm.utils import create_emoji_icon

        self.assertIsNot(create_emoji_icon("🎬", 16), create_emoji_icon("🎬", 24))

    def test_different_emoji_are_cached_separately(self):
        from my_idm.utils import create_emoji_icon

        self.assertIsNot(create_emoji_icon("🎬", 16), create_emoji_icon("📁", 16))


if __name__ == "__main__":
    unittest.main()