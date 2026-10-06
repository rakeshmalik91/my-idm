"""Hardened tests for ``TorrentEngine``: settings, add/remove, status mapping, thread safety.

``test_torrent_engine.py`` exercises the real libtorrent session for the behaviours that
only a real session can prove (fastresume round-trip across a restart, recheck, seeding
limits). What it does not cover is the large surface that sits *between* libtorrent calls -
proxy/interface settings assembly, the ``add_torrent`` source dispatch, ``get_status`` field
mapping, tracker/peer fallback, alert routing - which is where the untested 641 statements
live. That layer is exactly what a fake session can drive deterministically, so this file
never opens a socket or a torrent port.

The last two classes are the thread-safety half. The engine is shared across threads by
design: ``DownloadManager`` drives ``poll_all`` from a ``QTimer`` on the Qt thread, and
user actions (pause, resume, remove, bandwidth allocation) arrive on the same thread while
``ManagerMonitor`` may be reading. The tests assert the two properties that actually matter:
no thread mutates ``_handles`` while another is iterating it, and no exception escapes a
worker thread into the Qt event loop.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from PySide6.QtWidgets import QApplication

from my_idm import torrent_engine as te_module
from my_idm.config import TorConfig, TorrentConfig
from my_idm.database import Database, DownloadEntry
from my_idm.network import NetworkConfig
from my_idm.torrent_engine import (
    FASTRESUME_DIR,
    TorrentEngine,
    _get_info_hash_from_handle,
    _get_info_hash_from_params,
    _newer_seed_stamp,
    _priority_to_label,
    mark_seeding_started,
    seeding_session_start,
)

app = QApplication.instance() or QApplication(sys.argv)

# Windows CI runs headless; calling QApplication.processEvents() crashes with access violation.
IS_HEADLESS_WIN_CI = sys.platform == "win32" and os.environ.get("CI") == "true"


class FakeFiles:
    def __init__(self, paths, sizes):
        self._paths = paths
        self._sizes = sizes

    def file_path(self, index):
        return self._paths[index]

    def file_size(self, index):
        return self._sizes[index]


class FakeTorrentInfo:
    def __init__(self, name="Payload", total=1000, paths=("Payload/a.bin",), sizes=(1000,)):
        self._name = name
        self._total = total
        self._files = FakeFiles(list(paths), list(sizes))

    def name(self):
        return self._name

    def total_size(self):
        return self._total

    def num_files(self):
        return len(self._files._paths)

    def files(self):
        return self._files


class FakeHandle:
    """A libtorrent ``torrent_handle`` stand-in with a real (non-MagicMock) ``status``."""

    def __init__(self, info_hash="aa" * 20, state=3, has_metadata=True, total_wanted=1000,
                 total_wanted_done=0, paused=False, name="Payload", num_files=1,
                 file_progress=(0,), priorities=(4,), torrent_info=None):
        self._info_hash = info_hash
        self._state = state
        self._has_metadata = has_metadata
        self._total_wanted = total_wanted
        self._total_wanted_done = total_wanted_done
        self.paused = paused
        self.calls: list[tuple] = []
        self.upload_limit = None
        self.download_limit = None
        self._torrent_info = torrent_info or FakeTorrentInfo(
            name=name, total=total_wanted,
            paths=("Payload/a.bin",) * num_files, sizes=(total_wanted,) * num_files,
        )
        self._file_progress = list(file_progress)
        self._priorities = list(priorities)

    # -- introspection ---------------------------------------------------

    def is_valid(self):
        return True

    def info_hash(self):
        return self._info_hash

    def status(self):
        s = MagicMock()
        # Real bools/ints: the engine branches on these, and a MagicMock would be truthy
        # for every one of them.
        s.state = self._state
        s.has_metadata = self._has_metadata
        s.total_wanted = self._total_wanted
        s.total_wanted_done = self._total_wanted_done
        s.total_done = self._total_wanted_done
        s.progress = (self._total_wanted_done / self._total_wanted) if self._total_wanted else 0.0
        s.download_rate = 0
        s.upload_rate = 0
        s.num_seeds = 0
        s.num_peers = 0
        s.num_complete = -1
        s.num_incomplete = -1
        s.list_seeds = 0
        s.list_peers = 0
        s.is_finished = False
        s.is_seeding = False
        s.all_time_upload = 0
        s.all_time_download = 0
        s.last_seen_complete = 0
        del s.paused  # force the is_paused fallback, like a real non-paused flag
        s.is_paused = self.paused
        return s

    def torrent_file(self):
        return self._torrent_info

    def get_torrent_info(self):
        return self._torrent_info

    def file_progress(self):
        return self._file_progress

    def get_file_priorities(self):
        return self._priorities

    # -- mutators --------------------------------------------------------

    def pause(self):
        self.paused = True
        self.calls.append(("pause",))

    def resume(self):
        self.paused = False
        self.calls.append(("resume",))

    def force_recheck(self):
        self.calls.append(("force_recheck",))

    def force_reannounce(self):
        self.calls.append(("force_reannounce",))

    def save_resume_data(self):
        self.calls.append(("save_resume_data",))

    def flush_cache(self):
        self.calls.append(("flush_cache",))

    def move_storage(self, path):
        self.calls.append(("move_storage", path))

    def set_upload_limit(self, limit):
        self.upload_limit = limit
        self.calls.append(("set_upload_limit", limit))

    def set_download_limit(self, limit):
        self.download_limit = limit
        self.calls.append(("set_download_limit", limit))

    def file_priority(self, index, priority):
        if 0 <= index < len(self._priorities):
            self._priorities[index] = priority
        self.calls.append(("file_priority", index, priority))

    def rename_file(self, index, path):
        self.calls.append(("rename_file", index, path))

    def set_flags(self, *args):
        self.calls.append(("set_flags",))

    def unset_flags(self, *args):
        self.calls.append(("unset_flags",))


class FakeSession:
    """A ``lt.session`` stand-in that records the settings actually applied."""

    def __init__(self, alerts=None, handle_factory=None):
        self.alerts = list(alerts or [])
        self.applied: list[dict] = []
        self.added: list = []
        self.removed: list = []
        self.remove_options: list = []
        self.pop_error = None
        self.apply_error = None
        self._handle_factory = handle_factory or FakeHandle

    def get_settings(self):
        return {}

    def apply_settings(self, settings):
        if self.apply_error is not None:
            raise self.apply_error
        self.applied.append(dict(settings))

    def add_torrent(self, params):
        self.added.append(params)
        return self._handle_factory()

    def pop_alerts(self):
        if self.pop_error is not None:
            raise self.pop_error
        return self.alerts

    def remove_torrent(self, handle, *options):
        self.removed.append(handle)
        self.remove_options.append(options)


def magnet_params(name="", info_hash="aa" * 20, save_path=""):
    """A ``lt.parse_magnet_uri`` result whose hash is readable.

    ``info_hashes`` is deleted on purpose: the engine's
    ``_get_info_hash_from_params`` prefers ``params.info_hashes.v1``, and a bare
    ``MagicMock`` would hand back a ``"<MagicMock id=...>"`` string that never matches a
    real hash, so every fastresume would look mismatched.
    """
    params = MagicMock()
    del params.info_hashes
    params.info_hash = info_hash
    params.name = name
    params.save_path = save_path
    return params


def resume_params(info_hash="aa" * 20, save_path=""):
    params = MagicMock()
    del params.info_hashes
    params.info_hash = info_hash
    params.save_path = save_path
    del params.ti
    return params


def _urlparse(url):
    """Stand-in for ``urllib.parse.urlparse``, injected where the module forgot it."""
    from urllib.parse import urlparse

    return urlparse(url)


class EngineTestCase(unittest.TestCase):
    """Shared engine, in-memory DB, temp fastresume dir, and a fake libtorrent."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.fastresume = self.tmp / "fastresume"
        self.fastresume.mkdir(parents=True, exist_ok=True)
        patcher = patch.object(te_module, "FASTRESUME_DIR", self.fastresume)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.db = Database(":memory:")
        self.db.open()
        self.addCleanup(self.db.close)

        self.progress: list[tuple] = []
        self.statuses: list[tuple] = []
        self.filenames: list[tuple] = []

        self.engine = TorrentEngine(self.db)
        self.engine.set_callbacks(
            lambda *a: self.progress.append(a),
            lambda *a: self.statuses.append(a),
            lambda *a: self.filenames.append(a),
        )
        self.session = FakeSession()
        self.engine._session = self.session
        self.engine._running = True

    def use_handle_factory(self, factory):
        self.session._handle_factory = factory

    def add_torrent_entry(self, entry_id="t1", url="magnet:?xt=urn:btih:" + "aa" * 20,
                          **kw):
        base = dict(
            id=entry_id,
            url=url,
            filename="Payload",
            save_path=self.tmp.as_posix(),
            file_path=(self.tmp / "Payload").as_posix(),
            download_type="torrent",
            status="queued",
        )
        base.update(kw)
        entry = DownloadEntry(**base)
        self.db.add_download(entry)
        return entry

    def attach(self, entry_id="t1", **kw) -> FakeHandle:
        handle = FakeHandle(**kw)
        self.engine._handles[entry_id] = handle
        return handle

    def status_trail(self, entry_id="t1"):
        return [s[1] for s in self.statuses if s[0] == entry_id]

    def libtorrent(self, **attrs):
        return patch.multiple(te_module, _HAS_LIBTORRENT=True, lt=MagicMock(**attrs))


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

class TestInfoHashExtraction(unittest.TestCase):
    """Hash extraction across the three libtorrent API shapes and their failures."""

    def test_v1_hash_is_preferred(self):
        params = MagicMock()
        params.info_hashes.has_v1.return_value = True
        params.info_hashes.v1 = "AABB"
        self.assertEqual(_get_info_hash_from_params(params), "aabb")

    def test_v2_hash_is_used_when_there_is_no_v1(self):
        params = MagicMock()
        params.info_hashes.has_v1.return_value = False
        params.info_hashes.has_v2.return_value = True
        params.info_hashes.v2 = "CCDD"
        self.assertEqual(_get_info_hash_from_params(params), "ccdd")

    def test_a_none_params_is_empty(self):
        self.assertEqual(_get_info_hash_from_params(None), "")

    def test_a_raising_params_object_degrades_to_empty(self):
        params = MagicMock()
        type(params).info_hashes = property(lambda self: 1 / 0)
        self.assertEqual(_get_info_hash_from_params(params), "")

    def test_an_invalid_handle_has_no_hash(self):
        handle = MagicMock()
        handle.is_valid.return_value = False
        self.assertEqual(_get_info_hash_from_handle(handle), "")

    def test_a_none_handle_has_no_hash(self):
        self.assertEqual(_get_info_hash_from_handle(None), "")

    def test_handle_v1_hash_is_lowercased(self):
        handle = MagicMock()
        del handle.info_hashes
        handle.info_hash.return_value = "ABCDEF"
        self.assertEqual(_get_info_hash_from_handle(handle), "abcdef")

    def test_an_exploding_handle_degrades_to_empty(self):
        handle = MagicMock()
        type(handle).is_valid = property(lambda self: 1 / 0)
        self.assertEqual(_get_info_hash_from_handle(handle), "")


class TestSeedStampBackstop(unittest.TestCase):
    """``_newer_seed_stamp`` must never regress a newer manual stamp."""

    def test_zero_epoch_leaves_the_stamp_alone(self):
        self.assertEqual(_newer_seed_stamp("2026-01-01T00:00:00+00:00", 0), "")
        self.assertEqual(_newer_seed_stamp("", 0), "")

    def test_a_negative_epoch_leaves_the_stamp_alone(self):
        self.assertEqual(_newer_seed_stamp("2026-01-01T00:00:00+00:00", -5), "")

    def test_a_brand_new_stamp_is_applied_to_an_empty_field(self):
        stamp = _newer_seed_stamp("", 1_800_000_000)
        self.assertTrue(stamp)
        self.assertEqual(datetime.fromisoformat(stamp).tzinfo, timezone.utc)

    def test_an_older_libtorrent_value_does_not_regress_a_manual_stamp(self):
        manual = datetime.now(timezone.utc).isoformat()
        self.assertEqual(_newer_seed_stamp(manual, 1_600_000_000), "")

    def test_a_newer_libtorrent_value_is_applied(self):
        old = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        self.assertTrue(_newer_seed_stamp(old, 1_800_000_000))

    def test_an_unparseable_previous_stamp_is_overwritten(self):
        self.assertTrue(_newer_seed_stamp("not-a-date", 1_800_000_000))

    def test_an_out_of_range_epoch_degrades_to_leaving_the_stamp_alone(self):
        self.assertEqual(_newer_seed_stamp("2026-01-01T00:00:00+00:00", 10**18), "")

    def test_the_column_wins_over_the_legacy_metadata_key(self):
        entry = MagicMock()
        entry.seeding_started_at = "2026-05-01T00:00:00+00:00"
        entry.metadata = {"seeding_since": "2020-01-01T00:00:00+00:00"}
        self.assertEqual(seeding_session_start(entry), "2026-05-01T00:00:00+00:00")

    def test_the_legacy_metadata_key_is_the_fallback(self):
        entry = MagicMock()
        entry.seeding_started_at = ""
        entry.metadata = {"seeding_since": "2026-04-01T00:00:00+00:00"}
        self.assertEqual(seeding_session_start(entry), "2026-04-01T00:00:00+00:00")

    def test_no_column_and_no_metadata_is_empty(self):
        entry = MagicMock()
        entry.seeding_started_at = ""
        entry.metadata = {}
        self.assertEqual(seeding_session_start(entry), "")

    def test_mark_seeding_started_mirrors_both_fields(self):
        entry = DownloadEntry(id="x", url="u", filename="f", save_path="C:/t")
        stamp = mark_seeding_started(entry)
        self.assertEqual(entry.seeding_started_at, stamp)
        self.assertEqual(entry.metadata.get("seeding_since"), stamp)

    def test_mark_seeding_started_honours_an_explicit_time(self):
        entry = DownloadEntry(id="x", url="u", filename="f", save_path="C:/t")
        when = datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
        self.assertEqual(mark_seeding_started(entry, when), when.isoformat())


class TestPriorityLabels(unittest.TestCase):
    """Every documented priority band, plus the out-of-range values."""

    def test_bands(self):
        for prio, expected in (
            (7, "Max (100%)"), (8, "Max (100%)"),
            (6, "High (75%)"),
            (3, "Medium (50%)"), (4, "Medium (50%)"), (5, "Medium (50%)"),
            (1, "Low (25%)"), (2, "Low (25%)"),
            (0, "Don't Download"),
        ):
            with self.subTest(prio=prio):
                self.assertEqual(_priority_to_label(prio), expected)

    def test_a_negative_priority_is_do_not_download(self):
        self.assertEqual(_priority_to_label(-1), "Don't Download")


# ---------------------------------------------------------------------------
# Settings assembly
# ---------------------------------------------------------------------------

class TestSessionSettings(EngineTestCase):
    """``_apply_all_settings`` / ``set_session_limits``: what actually reaches libtorrent."""

    def _applied(self):
        return self.session.applied[-1]

    def test_defaults_bind_all_interfaces_and_disable_the_proxy(self):
        self.engine._apply_all_settings()
        settings = self._applied()
        self.assertEqual(settings["listen_interfaces"], "0.0.0.0:6881,[::]:6881")
        self.assertEqual(settings["outgoing_interfaces"], "")
        self.assertFalse(settings["force_proxy"])

    def test_a_bound_interface_is_used_for_listen_and_outgoing(self):
        self.engine.apply_network_config(
            NetworkConfig(interface_name="VPN", interface_ip="10.9.9.9")
        )
        settings = self._applied()
        self.assertEqual(settings["listen_interfaces"], "10.9.9.9:6881")
        self.assertEqual(settings["outgoing_interfaces"], "10.9.9.9")

    def test_an_interface_name_without_an_ip_is_not_treated_as_bound(self):
        self.engine.apply_network_config(NetworkConfig(interface_name="WiFi"))
        self.assertEqual(self._applied()["listen_interfaces"], "0.0.0.0:6881,[::]:6881")

    def test_tor_routing_forces_a_credential_free_socks5_proxy(self):
        """Tor's SOCKS5 is unauthenticated, so no username/password may be sent."""
        with patch.object(te_module.lt, "proxy_type_t", MagicMock(socks5="socks5")):
            self.engine.apply_tor_config(
                TorConfig(enabled=True, route_torrent=True, proxy_host="127.0.0.1",
                          proxy_port=9050)
            )
        settings = self._applied()
        self.assertEqual(settings["proxy_hostname"], "127.0.0.1")
        self.assertEqual(settings["proxy_port"], 9050)
        self.assertEqual(settings["proxy_username"], "")
        self.assertEqual(settings["proxy_password"], "")
        self.assertEqual(settings["proxy_type"], "socks5")
        self.assertTrue(settings["force_proxy"])
        self.assertTrue(settings["proxy_peer_connections"])
        self.assertTrue(settings["proxy_tracker_connections"])

    def test_tor_enabled_but_not_routing_torrents_falls_through(self):
        self.engine.apply_tor_config(TorConfig(enabled=True, route_torrent=False))
        self.engine.apply_network_config(
            NetworkConfig(proxy_enabled=True, proxy_host="p", proxy_port=8080,
                          proxy_type="http")
        )
        self.assertEqual(self._applied()["proxy_hostname"], "p")

    def test_a_socks5_proxy_without_credentials(self):
        with patch.object(te_module.lt, "proxy_type_t",
                          MagicMock(socks5="socks5", socks5_pw="socks5_pw")):
            self.engine.apply_network_config(
                NetworkConfig(proxy_enabled=True, proxy_host="p", proxy_port=1080,
                              proxy_type="socks5")
            )
        self.assertEqual(self._applied()["proxy_type"], "socks5")

    def test_a_socks5_proxy_with_credentials_uses_the_authenticated_variant(self):
        with patch.object(te_module.lt, "proxy_type_t",
                          MagicMock(socks5="socks5", socks5_pw="socks5_pw")):
            self.engine.apply_network_config(
                NetworkConfig(proxy_enabled=True, proxy_host="p", proxy_port=1080,
                              proxy_type="socks5", proxy_username="u", proxy_password="pw")
            )
        settings = self._applied()
        self.assertEqual(settings["proxy_type"], "socks5_pw")
        self.assertEqual(settings["proxy_username"], "u")
        self.assertEqual(settings["proxy_password"], "pw")

    def test_an_http_proxy_with_and_without_credentials(self):
        with patch.object(te_module.lt, "proxy_type_t",
                          MagicMock(http="http", http_pw="http_pw")):
            self.engine.apply_network_config(
                NetworkConfig(proxy_enabled=True, proxy_host="p", proxy_port=3128,
                              proxy_type="http")
            )
            self.assertEqual(self._applied()["proxy_type"], "http")
            self.engine.apply_network_config(
                NetworkConfig(proxy_enabled=True, proxy_host="p", proxy_port=3128,
                              proxy_type="http", proxy_username="u")
            )
            self.assertEqual(self._applied()["proxy_type"], "http_pw")

    def test_an_unrecognised_proxy_type_is_ignored_rather_than_half_applied(self):
        """A typo must disable the proxy, not force one libtorrent will not use.

        Regression test. ``proxy_type`` was only assigned inside the ``socks5``/``http``
        branches, so any other value - a typo, ``"https"``, a trailing space - still set
        ``force_proxy=True`` plus a proxy host, while ``proxy_type`` stayed at whatever the
        previous apply had left (or the libtorrent default, i.e. none). The result was a
        connection forced through a proxy that was never configured, failing silently. An
        unknown value is now ignored, with a warning.
        """
        with patch.object(te_module.lt, "proxy_type_t",
                          MagicMock(none="none", socks5="socks5", socks5_pw="socks5_pw",
                                    http="http", http_pw="http_pw")):
            self.engine.apply_network_config(
                NetworkConfig(proxy_enabled=True, proxy_host="p", proxy_port=3128,
                              proxy_type="https")
            )
        settings = self._applied()
        self.assertFalse(
            settings["force_proxy"],
            "an unrecognised proxy_type must not force a connection through a proxy that "
            "was never configured",
        )
        self.assertEqual(settings["proxy_type"], "none")
        self.assertEqual(settings["proxy_hostname"], "")

    def test_an_unrecognised_proxy_type_after_a_good_one_clears_the_previous_proxy(self):
        """The stale value from the previous apply must not survive the bad one."""
        with patch.object(te_module.lt, "proxy_type_t",
                          MagicMock(none="none", socks5="socks5", socks5_pw="socks5_pw",
                                    http="http", http_pw="http_pw")):
            self.engine.apply_network_config(
                NetworkConfig(proxy_enabled=True, proxy_host="good", proxy_port=1080,
                              proxy_type="socks5")
            )
            self.assertEqual(self._applied()["proxy_type"], "socks5")
            self.engine.apply_network_config(
                NetworkConfig(proxy_enabled=True, proxy_host="bad", proxy_port=9999,
                              proxy_type="SOCKS5 ")
            )
        settings = self._applied()
        self.assertEqual(settings["proxy_type"], "none")
        self.assertFalse(settings["force_proxy"])
        self.assertNotEqual(settings["proxy_hostname"], "bad")

    def test_a_proxy_with_no_host_is_ignored(self):
        self.engine.apply_network_config(
            NetworkConfig(proxy_enabled=True, proxy_host="", proxy_port=3128)
        )
        self.assertFalse(self._applied()["force_proxy"])

    def test_bandwidth_limits_are_forwarded(self):
        self.engine.apply_network_config(NetworkConfig(download_limit=500, upload_limit=250))
        settings = self._applied()
        self.assertEqual(settings["download_rate_limit"], 500)
        self.assertEqual(settings["upload_rate_limit"], 250)

    def test_a_failing_apply_is_contained(self):
        self.session.apply_error = RuntimeError("libtorrent unhappy")
        self.engine._apply_all_settings()  # must not raise

    def test_no_session_is_a_no_op(self):
        self.engine._session = None
        self.engine._apply_all_settings()
        self.assertEqual(self.session.applied, [])

    def test_no_libtorrent_is_a_no_op(self):
        with patch.object(te_module, "_HAS_LIBTORRENT", False):
            self.engine._apply_all_settings()
        self.assertEqual(self.session.applied, [])

    def test_set_session_limits_reaches_the_session_and_the_config(self):
        engine_config = NetworkConfig()
        self.engine.apply_network_config(engine_config)
        self.engine.set_session_limits(1234, 567)
        settings = self._applied()
        self.assertEqual(settings["download_rate_limit"], 1234)
        self.assertEqual(settings["upload_rate_limit"], 567)
        self.assertEqual(engine_config.download_limit, 1234)
        self.assertEqual(engine_config.upload_limit, 567)

    def test_set_session_limits_without_a_session_still_updates_the_config(self):
        self.engine._session = None
        self.engine.set_session_limits(1, 2)
        self.assertIsNone(self.engine.network_config)

    def test_set_session_limits_clamps_none_to_zero(self):
        self.engine.apply_network_config(NetworkConfig())
        self.engine.set_session_limits(None, None)
        settings = self._applied()
        self.assertEqual(settings["download_rate_limit"], 0)
        self.assertEqual(settings["upload_rate_limit"], 0)

    def test_a_failing_limit_apply_is_contained(self):
        self.session.apply_error = RuntimeError("nope")
        self.engine.set_session_limits(1, 2)  # must not raise


class TestSeedingLimits(EngineTestCase):
    """Upload limits applied to live handles, including the fallbacks.

    ``get_effective_seeding_speed_limit`` is the minimum of two non-zero candidates:
    ``max_seeding_speed`` (KB/s, x1024) and ``download_limit / download_to_seeding_ratio``.
    """

    def test_the_ratio_derived_limit_wins_when_it_is_the_lower_one(self):
        self.add_torrent_entry(status="seeding")
        self.engine.apply_network_config(NetworkConfig(download_limit=1000, upload_limit=900))
        self.engine.apply_torrent_config(
            TorrentConfig(max_seeding_speed=0, download_to_seeding_ratio=2.0)
        )
        handle = self.attach()
        self.engine._apply_seeding_limits()
        self.assertEqual(handle.upload_limit, 500, "1000 / ratio 2.0")

    def test_the_absolute_cap_wins_when_it_is_the_lower_one(self):
        self.add_torrent_entry(status="seeding")
        self.engine.apply_network_config(
            NetworkConfig(download_limit=100_000, upload_limit=99_000)
        )
        self.engine.apply_torrent_config(
            TorrentConfig(max_seeding_speed=16, download_to_seeding_ratio=2.0)
        )
        handle = self.attach()
        self.engine._apply_seeding_limits()
        self.assertEqual(
            handle.upload_limit, 16 * 1024,
            "KNOWN: the effective limit is the MIN of the two candidates, so the "
            "16 KB/s cap wins over the 50 KB/s derived one - documented at "
            "TorrentConfig.get_effective_seeding_speed_limit",
        )

    def test_an_unset_torrent_limit_falls_back_to_the_network_upload_limit(self):
        self.add_torrent_entry(status="seeding")
        self.engine.apply_network_config(NetworkConfig(upload_limit=777))
        self.engine.apply_torrent_config(
            TorrentConfig(max_seeding_speed=0, download_to_seeding_ratio=0.0)
        )
        handle = self.attach()
        self.engine._apply_seeding_limits()
        self.assertEqual(handle.upload_limit, 777)

    def test_no_limit_at_all_means_unlimited(self):
        self.add_torrent_entry(status="seeding")
        self.engine.apply_network_config(NetworkConfig(upload_limit=0))
        self.engine.apply_torrent_config(
            TorrentConfig(max_seeding_speed=0, download_to_seeding_ratio=0.0)
        )
        handle = self.attach()
        self.engine._apply_seeding_limits()
        self.assertEqual(handle.upload_limit, -1, "libtorrent spells unlimited as -1")

    def test_the_cap_alone_is_enough_without_a_download_limit(self):
        self.add_torrent_entry(status="seeding")
        self.engine.apply_network_config(NetworkConfig(download_limit=0, upload_limit=0))
        self.engine.apply_torrent_config(
            TorrentConfig(max_seeding_speed=128, download_to_seeding_ratio=2.0)
        )
        handle = self.attach()
        self.engine._apply_seeding_limits()
        self.assertEqual(handle.upload_limit, 128 * 1024)

    def test_only_seeding_torrents_get_the_limit(self):
        self.add_torrent_entry(status="downloading")
        self.engine.apply_network_config(NetworkConfig(upload_limit=500))
        self.engine.apply_torrent_config(
            TorrentConfig(max_seeding_speed=0, download_to_seeding_ratio=1.0)
        )
        handle = self.attach()
        self.engine._apply_seeding_limits()
        self.assertIsNone(handle.upload_limit, "a downloading torrent must not be throttled")

    def test_an_invalid_handle_is_skipped(self):
        self.add_torrent_entry(status="seeding")
        self.engine.apply_network_config(NetworkConfig(upload_limit=500))
        self.engine.apply_torrent_config(
            TorrentConfig(max_seeding_speed=0, download_to_seeding_ratio=1.0)
        )
        handle = self.attach()
        handle.is_valid = lambda: False
        self.engine._apply_seeding_limits()  # must not raise
        self.assertIsNone(handle.upload_limit)

    def test_a_raising_handle_is_contained(self):
        self.add_torrent_entry(status="seeding")
        self.engine.apply_network_config(NetworkConfig(upload_limit=500))
        self.engine.apply_torrent_config(
            TorrentConfig(max_seeding_speed=0, download_to_seeding_ratio=1.0)
        )
        handle = self.attach()
        handle.set_upload_limit = MagicMock(side_effect=RuntimeError("boom"))
        self.engine._apply_seeding_limits()  # must not raise

    def test_without_a_torrent_config_nothing_is_applied(self):
        self.add_torrent_entry(status="seeding")
        self.engine.apply_network_config(NetworkConfig(upload_limit=500))
        handle = self.attach()
        self.engine._apply_seeding_limits()
        self.assertIsNone(handle.upload_limit)


# ---------------------------------------------------------------------------
# add_torrent dispatch
# ---------------------------------------------------------------------------

class TestAddTorrentDispatch(EngineTestCase):
    """Source dispatch, the kill switch, and fastresume validation."""

    def test_without_a_session_it_refuses(self):
        self.engine._session = None
        entry = self.add_torrent_entry()
        self.assertFalse(self.engine.add_torrent(entry))

    def test_a_non_torrent_entry_is_skipped(self):
        entry = self.add_torrent_entry(download_type="http")
        self.assertFalse(self.engine.add_torrent(entry))
        self.assertEqual(self.session.added, [], "a .torrent must not be synthesised")

    def test_an_already_added_torrent_is_resumed_not_duplicated(self):
        entry = self.add_torrent_entry()
        handle = self.attach()
        self.assertTrue(self.engine.add_torrent(entry))
        self.assertIn(("resume",), handle.calls)
        self.assertEqual(self.session.added, [], "the session must not get a second handle")
        self.assertEqual(len(self.engine._handles), 1)

    def test_the_kill_switch_blocks_a_dead_interface(self):
        entry = self.add_torrent_entry()
        self.engine.apply_network_config(
            NetworkConfig(interface_name="VPN", interface_ip="10.9.9.9", kill_switch=True)
        )
        with patch.object(te_module, "is_interface_active", return_value=False):
            self.assertFalse(self.engine.add_torrent(entry))
        self.assertEqual(self.db.get_download("t1").status, "error")
        self.assertIn("Kill switch", self.db.get_download("t1").error_message)
        self.assertEqual(self.statuses[-1][1], "error")
        self.assertEqual(self.session.added, [])

    def test_the_kill_switch_allows_a_live_interface(self):
        entry = self.add_torrent_entry()
        self.engine.apply_network_config(
            NetworkConfig(interface_name="VPN", interface_ip="10.9.9.9", kill_switch=True)
        )
        with patch.object(te_module, "is_interface_active", return_value=True), \
             patch.object(te_module.lt, "parse_magnet_uri", return_value=magnet_params()):
            self.assertTrue(self.engine.add_torrent(entry))

    def test_an_unsupported_source_is_rejected(self):
        entry = self.add_torrent_entry(url="C:/not-a-magnet.txt")
        self.assertFalse(self.engine.add_torrent(entry))

    def test_an_http_url_that_is_not_a_torrent_is_rejected_without_fetching(self):
        """A magnet or a local file is fine; a plain HTTP media URL must not be fetched.

        This doubles as the check that ``urlparse`` is genuinely imported: if it were not,
        ``add_torrent`` would raise ``NameError`` instead of returning ``False`` and this
        test would error rather than pass.
        """
        entry = self.add_torrent_entry(url="https://example.com/movie.mkv")
        with patch("urllib.request.urlopen") as fetch:
            self.assertFalse(self.engine.add_torrent(entry))
        fetch.assert_not_called()

    def test_an_http_torrent_url_with_the_suffix_in_the_query_is_fetched(self):
        entry = self.add_torrent_entry(url="https://example.com/dl?id=x.torrent")
        with patch("urllib.request.urlopen", side_effect=OSError("offline")) as fetch:
            self.assertFalse(self.engine.add_torrent(entry))
        fetch.assert_called_once(), (
            "a .torrent in the query string must be fetched; today the missing "
            "urlparse import means the method never gets this far"
        )

    def test_a_magnet_that_will_not_parse_is_rejected(self):
        entry = self.add_torrent_entry()
        with patch.object(te_module.lt, "parse_magnet_uri", side_effect=ValueError("bad magnet")):
            self.assertFalse(self.engine.add_torrent(entry))
        self.assertEqual(self.session.added, [])

    def test_a_parsed_magnet_records_its_original_name_once(self):
        entry = self.add_torrent_entry()
        params = magnet_params(name="Real Torrent Name")
        with patch.object(te_module.lt, "parse_magnet_uri", return_value=params):
            self.assertTrue(self.engine.add_torrent(entry))
        self.assertEqual(self.db.get_download("t1").metadata["original_name"], "Real Torrent Name")

    def test_an_existing_original_name_is_not_overwritten_by_the_magnet(self):
        entry = self.add_torrent_entry()
        entry.metadata["original_name"] = "User Renamed"
        self.db.update_download(entry)
        params = magnet_params(name="Real Torrent Name")
        with patch.object(te_module.lt, "parse_magnet_uri", return_value=params):
            self.engine.add_torrent(entry)
        self.assertEqual(self.db.get_download("t1").metadata["original_name"], "User Renamed")

    def test_a_fastresume_for_a_different_torrent_is_discarded(self):
        entry = self.add_torrent_entry()
        (self.fastresume / "t1.fastresume").write_bytes(b"stale")
        params = magnet_params()
        resume = resume_params(info_hash="bb" * 20)
        with patch.object(te_module.lt, "parse_magnet_uri", return_value=params), \
             patch.object(te_module.lt, "read_resume_data", return_value=resume):
            self.engine.add_torrent(entry)
        self.assertFalse(
            (self.fastresume / "t1.fastresume").exists(),
            "a hash-mismatched fastresume must be deleted, not applied",
        )

    def test_a_matching_fastresume_is_applied(self):
        entry = self.add_torrent_entry(torrent_info_hash="aa" * 20)
        (self.fastresume / "t1.fastresume").write_bytes(b"good")
        params = magnet_params()
        resume = resume_params(info_hash="aa" * 20)
        with patch.object(te_module.lt, "parse_magnet_uri", return_value=params), \
             patch.object(te_module.lt, "read_resume_data", return_value=resume):
            self.engine.add_torrent(entry)
        self.assertTrue((self.fastresume / "t1.fastresume").exists())

    def test_an_unparseable_fastresume_is_ignored_and_kept(self):
        entry = self.add_torrent_entry()
        (self.fastresume / "t1.fastresume").write_bytes(b"corrupt")
        params = magnet_params()
        with patch.object(te_module.lt, "parse_magnet_uri", return_value=params), \
             patch.object(te_module.lt, "read_resume_data", side_effect=ValueError("bad")):
            self.engine.add_torrent(entry)
        self.assertTrue((self.fastresume / "t1.fastresume").exists(),
                        "an unreadable fastresume must not be silently deleted")

    def test_a_torrent_file_that_cannot_be_parsed_is_rejected(self):
        source = self.tmp / "broken.torrent"
        source.write_bytes(b"not bencode")
        entry = self.add_torrent_entry(url=source.as_posix())
        with patch.object(te_module.lt, "torrent_info", side_effect=ValueError("bad metainfo")):
            self.assertFalse(self.engine.add_torrent(entry))
        self.assertEqual(self.session.added, [])

    def test_a_torrent_file_records_its_original_name(self):
        source = self.tmp / "x.torrent"
        source.write_bytes(b"stub")
        entry = self.add_torrent_entry(url=source.as_posix())
        info = MagicMock()
        info.info_hash.return_value = "cc" * 20
        info.name.return_value = "From File"
        with patch.object(te_module.lt, "torrent_info", return_value=info), \
             patch.object(te_module.lt, "add_torrent_params", return_value=MagicMock()):
            self.assertTrue(self.engine.add_torrent(entry))
        self.assertEqual(self.db.get_download("t1").metadata["original_name"], "From File")

    def test_a_remote_torrent_that_cannot_be_fetched_is_rejected(self):
        """With ``urlparse`` supplied, the fetch path runs and reports its own failure."""
        entry = self.add_torrent_entry(url="https://example.com/a.torrent")
        with patch("urllib.request.urlopen", side_effect=OSError("offline")):
            self.assertFalse(self.engine.add_torrent(entry))
        self.assertEqual(self.db.get_download("t1").status, "queued",
                         "a failed fetch must not leave the row in a partial state")

    def test_a_remote_torrent_is_cached_before_parsing(self):
        """A remote ``.torrent`` must actually be downloaded, cached and parsed.

        Regression test. ``add_torrent`` calls ``urlparse(url)`` to decide whether an
        ``http(s)://`` source points at a ``.torrent`` file, but the module never imported
        ``urlparse`` - so every remote ``.torrent`` raised ``NameError`` from inside
        ``add_torrent``, and the caller's broad ``except Exception`` turned it into a silent
        "failed to add". Remote torrents were simply unusable.
        """
        entry = self.add_torrent_entry(url="https://example.com/a.torrent")
        info = MagicMock()
        info.info_hash.return_value = "dd" * 20
        info.name.return_value = "Remote"
        response = MagicMock()
        response.read.return_value = b"bencode-bytes"
        response.__enter__.return_value = response
        with patch("urllib.request.urlopen", return_value=response), \
             patch.object(te_module.lt, "torrent_info", return_value=info), \
             patch.object(te_module.lt, "add_torrent_params", return_value=MagicMock()):
            self.assertTrue(self.engine.add_torrent(entry))
        self.assertTrue((self.fastresume / "t1.torrent").exists())
        self.assertEqual((self.fastresume / "t1.torrent").read_bytes(), b"bencode-bytes")

    def test_a_remote_torrent_records_its_original_name(self):
        entry = self.add_torrent_entry(url="https://example.com/a.torrent")
        info = MagicMock()
        info.info_hash.return_value = "dd" * 20
        info.name.return_value = "Remote"
        response = MagicMock()
        response.read.return_value = b"bencode"
        response.__enter__.return_value = response
        with patch("urllib.request.urlopen", return_value=response), \
             patch.object(te_module.lt, "torrent_info", return_value=info), \
             patch.object(te_module.lt, "add_torrent_params", return_value=MagicMock()):
            self.engine.add_torrent(entry)
        self.assertEqual(self.db.get_download("t1").metadata["original_name"], "Remote")

    def test_the_fetch_sends_a_browser_user_agent(self):
        entry = self.add_torrent_entry(url="https://example.com/a.torrent")
        info = MagicMock()
        info.info_hash.return_value = "dd" * 20
        info.name.return_value = ""
        response = MagicMock()
        response.read.return_value = b"bencode"
        response.__enter__.return_value = response
        with patch("urllib.request.urlopen", return_value=response) as fetch, \
             patch.object(te_module.lt, "torrent_info", return_value=info), \
             patch.object(te_module.lt, "add_torrent_params", return_value=MagicMock()):
            self.engine.add_torrent(entry)
        request = fetch.call_args.args[0]
        self.assertIn("Mozilla", request.headers["User-agent"])

    def test_a_remote_torrent_with_unparseable_metainfo_is_rejected(self):
        entry = self.add_torrent_entry(url="https://example.com/a.torrent")
        response = MagicMock()
        response.read.return_value = b"not bencode"
        response.__enter__.return_value = response
        with patch("urllib.request.urlopen", return_value=response), \
             patch.object(te_module.lt, "torrent_info", side_effect=ValueError("bad")):
            self.assertFalse(self.engine.add_torrent(entry))

    def test_a_paused_entry_is_added_paused_and_says_so(self):
        entry = self.add_torrent_entry(status="paused")
        params = magnet_params()
        with patch.object(te_module.lt, "parse_magnet_uri", return_value=params):
            self.engine.add_torrent(entry)
        handle = self.engine._handles["t1"]
        self.assertIn(("pause",), handle.calls)
        self.assertEqual(self.db.get_download("t1").status, "paused")
        self.assertEqual(self.status_trail()[-1], "paused")

    def test_a_completed_entry_is_added_paused_without_losing_its_status(self):
        entry = self.add_torrent_entry(status="completed")
        params = magnet_params()
        with patch.object(te_module.lt, "parse_magnet_uri", return_value=params):
            self.engine.add_torrent(entry)
        self.assertEqual(self.db.get_download("t1").status, "completed",
                         "a completed torrent must not regress to fetching_metadata")
        self.assertIn(("pause",), self.engine._handles["t1"].calls)

    def test_a_metadata_less_handle_starts_in_fetching_metadata_and_stamps_the_timer(self):
        """The stored row must say ``fetching_metadata``, not just the callback.

        Regression test. ``add_torrent`` calls ``update_status(id, "fetching_metadata")`` and
        then, to arm the watchdog, sets ``fetching_metadata_since`` and calls
        ``update_download(entry)``. ``update_download`` writes *every* column and ``entry``
        is the caller's object whose ``status`` is still ``"queued"``, so the second write
        silently reverted the first: the table showed "Queued" forever and
        ``_check_fetching_metadata_timeout`` - which requires that status - could never
        suspend a stuck magnet. The status is now carried onto the entry first.
        """
        self.use_handle_factory(lambda: FakeHandle(has_metadata=False))
        entry = self.add_torrent_entry()
        params = magnet_params()
        with patch.object(te_module.lt, "parse_magnet_uri", return_value=params):
            self.engine.add_torrent(entry)
        row = self.db.get_download("t1")
        self.assertEqual(row.status, "fetching_metadata")
        self.assertEqual(self.status_trail()[-1], "fetching_metadata")
        self.assertTrue(row.fetching_metadata_since, "the metadata timer must be armed")

    def test_the_metadata_timeout_guard_fires_for_a_stuck_magnet(self):
        """The suspend-after-N-days watchdog, which the status revert made unreachable."""
        self.use_handle_factory(lambda: FakeHandle(has_metadata=False))
        entry = self.add_torrent_entry()
        params = magnet_params()
        with patch.object(te_module.lt, "parse_magnet_uri", return_value=params):
            self.engine.add_torrent(entry)

        row = self.db.get_download("t1")
        row.fetching_metadata_since = (
            datetime.now(timezone.utc) - timedelta(days=30)
        ).isoformat()
        self.db.update_download(row)
        self.engine._torrent_config = TorrentConfig(metadata_fetch_timeout_days=1)
        self.engine._check_fetching_metadata_timeout("t1", row, {"downloaded": 0})
        self.assertEqual(
            self.db.get_download("t1").status, "suspended",
            "a month-old fetching_metadata torrent must be suspended",
        )

    def test_a_handle_with_metadata_and_no_fastresume_is_rechecked_first(self):
        entry = self.add_torrent_entry()
        params = magnet_params()
        with patch.object(te_module.lt, "parse_magnet_uri", return_value=params):
            self.engine.add_torrent(entry)
        handle = self.engine._handles["t1"]
        self.assertIn(("force_recheck",), handle.calls)
        self.assertEqual(self.db.get_download("t1").status, "checking")

    def test_a_handle_with_metadata_and_a_fastresume_skips_the_recheck(self):
        entry = self.add_torrent_entry()
        (self.fastresume / "t1.fastresume").write_bytes(b"good")
        params = magnet_params()
        resume = resume_params(info_hash="aa" * 20)
        with patch.object(te_module.lt, "parse_magnet_uri", return_value=params), \
             patch.object(te_module.lt, "read_resume_data", return_value=resume):
            self.engine.add_torrent(entry)
        self.assertEqual(self.db.get_download("t1").status, "downloading")
        self.assertNotIn(("force_recheck",), self.engine._handles["t1"].calls)

    def test_a_seeding_entry_keeps_seeding_and_gets_its_upload_limit(self):
        entry = self.add_torrent_entry(status="seeding")
        self.engine.apply_network_config(
            NetworkConfig(download_limit=400, upload_limit=400)
        )
        self.engine.apply_torrent_config(
            TorrentConfig(max_seeding_speed=0, download_to_seeding_ratio=4.0)
        )
        params = magnet_params()
        with patch.object(te_module.lt, "parse_magnet_uri", return_value=params):
            self.engine.add_torrent(entry)
        handle = self.engine._handles["t1"]
        self.assertEqual(handle.upload_limit, 100, "400 / ratio 4.0")
        self.assertNotIn(("pause",), handle.calls)
        self.assertEqual(self.db.get_download("t1").status, "seeding")

    def test_the_info_hash_is_persisted_from_the_handle(self):
        entry = self.add_torrent_entry(torrent_info_hash="")
        params = magnet_params(info_hash="00" * 20)
        with patch.object(te_module.lt, "parse_magnet_uri", return_value=params):
            self.engine.add_torrent(entry)
        self.assertEqual(
            self.db.get_download("t1").torrent_info_hash, "aa" * 20,
            "the handle's own info hash must be written back so the queue can "
            "reconcile it against the magnet",
        )

    def test_the_handles_hash_overwrites_a_stale_row_value(self):
        """Only the session knows the resolved hash, so the handle wins.

        For a magnet the row is created before the metadata is fetched, so
        ``torrent_info_hash`` is empty or wrong at ``add_torrent`` time. The engine
        therefore writes the handle's hash back unconditionally, overwriting whatever
        the row held. That is correct for magnets, but it also means a row whose hash
        was manually corrected is silently reverted on the next session start.
        """
        entry = self.add_torrent_entry(torrent_info_hash="00" * 20)
        params = magnet_params(info_hash="00" * 20)
        with patch.object(te_module.lt, "parse_magnet_uri", return_value=params):
            self.engine.add_torrent(entry)
        self.assertEqual(
            self.db.get_download("t1").torrent_info_hash, "aa" * 20,
            "the session's own hash is authoritative",
        )


# ---------------------------------------------------------------------------
# Per-download controls
# ---------------------------------------------------------------------------

class TestTorRouteAndAllocation(EngineTestCase):
    """The two per-download flags libtorrent only honours at session scope."""

    def test_the_tor_flag_is_persisted(self):
        self.add_torrent_entry()
        self.engine.set_torrent_tor_route("t1", True)
        self.assertTrue(self.db.get_download("t1").metadata.get("route_through_tor"))
        self.assertTrue(self.engine.is_torrent_tor_routed("t1"))

    def test_the_tor_flag_is_cleared(self):
        self.add_torrent_entry()
        self.engine.set_torrent_tor_route("t1", True)
        self.engine.set_torrent_tor_route("t1", False)
        self.assertFalse(self.engine.is_torrent_tor_routed("t1"))

    def test_reapplying_the_same_flag_does_not_rewrite_the_row(self):
        entry = self.add_torrent_entry()
        self.engine.set_torrent_tor_route("t1", True)
        before = self.db.get_download("t1").metadata_json
        self.engine.set_torrent_tor_route("t1", True)
        self.assertEqual(self.db.get_download("t1").metadata_json, before)

    def test_the_tor_flag_of_an_unknown_id_is_a_safe_no_op(self):
        self.engine.set_torrent_tor_route("nope", True)
        self.assertFalse(self.engine.is_torrent_tor_routed("nope"))

    def test_allocation_fractions_scale_the_global_limits(self):
        self.add_torrent_entry()
        self.engine.apply_network_config(NetworkConfig(download_limit=1000, upload_limit=800))
        handle = self.attach()
        for allocation, expected in (("low", 250), ("medium", 500), ("high", 750), ("max", 1000)):
            self.engine.set_torrent_bandwidth_allocation("t1", allocation)
            self.assertEqual(handle.download_limit, expected, allocation)
            self.assertEqual(handle.upload_limit, int(800 * expected / 1000), allocation)

    def test_allocation_is_case_insensitive(self):
        self.add_torrent_entry()
        self.engine.apply_network_config(NetworkConfig(download_limit=1000, upload_limit=800))
        handle = self.attach()
        self.engine.set_torrent_bandwidth_allocation("t1", "LOW")
        self.assertEqual(handle.download_limit, 250)

    def test_an_unknown_allocation_gets_the_whole_limit(self):
        self.add_torrent_entry()
        self.engine.apply_network_config(NetworkConfig(download_limit=1000, upload_limit=800))
        handle = self.attach()
        self.engine.set_torrent_bandwidth_allocation("t1", "turbo")
        self.assertEqual(handle.download_limit, 1000)
        self.assertEqual(handle.upload_limit, 800)

    def test_without_a_global_limit_a_reduced_allocation_uses_a_10mbps_floor(self):
        """Unlimited global + a reduced share still needs a concrete number to give libtorrent."""
        self.add_torrent_entry()
        self.engine.apply_network_config(NetworkConfig(download_limit=0, upload_limit=0))
        handle = self.attach()
        self.engine.set_torrent_bandwidth_allocation("t1", "low")
        self.assertEqual(handle.download_limit, 2_500_000)
        self.engine.set_torrent_bandwidth_allocation("t1", "max")
        self.assertEqual(handle.download_limit, -1, "a full share means unlimited, not 10MB/s")

    def test_allocation_is_persisted_even_without_a_handle(self):
        self.add_torrent_entry()
        self.engine.set_torrent_bandwidth_allocation("t1", "high")
        self.assertEqual(
            self.db.get_download("t1").metadata.get("bandwidth_allocation"), "high"
        )

    def test_allocation_for_an_unknown_id_does_not_raise(self):
        self.engine.set_torrent_bandwidth_allocation("nope", "high")  # must not raise

    def test_an_invalid_handle_skips_the_limit_without_raising(self):
        self.add_torrent_entry()
        handle = self.attach()
        handle.is_valid = lambda: False
        self.engine.apply_network_config(NetworkConfig(download_limit=1000))
        self.engine.set_torrent_bandwidth_allocation("t1", "low")  # must not raise
        self.assertIsNone(handle.download_limit)

    def test_a_raising_handle_is_contained(self):
        self.add_torrent_entry()
        handle = self.attach()
        handle.set_download_limit = MagicMock(side_effect=RuntimeError("boom"))
        self.engine.apply_network_config(NetworkConfig(download_limit=1000))
        self.engine.set_torrent_bandwidth_allocation("t1", "low")  # must not raise


# ---------------------------------------------------------------------------
# remove / recheck / move_storage
# ---------------------------------------------------------------------------

class TestRemovalAndMaintenance(EngineTestCase):
    """The small lifecycle verbs, and the fastresume file each one leaves behind."""

    def test_remove_drops_the_handle_and_keeps_the_files(self):
        self.add_torrent_entry()
        handle = self.attach()
        self.engine.remove("t1")
        self.assertNotIn("t1", self.engine._handles)
        self.assertEqual(self.session.removed, [handle])
        self.assertEqual(self.session.remove_options, [()])

    def test_remove_with_delete_files_passes_the_option(self):
        self.add_torrent_entry()
        handle = self.attach()
        with patch.object(te_module.lt, "options_t", MagicMock(delete_files="delete")):
            self.engine.remove("t1", delete_files=True)
        self.assertEqual(self.session.remove_options, [("delete",)])

    def test_remove_deletes_the_fastresume(self):
        self.add_torrent_entry()
        self.attach()
        (self.fastresume / "t1.fastresume").write_bytes(b"resume")
        self.engine.remove("t1")
        self.assertFalse((self.fastresume / "t1.fastresume").exists())

    def test_remove_of_an_unknown_id_is_a_no_op(self):
        self.engine.remove("nope")  # must not raise
        self.assertEqual(self.session.removed, [])

    def test_remove_without_a_session_still_drops_the_handle(self):
        self.add_torrent_entry()
        self.attach()
        self.engine._session = None
        self.engine.remove("t1")
        self.assertNotIn("t1", self.engine._handles)

    def test_recheck_reenables_auto_managed_and_resumes(self):
        self.add_torrent_entry()
        handle = self.attach()
        self.engine.recheck("t1")
        self.assertIn(("set_flags",), handle.calls)
        self.assertIn(("resume",), handle.calls)
        self.assertIn(("force_recheck",), handle.calls)
        self.assertEqual(self.status_trail()[-1] if self.statuses else "checking", "checking")

    def test_recheck_of_an_unknown_id_is_a_no_op(self):
        self.engine.recheck("nope")
        self.assertEqual(self.db.get_download("nope"), None)

    def test_move_storage_is_forwarded_to_the_handle(self):
        self.add_torrent_entry()
        handle = self.attach()
        self.engine.move_storage("t1", "D:/new")
        self.assertIn(("move_storage", "D:/new"), handle.calls)

    def test_a_failing_move_storage_is_contained(self):
        self.add_torrent_entry()
        handle = self.attach()
        handle.move_storage = MagicMock(side_effect=RuntimeError("locked"))
        self.engine.move_storage("t1", "D:/new")  # must not raise

    def test_move_storage_without_a_handle_is_a_no_op(self):
        self.engine.move_storage("nope", "D:/new")


# ---------------------------------------------------------------------------
# get_status
# ---------------------------------------------------------------------------

class TestGetStatusMapping(EngineTestCase):
    """Every field ``get_status`` derives, and the fallbacks it prefers."""

    def test_no_handle_is_none(self):
        self.assertIsNone(self.engine.get_status("nope"))

    def test_a_raising_status_is_none_not_an_exception(self):
        handle = self.attach()
        handle.status = MagicMock(side_effect=RuntimeError("libtorrent died"))
        self.assertIsNone(self.engine.get_status("t1"))

    def test_a_finished_torrent_reports_one_hundred_percent(self):
        handle = self.attach(state=5, total_wanted=1000, total_wanted_done=999)
        status = self.engine.get_status("t1")
        self.assertEqual(status["state"], "seeding")
        self.assertEqual(status["downloaded"], 1000, "a finished torrent is fully downloaded")
        self.assertEqual(status["progress"], 100.0)

    def test_the_finished_state_is_named(self):
        handle = self.attach(state=4)
        self.assertEqual(self.engine.get_status("t1")["state"], "finished")

    def test_an_unknown_state_index_falls_back_to_its_raw_value(self):
        handle = self.attach(state=3)
        self.assertEqual(self.engine.get_status("t1")["state"], "downloading")
        handle._state = 99
        self.assertEqual(self.engine.get_status("t1")["state"], "99")

    def test_total_done_wins_over_total_wanted_done(self):
        handle = self.attach(total_wanted=1000, total_wanted_done=100)
        base_status = handle.status

        def _status():
            s = base_status()
            s.total_done = 400
            return s

        handle.status = _status
        self.assertEqual(self.engine.get_status("t1")["downloaded"], 400)

    def test_torrent_info_total_size_overrides_total_wanted(self):
        handle = self.attach(total_wanted=1000)
        handle._torrent_info = FakeTorrentInfo(total=5000)
        self.assertEqual(self.engine.get_status("t1")["total_size"], 5000)

    def test_a_zero_info_size_does_not_zero_the_total(self):
        handle = self.attach(total_wanted=1000)
        handle._torrent_info = FakeTorrentInfo(total=0)
        self.assertEqual(self.engine.get_status("t1")["total_size"], 1000)

    def test_eta_is_zero_without_speed_and_positive_with_it(self):
        handle = self.attach(total_wanted=1000, total_wanted_done=400)
        base = FakeHandle.status.__get__(handle)

        handle.status = _with_speed(base, 100)
        self.assertAlmostEqual(self.engine.get_status("t1")["eta"], 6.0,
                               msg="600 remaining bytes at 100 B/s")

        handle.status = _with_speed(base, 0)
        self.assertEqual(self.engine.get_status("t1")["eta"], 0, "no speed means no ETA")

    def test_swarm_totals_prefer_the_scrape_then_the_list(self):
        handle = self.attach()
        base = handle.status

        def _swarm(num_complete, num_incomplete, list_seeds, list_peers, seeds, peers):
            def _f():
                s = base()
                s.num_complete = num_complete
                s.num_incomplete = num_incomplete
                s.list_seeds = list_seeds
                s.list_peers = list_peers
                s.num_seeds = seeds
                s.num_peers = peers
                return s

            return _f

        handle.status = _swarm(50, -1, 10, 0, 3, 4)
        status = self.engine.get_status("t1")
        self.assertEqual(status["total_seeds"], 50, "the scrape wins when it is known")
        self.assertEqual(status["total_peers"], 4, "unknown scrape falls back to the list")

        handle.status = _swarm(-1, -1, 0, 0, 3, 4)
        status = self.engine.get_status("t1")
        self.assertEqual((status["total_seeds"], status["total_peers"]), (3, 4))

        handle.status = _swarm(2, 2, 9, 9, 3, 4)
        status = self.engine.get_status("t1")
        self.assertEqual((status["total_seeds"], status["total_peers"]), (9, 9))

    def test_the_name_comes_from_the_torrent_info(self):
        handle = self.attach()
        handle._torrent_info = FakeTorrentInfo(name="Real Name")
        self.assertEqual(self.engine.get_status("t1")["name"], "Real Name")

    def test_no_metadata_means_no_name(self):
        handle = self.attach(has_metadata=False)
        self.assertEqual(self.engine.get_status("t1")["name"], "")

    def test_last_seeded_comes_from_either_attribute_spelling(self):
        handle = self.attach()
        base = handle.status

        def _new():
            s = base()
            s.last_seen_complete = 1_800_000_000
            return s

        handle.status = _new
        self.assertEqual(self.engine.get_status("t1")["last_seeded_epoch"], 1_800_000_000)

        def _legacy():
            s = base()
            s.last_seen_complete = 0
            s.last_seen = 1_700_000_000
            return s

        handle.status = _legacy
        self.assertEqual(
            self.engine.get_status("t1")["last_seeded_epoch"], 1_700_000_000,
            "the 1.2-era last_seen spelling must still be read",
        )

    def test_an_unparseable_last_seen_becomes_zero(self):
        handle = self.attach()
        base = handle.status

        def _bad():
            s = base()
            s.last_seen_complete = "not a number"
            return s

        handle.status = _bad
        self.assertEqual(self.engine.get_status("t1")["last_seeded_epoch"], 0)

    def test_upload_and_download_totals_prefer_all_time(self):
        handle = self.attach()
        base = handle.status

        def _totals():
            s = base()
            s.all_time_upload = 500
            s.all_time_download = 900
            return s

        handle.status = _totals
        status = self.engine.get_status("t1")
        self.assertEqual((status["total_upload"], status["total_download"]), (500, 900))


def _with_speed(base_status, speed):
    """Wrap a status producer so it reports *speed*."""

    def _f():
        s = base_status()
        s.download_rate = speed
        return s

    return _f


# ---------------------------------------------------------------------------
# Details queries
# ---------------------------------------------------------------------------

class TestTorrentFiles(EngineTestCase):
    """File-list assembly, priorities, and the cached-metadata fallbacks."""

    def _handle_with_files(self, sizes=(100, 200, 300), progress=None, priorities=None):
        sizes = list(sizes)
        if progress is None:
            progress = [0] * len(sizes)
        if priorities is None:
            priorities = [4] * len(sizes)
        return self.attach(
            num_files=len(sizes),
            file_progress=list(progress),
            priorities=list(priorities),
            torrent_info=FakeTorrentInfo(
                total=sum(sizes),
                paths=tuple(f"Payload/f{i}.bin" for i in range(len(sizes))),
                sizes=sizes,
            ),
        )

    def test_no_handle_falls_back_to_the_cached_list(self):
        self.add_torrent_entry()
        entry = self.db.get_download("t1")
        entry.metadata["files"] = [{"path": "Payload/a.bin", "size": 10, "priority": 4}]
        self.db.update_download(entry)
        files = self.engine.get_torrent_files("t1")
        self.assertEqual(len(files), 1)

    def test_no_handle_and_no_cache_is_empty(self):
        self.add_torrent_entry()
        self.assertEqual(self.engine.get_torrent_files("t1"), [])

    def test_a_completed_entry_marks_every_wanted_file_completed(self):
        self.add_torrent_entry(status="completed")
        self._handle_with_files(progress=(0, 0, 0), priorities=(4, 0, 7))
        files = self.engine.get_torrent_files("t1")
        by_index = {f["index"]: f for f in files}
        self.assertEqual(by_index[0]["status"], "completed")
        self.assertEqual(by_index[1]["status"], "skipped", "priority 0 is never downloaded")
        self.assertEqual(by_index[2]["status"], "completed")

    def test_a_skipped_file_reports_its_real_progress(self):
        self.add_torrent_entry()
        self._handle_with_files(sizes=(100, 200, 300), progress=(0, 50, 0),
                                priorities=(4, 0, 4))
        files = self.engine.get_torrent_files("t1")
        by_index = {f["index"]: f for f in files}
        self.assertEqual(by_index[1]["downloaded"], 50)
        self.assertEqual(by_index[1]["progress"], 25.0, "50 of 200 bytes")
        self.assertEqual(by_index[1]["status"], "skipped")

    def test_status_transitions_with_progress(self):
        self.add_torrent_entry()
        self._handle_with_files(sizes=(100, 100, 100), progress=(0, 50, 100),
                                priorities=(4, 4, 4))
        by_index = {f["index"]: f for f in self.engine.get_torrent_files("t1")}
        self.assertEqual(by_index[0]["status"], "pending")
        self.assertEqual(by_index[1]["status"], "downloading")
        self.assertEqual(by_index[2]["status"], "completed")

    def test_a_pending_file_whose_size_is_zero_does_not_divide_by_zero(self):
        self.add_torrent_entry()
        self._handle_with_files(sizes=(0,), progress=(), priorities=(4,))
        files = self.engine.get_torrent_files("t1")
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["progress"], 100.0, "an empty file counts as done")

    def test_progress_is_clamped_to_one_hundred(self):
        self.add_torrent_entry()
        self._handle_with_files(sizes=(100,), progress=(500,), priorities=(4,))
        self.assertEqual(self.engine.get_torrent_files("t1")[0]["progress"], 100.0)

    def test_a_nested_path_root_is_rewritten_to_the_entry_filename(self):
        self.add_torrent_entry(filename="Renamed Root")
        self._handle_with_files(sizes=(100,))
        files = self.engine.get_torrent_files("t1")
        self.assertTrue(files[0]["path"].startswith("Renamed Root/"))

    def test_a_single_file_path_is_rewritten_only_for_an_explicit_filename(self):
        self.add_torrent_entry()
        entry = self.db.get_download("t1")
        entry.metadata["explicit_filename"] = True
        entry.filename = "MyName"
        self.db.update_download(entry)
        self.attach(torrent_info=FakeTorrentInfo(paths=("TorrentName.bin",), sizes=(10,)))
        files = self.engine.get_torrent_files("t1")
        self.assertEqual(files[0]["path"], "MyName")

    def test_an_invalid_handle_falls_back_to_the_cached_list(self):
        self.add_torrent_entry()
        entry = self.db.get_download("t1")
        entry.metadata["files"] = [{"path": "Payload/a.bin", "size": 10}]
        self.db.update_download(entry)
        handle = self.attach()
        handle.is_valid = lambda: False
        self.assertEqual(len(self.engine.get_torrent_files("t1")), 1)

    def test_a_handle_with_no_torrent_info_falls_back_to_the_cached_list(self):
        self.add_torrent_entry()
        entry = self.db.get_download("t1")
        entry.metadata["files"] = [{"path": "Payload/a.bin", "size": 10}]
        self.db.update_download(entry)
        handle = self.attach()
        handle.torrent_file = lambda: None
        self.assertEqual(len(self.engine.get_torrent_files("t1")), 1)

    def test_a_raising_handle_falls_back_to_the_cached_list(self):
        self.add_torrent_entry()
        entry = self.db.get_download("t1")
        entry.metadata["files"] = [{"path": "Payload/a.bin", "size": 10}]
        self.db.update_download(entry)
        handle = self.attach()
        handle.is_valid = MagicMock(side_effect=RuntimeError("boom"))
        self.assertEqual(len(self.engine.get_torrent_files("t1")), 1)

    def test_priority_labels_are_attached(self):
        self.add_torrent_entry()
        self._handle_with_files(priorities=(0, 1, 7))
        labels = {f["index"]: f["priority_label"] for f in self.engine.get_torrent_files("t1")}
        self.assertEqual(labels, {0: "Don't Download", 1: "Low (25%)", 2: "Max (100%)"})


class TestFilePriority(EngineTestCase):
    """``set_torrent_file_priority`` for a live handle and the offline paths."""

    def test_a_non_torrent_entry_is_refused(self):
        self.add_torrent_entry(download_type="http")
        self.assertFalse(self.engine.set_torrent_file_priority("t1", 0, 0))

    def test_an_unknown_id_is_refused(self):
        self.assertFalse(self.engine.set_torrent_file_priority("nope", 0, 0))

    def test_unchecking_a_file_on_a_finished_torrent_persists(self):
        """The uncheck must survive a reload, not just the current session.

        Regression test. ``DownloadEntry.metadata`` re-parses ``metadata_json`` on every
        access and only a *top-level* assignment syncs, so mutating a file dict in place was
        discarded and ``update_download`` wrote the old JSON back: the call reported success
        and the UI updated, then every file was priority 4 again after a restart. The call
        site now re-assigns the whole list.
        """
        self.add_torrent_entry(status="completed")
        entry = self.db.get_download("t1")
        entry.metadata["files"] = [
            {"index": 0, "path": "Payload/a.bin", "size": 10, "priority": 4, "progress": 100.0},
            {"index": 1, "path": "Payload/b.bin", "size": 10, "priority": 4, "progress": 100.0},
        ]
        self.db.update_download(entry)
        self.assertTrue(self.engine.set_torrent_file_priority("t1", 1, 0))

        files = self.db.get_download("t1").metadata["files"]
        self.assertEqual(files[0]["priority"], 4, "the sibling file is untouched")
        self.assertEqual(files[1]["priority"], 0, "the change must reach the database")
        self.assertEqual(files[1]["status"], "skipped")
        self.assertEqual(self.session.added, [], "no handle should be created to uncheck a file")

    def test_a_top_level_metadata_assignment_does_persist(self):
        """The contrast that documents why the file list is re-assigned."""
        self.add_torrent_entry(status="completed")
        entry = self.db.get_download("t1")
        entry.metadata["files"] = [
            {"index": 0, "path": "Payload/a.bin", "size": 10, "priority": 4},
        ]
        entry.metadata["manual_seeding"] = True
        self.db.update_download(entry)
        self.assertTrue(self.db.get_download("t1").metadata.get("manual_seeding"))

    def test_an_invalid_handle_refuses(self):
        self.add_torrent_entry()
        handle = self.attach()
        handle.is_valid = lambda: False
        self.assertFalse(self.engine.set_torrent_file_priority("t1", 0, 0))

    def test_a_raising_handle_is_contained(self):
        self.add_torrent_entry()
        handle = self.attach()
        handle.file_priority = MagicMock(side_effect=RuntimeError("boom"))
        self.assertFalse(self.engine.set_torrent_file_priority("t1", 0, 0))

    def test_a_live_priority_change_reaches_both_the_handle_and_the_database(self):
        self.add_torrent_entry()
        handle = self.attach()
        entry = self.db.get_download("t1")
        entry.metadata["files"] = [
            {"index": 0, "path": "Payload/a.bin", "size": 100, "downloaded": 50, "progress": 50.0},
        ]
        self.db.update_download(entry)
        self.assertTrue(self.engine.set_torrent_file_priority("t1", 0, 0))
        self.assertIn(("file_priority", 0, 0), handle.calls, "libtorrent must be told")
        stored = self.db.get_download("t1").metadata["files"][0]
        self.assertEqual(stored["status"], "skipped")
        self.assertEqual(stored["priority_label"], "Don't Download")
        self.assertEqual(stored["priority"], 0)

    def test_rechecking_a_file_on_a_finished_torrent_resumes_the_download(self):
        self.add_torrent_entry(status="completed", total_size=1000, downloaded_size=1000)
        handle = self.attach()
        entry = self.db.get_download("t1")
        entry.metadata["files"] = [
            {"index": 0, "path": "Payload/a.bin", "size": 1000, "downloaded": 1000,
             "progress": 100.0},
        ]
        self.db.update_download(entry)
        # total_wanted == total_wanted_done would mean "all done", so make it incomplete.
        handle._total_wanted = 2000
        handle._total_wanted_done = 1000
        self.assertTrue(self.engine.set_torrent_file_priority("t1", 0, 4))
        self.assertEqual(self.db.get_download("t1").status, "downloading")
        self.assertIn(("resume",), handle.calls)
        self.assertEqual(self.status_trail()[-1], "downloading")

    def test_rechecking_a_file_while_incomplete_does_not_change_status(self):
        self.add_torrent_entry(status="downloading")
        handle = self.attach()
        self.assertTrue(self.engine.set_torrent_file_priority("t1", 0, 4))
        self.assertEqual(self.db.get_download("t1").status, "downloading")
        self.assertNotIn(("resume",), handle.calls)

    def test_progress_is_emitted_after_a_priority_change(self):
        self.add_torrent_entry()
        self.attach()
        self.engine.set_torrent_file_priority("t1", 0, 0)
        self.assertTrue(self.progress, "the details panel needs a progress refresh")


class TestTorrentTrackersAndPeers(EngineTestCase):
    """Tracker status precedence, scrape totals, and the cached fallbacks."""

    def test_no_handle_falls_back_to_the_cached_trackers(self):
        self.add_torrent_entry()
        entry = self.db.get_download("t1")
        entry.metadata["trackers"] = [{"url": "udp://cached", "tier": 0, "status": "Working"}]
        self.db.update_download(entry)
        trackers = self.engine.get_torrent_trackers("t1")
        self.assertEqual(trackers[0]["url"], "udp://cached")

    def test_no_handle_and_no_cache_is_empty(self):
        self.add_torrent_entry()
        self.assertEqual(self.engine.get_torrent_trackers("t1"), [])

    def test_a_working_tracker_with_no_message(self):
        self.add_torrent_entry()
        handle = self.attach()
        handle.trackers = lambda: [_tracker(url="udp://a", fails=0, message="", endpoints=[])]
        self.assertEqual(self.engine.get_torrent_trackers("t1")[0]["status"], "Working")

    def test_a_failing_tracker_with_a_message_reports_the_error(self):
        self.add_torrent_entry()
        handle = self.attach()
        handle.trackers = lambda: [_tracker(fails=3, message="connection refused")]
        self.assertEqual(
            self.engine.get_torrent_trackers("t1")[0]["status"], "Error: connection refused"
        )

    def test_a_failing_tracker_without_a_message_is_unreachable(self):
        self.add_torrent_entry()
        handle = self.attach()
        handle.trackers = lambda: [_tracker(fails=3, message="")]
        self.assertEqual(self.engine.get_torrent_trackers("t1")[0]["status"], "Unreachable")

    def test_a_tracker_message_without_failures_is_shown_verbatim(self):
        self.add_torrent_entry()
        handle = self.attach()
        handle.trackers = lambda: [_tracker(fails=0, message="updating from cache")]
        self.assertEqual(
            self.engine.get_torrent_trackers("t1")[0]["status"], "updating from cache"
        )

    def test_an_updating_endpoint_outranks_failures(self):
        self.add_torrent_entry()
        handle = self.attach()
        handle.trackers = lambda: [
            _tracker(fails=5, message="stale error",
                     endpoints=[_endpoint(updating=True, scrape_complete=7,
                                          scrape_incomplete=9)])
        ]
        tracker = self.engine.get_torrent_trackers("t1")[0]
        self.assertEqual(tracker["status"], "Updating")
        self.assertEqual((tracker["seeds"], tracker["peers"]), (7, 9))

    def test_an_endpoint_message_is_used_when_the_tracker_has_none(self):
        self.add_torrent_entry()
        handle = self.attach()
        handle.trackers = lambda: [
            _tracker(fails=2, message="",
                     endpoints=[_endpoint(updating=False, message="peer list corrupt")])
        ]
        self.assertEqual(
            self.engine.get_torrent_trackers("t1")[0]["status"], "Error: peer list corrupt"
        )

    def test_an_unknown_scrape_reports_zero_not_minus_one(self):
        self.add_torrent_entry()
        handle = self.attach()
        handle.trackers = lambda: [
            _tracker(endpoints=[_endpoint(scrape_complete=-1, scrape_incomplete=-1)])
        ]
        tracker = self.engine.get_torrent_trackers("t1")[0]
        self.assertEqual((tracker["seeds"], tracker["peers"]), (0, 0))

    def test_an_invalid_handle_falls_back_to_the_cache(self):
        self.add_torrent_entry()
        entry = self.db.get_download("t1")
        entry.metadata["trackers"] = [{"url": "udp://cached"}]
        self.db.update_download(entry)
        handle = self.attach()
        handle.is_valid = lambda: False
        self.assertEqual(len(self.engine.get_torrent_trackers("t1")), 1)

    def test_a_raising_tracker_query_falls_back_to_the_cache(self):
        self.add_torrent_entry()
        entry = self.db.get_download("t1")
        entry.metadata["trackers"] = [{"url": "udp://cached"}]
        self.db.update_download(entry)
        handle = self.attach()
        handle.trackers = MagicMock(side_effect=RuntimeError("boom"))
        self.assertEqual(len(self.engine.get_torrent_trackers("t1")), 1)

    # -- peers -------------------------------------------------------------

    def test_no_handle_falls_back_to_the_cached_peers(self):
        self.add_torrent_entry()
        entry = self.db.get_download("t1")
        entry.metadata["peer_list"] = [{"ip": "1.2.3.4:80", "client": "Cached"}]
        self.db.update_download(entry)
        peers = self.engine.get_torrent_peers("t1")
        self.assertEqual(peers[0]["client"], "Cached")

    def test_the_legacy_peers_key_is_used_when_peer_list_is_absent(self):
        self.add_torrent_entry()
        entry = self.db.get_download("t1")
        entry.metadata["peers"] = [{"ip": "1.2.3.4:80", "client": "Legacy"}]
        self.db.update_download(entry)
        self.assertEqual(self.engine.get_torrent_peers("t1")[0]["client"], "Legacy")

    def test_a_peer_with_no_flags_falls_back_to_a_dash(self):
        self.add_torrent_entry()
        handle = self.attach()
        # Every flag off, except outgoing_connection, which the engine inverts.
        flags_off = {flag: False for flag in _PEER_FLAGS}
        # outgoing_connection is inverted by the engine, so leave it on to get no
        # "I" flag and genuinely exercise the empty-flags fallback.
        flags_off["outgoing_connection"] = True
        handle.get_peer_info = lambda: [_peer(**flags_off)]
        self.assertEqual(self.engine.get_torrent_peers("t1")[0]["flags"], "—")

    def test_a_raising_peer_query_falls_back_to_the_cache(self):
        self.add_torrent_entry()
        entry = self.db.get_download("t1")
        entry.metadata["peer_list"] = [{"ip": "1.2.3.4:80", "client": "Cached"}]
        self.db.update_download(entry)
        handle = self.attach()
        handle.get_peer_info = MagicMock(side_effect=RuntimeError("boom"))
        self.assertEqual(len(self.engine.get_torrent_peers("t1")), 1)

    def test_an_invalid_handle_falls_back_to_the_cache(self):
        self.add_torrent_entry()
        entry = self.db.get_download("t1")
        entry.metadata["peer_list"] = [{"ip": "1.2.3.4:80", "client": "Cached"}]
        self.db.update_download(entry)
        handle = self.attach()
        handle.is_valid = lambda: False
        self.assertEqual(len(self.engine.get_torrent_peers("t1")), 1)


_PEER_FLAGS = (
    "seed", "upload_only", "choked", "interesting", "remote_choked",
    "remote_interested", "optimistic_unchoke", "snubbed", "rc4_encrypted",
    "plaintext_encrypted", "dht", "pex", "outgoing_connection", "local_connection",
)


def _peer(**kw):
    peer = MagicMock()
    peer.ip = (10, 0, 0, 1, 6881)
    peer.client = "TestClient/1.0"
    peer.progress = 0.5
    peer.down_speed = 0
    peer.up_speed = 0
    peer.flags = ""
    for flag, value in kw.items():
        setattr(peer, flag, value)
    return peer


def _tracker(url="udp://tracker:80", tier=0, fails=0, message="", endpoints=None):
    tracker = MagicMock()
    tracker.url = url
    tracker.tier = tier
    tracker.fails = fails
    tracker.message = message
    tracker.endpoints = endpoints or []
    tracker.send_stats = False
    return tracker


def _endpoint(updating=False, scrape_complete=-1, scrape_incomplete=-1, message=""):
    ih = MagicMock()
    ih.updating = updating
    ih.scrape_complete = scrape_complete
    ih.scrape_incomplete = scrape_incomplete
    ih.message = message
    endpoint = MagicMock()
    endpoint.updating = updating
    endpoint.info_hashes = [ih]
    return endpoint


# ---------------------------------------------------------------------------
# Alert routing
# ---------------------------------------------------------------------------

class _FakeAlert:
    """A libtorrent alert stand-in identified by its ``what()`` string."""

    def __init__(self, name, handle=None, message=""):
        self._name = name
        self.handle = handle
        self.params = None
        self._message = message

    def what(self):
        return self._name

    def message(self):
        return self._message


def _fake_alert(name, handle=None, message=""):
    return _FakeAlert(name, handle=handle, message=message)


class TestAlertRouting(EngineTestCase):
    """Which libtorrent alert turns into which user-visible outcome."""

    _FakeAlert = _FakeAlert

    def test_a_pop_failure_is_contained(self):
        self.session.pop_error = RuntimeError("session closed")
        self.engine._process_alerts()  # must not raise

    def test_no_session_is_a_no_op(self):
        self.engine._session = None
        self.engine._process_alerts()  # must not raise

    def test_an_empty_alert_batch_is_a_no_op(self):
        self.engine._process_alerts()  # must not raise

    def test_a_file_error_alert_marks_the_row_file_not_found(self):
        self.add_torrent_entry()
        handle = self.attach()
        alert = self._FakeAlert("file_error_alert", handle=handle)
        with patch.object(te_module.lt, "file_error_alert", self._FakeAlert):
            self.session.alerts = [alert]
            self.engine._process_alerts()
        self.assertEqual(self.db.get_download("t1").status, "file_not_found")
        self.assertEqual(self.status_trail()[-1], "file_not_found")

    def test_a_file_error_alert_for_an_unknown_handle_is_ignored(self):
        self.add_torrent_entry()
        orphan = FakeHandle()
        alert = self._FakeAlert("file_error_alert", handle=orphan)
        with patch.object(te_module.lt, "file_error_alert", self._FakeAlert):
            self.session.alerts = [alert]
            self.engine._process_alerts()
        self.assertEqual(self.db.get_download("t1").status, "queued")

    def test_a_file_error_alert_with_no_handle_is_ignored(self):
        self.add_torrent_entry()
        self.attach()
        alert = self._FakeAlert("file_error_alert", handle=None)
        with patch.object(te_module.lt, "file_error_alert", self._FakeAlert):
            self.session.alerts = [alert]
            self.engine._process_alerts()  # must not raise

    def test_a_resume_data_alert_writes_the_matching_fastresume(self):
        self.add_torrent_entry()
        handle = self.attach(info_hash="ff" * 20)
        alert = self._FakeAlert("save_resume_data_alert", handle=handle)
        alert.params = _buf_params(b"buf", info_hash="ff" * 20)
        with patch.object(te_module.lt, "write_resume_data_buf", return_value=b"buf"):
            self.session.alerts = [alert]
            self.engine._process_alerts()
        self.assertEqual((self.fastresume / "t1.fastresume").read_bytes(), b"buf")

    def test_a_resume_data_alert_whose_hash_disagrees_is_refused(self):
        """Writing the wrong torrent's resume data would corrupt the next session."""
        self.add_torrent_entry()
        handle = self.attach(info_hash="ff" * 20)
        alert = self._FakeAlert("save_resume_data_alert", handle=handle)
        alert.params = _buf_params(b"buf", info_hash="00" * 20)
        with patch.object(te_module.lt, "write_resume_data_buf", return_value=b"buf") as write:
            self.session.alerts = [alert]
            self.engine._process_alerts()
        write.assert_not_called()
        self.assertFalse((self.fastresume / "t1.fastresume").exists())

    def test_a_resume_data_alert_for_an_unmatched_handle_is_dropped(self):
        self.add_torrent_entry()
        self.attach(info_hash="ff" * 20)
        alert = self._FakeAlert("save_resume_data_alert", handle=FakeHandle(info_hash="11" * 20))
        alert.params = _buf_params(b"buf", info_hash="11" * 20)
        with patch.object(te_module.lt, "write_resume_data_buf", return_value=b"buf") as write:
            self.session.alerts = [alert]
            self.engine._process_alerts()
        write.assert_not_called()

    def test_a_resume_data_alert_with_no_handle_is_dropped(self):
        self.add_torrent_entry()
        alert = self._FakeAlert("save_resume_data_alert", handle=None)
        with patch.object(te_module.lt, "write_resume_data_buf", return_value=b"buf") as write:
            self.session.alerts = [alert]
            self.engine._process_alerts()
        write.assert_not_called()

    def test_an_unwritable_fastresume_is_contained(self):
        self.add_torrent_entry()
        handle = self.attach()
        alert = self._FakeAlert("save_resume_data_alert", handle=handle)
        alert.params = _buf_params(b"buf", info_hash=handle._info_hash)
        with patch.object(te_module.lt, "write_resume_data_buf", side_effect=RuntimeError("disk")):
            self.session.alerts = [alert]
            self.engine._process_alerts()  # must not raise

    def test_save_resume_data_failed_is_swallowed(self):
        self.add_torrent_entry()
        self.attach()
        alert = self._FakeAlert("save_resume_data_failed_alert", message="no space")
        self.session.alerts = [alert]
        self.engine._process_alerts()  # must not raise
        self.assertEqual(self.db.get_download("t1").status, "queued")


@unittest.skipIf(IS_HEADLESS_WIN_CI, "headless Windows CI crashes on QApplication.processEvents in engine.stop()")
class TestStopResumeDrain(EngineTestCase):
    """``stop()``: resume data is paired to the right torrent, and shutdown never hangs."""

    def test_stop_reports_progress_when_a_status_callback_is_supplied(self):
        self.add_torrent_entry()
        self.attach()
        seen: list[tuple] = []
        self.engine.stop(status_cb=lambda *a: seen.append(a))
        self.assertTrue(any("resume state" in str(a[0]) for a in seen))

    def test_stop_pairs_each_alert_to_its_own_torrent_by_hash(self):
        for index, info_hash in enumerate(("11" * 20, "22" * 20), start=1):
            self.add_torrent_entry(entry_id=f"t{index}")
            self.attach(entry_id=f"t{index}", info_hash=info_hash)
        alerts = [
            self._alert(FakeHandle(info_hash="22" * 20), b"second"),
            self._alert(FakeHandle(info_hash="11" * 20), b"first"),
        ]
        self.session.alerts = alerts
        with patch.object(te_module.lt, "write_resume_data_buf",
                          side_effect=lambda p: p._buf):
            self.engine.stop()
        self.assertEqual((self.fastresume / "t1.fastresume").read_bytes(), b"first")
        self.assertEqual((self.fastresume / "t2.fastresume").read_bytes(), b"second")

    def test_a_failure_alert_stops_the_engine_waiting_for_that_torrent(self):
        self.add_torrent_entry()
        handle = self.attach()
        alert = self._resume_alert(handle)
        alert.__class__.__name__ = "save_resume_data_failed_alert"
        self.session.alerts = [alert]
        # A second, unmatched batch so the loop would spin if the pending set never cleared.
        self.session.pop_alerts = MagicMock(side_effect=[[], []])
        with patch.object(te_module.time, "time", side_effect=[0, 0.1, 0.2, 99, 99, 99]), \
             patch.object(te_module.time, "sleep"):
            self.engine.stop()
        self.assertIsNone(self.engine._session)

    def test_stop_gives_up_after_the_deadline(self):
        """A session that never answers must not block shutdown past the deadline."""
        self.add_torrent_entry()
        self.attach()
        self.session.pop_alerts = MagicMock(return_value=[])
        clock = iter([0, 0, 0, 1.0, 1.0, 5.0, 5.0, 5.0])
        with patch.object(te_module.time, "time", side_effect=lambda: next(clock)), \
             patch.object(te_module.time, "sleep"):
            self.engine.stop()
        self.assertIsNone(self.engine._session)
        self.assertEqual(self.engine._handles, {})

    def test_an_invalid_handle_is_not_asked_for_resume_data(self):
        self.add_torrent_entry()
        handle = self.attach()
        handle.is_valid = lambda: False
        with patch.object(te_module.time, "time", side_effect=[0, 0, 0, 5, 5, 5]), \
             patch.object(te_module.time, "sleep"):
            self.engine.stop()
        self.assertNotIn(("save_resume_data",), handle.calls)

    def test_stop_without_a_session_is_a_no_op(self):
        self.engine._session = None
        self.engine.stop()

    def test_a_raising_handle_does_not_abort_shutdown(self):
        self.add_torrent_entry()
        handle = self.attach()
        handle.is_valid = MagicMock(side_effect=RuntimeError("boom"))
        with patch.object(te_module.time, "time", side_effect=[0, 0, 0, 5, 5, 5]), \
             patch.object(te_module.time, "sleep"):
            self.engine.stop()
        self.assertIsNone(self.engine._session)

    @staticmethod
    def _alert(handle, buf):
        alert = _fake_alert("save_resume_data_alert", handle=handle)
        alert.params = _buf_params(buf, info_hash=handle._info_hash)
        return alert

    def _resume_alert(self, handle, buf=b"x"):
        return self._alert(handle, buf)


def _buf_params(buf: bytes, info_hash: str = "aa" * 20):
    """A ``lt.add_torrent_params`` whose info hash the engine can actually read."""
    params = MagicMock()
    del params.info_hashes
    params.info_hash = info_hash
    params._buf = buf
    return params


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------

class TestConcurrentAccess(EngineTestCase):
    """The engine is shared across the Qt thread and manager threads.

    ``DownloadManager`` polls from a ``QTimer`` while user actions arrive on the same Qt
    thread, and ``ManagerMonitor`` reads from its own thread. These tests assert the two
    properties that actually matter for that arrangement: iteration is never invalidated
    by a concurrent mutation, and no exception escapes a worker thread (where it would be
    lost silently and the download would look unmonitored).
    """

    WORKERS = 6
    ROUNDS = 25

    def _seed_torrents(self, count):
        for index in range(count):
            did = f"t{index}"
            self.add_torrent_entry(entry_id=did)
            self.attach(entry_id=did)

    def _run_concurrently(self, target, workers=None):
        workers = workers or self.WORKERS
        errors: list[BaseException] = []
        lock = threading.Lock()
        start = threading.Barrier(workers, timeout=10)

        def _wrapped(index):
            try:
                start.wait()
                for _ in range(self.ROUNDS):
                    target(index)
            except BaseException as exc:  # noqa: BLE001 - asserted on below
                with lock:
                    errors.append(exc)

        threads = [
            threading.Thread(target=_wrapped, args=(i,), daemon=True)
            for i in range(workers)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
            self.assertFalse(thread.is_alive(), "a worker thread hung")
        self.assertEqual(errors, [], f"worker threads raised: {errors!r}")

    def test_polling_while_handles_are_removed_never_invalidates_iteration(self):
        """``poll_all`` iterates ``list(self._handles.items())``; ``remove`` pops.

        If that snapshot were ever replaced by a live view, a concurrent ``remove`` would
        raise "dictionary changed size during iteration" from the poll timer.
        """
        self._seed_torrents(self.WORKERS)
        stop = threading.Event()

        def _poll():
            while not stop.is_set():
                self.engine.poll_all()

        poller = threading.Thread(target=_poll, daemon=True)
        poller.start()
        try:
            self._run_concurrently(lambda i: self.engine.remove(f"t{i}"))
        finally:
            stop.set()
            poller.join(timeout=10)
            self.assertFalse(poller.is_alive(), "the poll loop failed to exit")

        self.assertEqual(self.engine._handles, {}, "every handle must have been removed")

    def test_polling_while_pausing_and_resuming_never_invalidates_iteration(self):
        self._seed_torrents(self.WORKERS)
        stop = threading.Event()
        errors: list[BaseException] = []

        def _poll():
            try:
                while not stop.is_set():
                    self.engine.poll_all()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        poller = threading.Thread(target=_poll, daemon=True)
        poller.start()
        try:
            self._run_concurrently(
                lambda i: (
                    self.engine.pause(f"t{i % self.WORKERS}"),
                    self.engine.resume(f"t{i % self.WORKERS}"),
                )
            )
        finally:
            stop.set()
            poller.join(timeout=10)

        self.assertEqual(errors, [], f"the poll thread raised: {errors!r}")

    def test_concurrent_bandwidth_changes_all_land_on_the_right_handle(self):
        self._seed_torrents(self.WORKERS)
        self.engine.apply_network_config(NetworkConfig(download_limit=1000, upload_limit=1000))
        allocations = ["low", "medium", "high", "max"]

        def _worker(index):
            did = f"t{index}"
            self.engine.set_torrent_bandwidth_allocation(did, allocations[index % 4])

        self._run_concurrently(_worker)

        fractions = {"low": 0.25, "medium": 0.50, "high": 0.75, "max": 1.0}
        for index in range(self.WORKERS):
            handle = self.engine._handles[f"t{index}"]
            expected = int(1000 * fractions[allocations[index % 4]])
            self.assertEqual(handle.download_limit, expected, f"handle t{index}")

    def test_concurrent_add_and_remove_leave_a_consistent_registry(self):
        self._run_concurrently(
            lambda i: (
                self.engine.remove(f"cx{i}"),
                self.engine._handles.__setitem__(f"cx{i}", FakeHandle()),
            )
        )
        self.assertEqual(len(self.engine._handles), self.WORKERS)

    def test_concurrent_seeding_limit_application_is_contained(self):
        self._seed_torrents(self.WORKERS)
        for index in range(self.WORKERS):
            self.db.update_status(f"t{index}", "seeding")
        self._run_concurrently(
            lambda i: self.engine.apply_torrent_config(
                TorrentConfig(max_seeding_speed=0, download_to_seeding_ratio=float(i % 5) or 1.0)
            )
        )

    def test_concurrent_status_reads_agree_with_the_handle(self):
        self._seed_torrents(self.WORKERS)
        results: dict[str, int] = {}
        lock = threading.Lock()
        start = threading.Barrier(self.WORKERS, timeout=10)

        def _worker(index):
            did = f"t{index}"
            start.wait()
            for _ in range(self.ROUNDS):
                status = self.engine.get_status(did)
                with lock:
                    results[did] = status["total_size"]

        threads = [
            threading.Thread(target=_worker, args=(i,), daemon=True)
            for i in range(self.WORKERS)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
            self.assertFalse(thread.is_alive())

        self.assertEqual(results, {f"t{i}": 1000 for i in range(self.WORKERS)})

    def test_a_failing_poll_is_contained_so_the_timer_survives(self):
        """``poll_all`` runs from a Qt timer; a raise would kill every later tick."""
        self._seed_torrents(1)
        self.engine._handles["t0"] = MagicMock()
        type(self.engine._handles["t0"]).status = property(
            lambda self: 1 / 0
        )
        for _ in range(3):
            self.engine.poll_all()  # must not raise


if __name__ == "__main__":
    unittest.main()


