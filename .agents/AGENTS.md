# Agent Guidelines & Repository Architecture

This file provides context and navigation for AI agents working in the **My-IDM** repository.

---

## 🏛️ Architecture Documentation

The canonical architecture documentation is organized under [`docs/architecture/`](file:///d:/Projects/my-idm/docs/architecture):

| Document | File | Description |
| :--- | :--- | :--- |
| **System Architecture** | [`docs/architecture/main.md`](file:///d:/Projects/my-idm/docs/architecture/main.md) | High-level system architecture, engine threading model (asyncio + libtorrent + Qt), SQLite database schema, and component workflows. |
| **Tor Network Privacy** | [`docs/architecture/tor.md`](file:///d:/Projects/my-idm/docs/architecture/tor.md) | Tor SOCKS5 proxy routing, `TorServiceManager` background lifecycle, executable auto-discovery, startup gating, and exit termination. |
| **VPN & Kill Switch** | [`docs/architecture/vpn.md`](file:///d:/Projects/my-idm/docs/architecture/vpn.md) | Network adapter interface binding, live adapter monitoring, instant kill switch loop, and HTTP/SOCKS5 proxy support. |
| **Antivirus & Security** | [`docs/architecture/antivirus.md`](file:///d:/Projects/my-idm/docs/architecture/antivirus.md) | Pre-download dangerous format warnings, double-extension inspection, post-download Windows Defender / custom scanning, and quarantine. |
| **BitTorrent Engine** | [`docs/architecture/torrent.md`](file:///d:/Projects/my-idm/docs/architecture/torrent.md) | `libtorrent` session management, magnet URI / `.torrent` file handling, file priorities, swarm/peer/tracker tracking, and fastresume. |
| **State Machines & Lifecycle** | [`docs/architecture/state-machines.md`](file:///d:/Projects/my-idm/docs/architecture/state-machines.md) | Dedicated Mermaid state diagrams for HTTP and BitTorrent, transition triggers, slot allocation, and backoff/seeding flows. |
| **Backlog Processing** | [`docs/architecture/backlog.md`](file:///d:/Projects/my-idm/docs/architecture/backlog.md) | Multi-location auto-discovery, custom download location delimiters & directives, auto-clearing queue lifecycle, and IPC ingestion. |
| **Database & Persistence** | [`docs/architecture/database.md`](file:///d:/Projects/my-idm/docs/architecture/database.md) | SQLite schema specification, constraints, indexing, `metadata_json` contract, migrations, and self-healing. |
| **Browser Integration** | [`docs/architecture/browser-integration.md`](file:///d:/Projects/my-idm/docs/architecture/browser-integration.md) | Chromium & Mozilla Firefox Manifest V3 extension, loopback REST server (127.0.0.1:19582), automated download interception, and cookie preservation. |
| **Window & System Tray** | [`docs/architecture/window-system-tray.md`](file:///d:/Projects/my-idm/docs/architecture/window-system-tray.md) | Windows system tray icon lifecycle, minimize-to-tray, close-to-tray, desktop completion toast notifications, and preferences reorganization. |
| **Table Views & Segregation** | [`docs/architecture/table-views.md`](file:///d:/Projects/my-idm/docs/architecture/table-views.md) | Table model architecture, custom delegates, status/date segregation algorithms, section header spans, context menus, and telemetry tables. |
| **YouTube Scraper** | [`docs/architecture/youtube-scraper.md`](file:///d:/Projects/my-idm/docs/architecture/youtube-scraper.md) | `yt-dlp` integration, Mode A (direct CDN URL via `HTTPEngine`) vs Mode B (yt-dlp + ffmpeg merge), the download dialog, playlist listing, and rate-limit budgeting. |

> When adding a new subsystem doc, add a row here **and** to the index in
> [`README.md`](file:///d:/Projects/my-idm/README.md) so the two do not drift.

---

## 📚 General Documentation & References

- **[User Guide](file:///d:/Projects/my-idm/docs/user-guide.md)** — Comprehensive user manual covering installation, GUI navigation, settings, and workflows.
- **[API Reference](file:///d:/Projects/my-idm/docs/api-reference.md)** — Detailed API reference for engines, database schema, manager signals, and delegates.
- **[TODO & Roadmap](file:///d:/Projects/my-idm/docs/TODO.md)** — Active development backlog, feature checklist, and tracked bug fixes.
- **[Project README](file:///d:/Projects/my-idm/README.md)** — Project overview, feature summary, screenshots, and setup instructions.

---

## 🛠️ Key Conventions for Agents

1. **Commit Convention**: Follow standard conventional commit prefixes (`feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `chore:`).
2. **Testing**: Always run `python -m pytest` and ensure all unit tests pass before committing.
   - The full suite is green: `1724 passed, 1 skipped`. The single skip is the opt-in real
     Windows Defender scan (`MYIDM_RUN_AV_TESTS`).
   - **The suite is hermetic by construction, enforced in `tests/conftest.py`.** Autouse
     fixtures fail the run if anything escapes the sandbox, so a violation is a bug to fix,
     not something to work around:
     - non-loopback TCP or DNS is refused (`block_non_loopback_network`),
     - destructive `subprocess` commands (`taskkill`, `del`, `rd`) are refused and recorded
       (`block_destructive_subprocess`); violations are reported at session teardown so a
       production `except Exception` cannot hide them,
     - anything that would pop open File Explorer on the host - `os.startfile`,
       `explorer.exe`, or a local-file `QDesktopServices.openUrl` - is refused and recorded
       (`block_desktop_shell_launches`); remote URLs stay allowed for the browser tests,
     - `my_idm.database.DB_PATH` is redirected to a temp file, so a `Database()` with no
       argument (e.g. `SettingsDialog._get_db()`) can never open the user's live database,
     - the system clipboard is snapshotted, cleared, and restored per test.
     `ALLOW_DESTRUCTIVE_SUBPROCESS` is the only opt-out, and a test that sets it must say why.
   - **Never let a test call a destructive filesystem helper on a path you did not create.**
     `quarantine_or_delete_file("")` resolves `Path("")` to `Path(".")`, i.e. the process's
     **current working directory**, and `shutil.rmtree`s it - so an unguarded call from the
     repo root deletes the repository. Any test touching such a helper must `os.chdir` into its
     own temp tree in `setUp` and restore it in cleanup. The same applies to any helper that
     resolves a blank or relative path: pin the behaviour with a canary inside the temp dir
     rather than asserting on the return value.
   - **Never use the real OS clipboard in a test.** `QClipboard.setText` is a silent no-op when
     the clipboard is transiently locked, which is what made
     `test_copy_multiple_urls_to_clipboard` flaky. Patch the single accessor production uses
     (`monkeypatch.setattr(QApplication, "clipboard", staticmethod(lambda: fake))` and assert
     the exact expected string) instead of retry loops.
   - **Never use `time.sleep`, a busy-wait, or a wall-clock budget as synchronisation.** Use a
     `threading.Event` the test controls, `QSignalSpy`, or a bounded pump-until-predicate helper.
     The only sleeps allowed are inside a Qt event-loop pump, where the predicate - not the sleep -
     is the synchronisation.
   - **No test may touch the real filesystem outside its temp dir.** This includes
     `~/.my-idm`, `~/Downloads`, the repo tree (a `.xpi` once landed in
     `browser_extension/`), and the real Recycle Bin. `test_security.py` gates the one genuine
     AV integration test behind `MYIDM_RUN_AV_TESTS`.
   - `Database` serialises its shared connection behind a reentrant lock
     (`_LockedConnection`), and a closed connection degrades to inert empty results. Do not
     remove either: without the lock, concurrent `update_progress` from two threads **segfaults
     the interpreter** (see `TestDatabaseThreadSafety` in `tests/test_database.py`). The
     documented `test_delete_during_download_releases_lock` shutdown race is fixed on two sides -
     `DownloadManager._await_timer_slots()` joins in-flight timer work, and the connection lock
     covers the rest - so that flake should stay gone. If it reappears, find the real cause; do
     not re-introduce a sleep.
   - The AnimePahe settings buttons are **intentionally silent on success** - the button label and
     the footer badge already show the new state, and only failures raise a dialog. Do not
     "fix" this by adding a success alert.
   - Prefer deterministic tests. Mocking `libtorrent` handles and driving `TorrentEngine.poll_all()`
     with bare `MagicMock()` handles silently disables the status path: `get_status()` raises
     `TypeError` on `ti.total_size() > 0` and swallows it, so the test passes without the code
     under test ever running. Use the explicit `FakeStatus` / `FakeHandle` shape instead.
     Also use real `TorrentConfig` / `GeneralConfig` objects rather than `MagicMock` where the
     engine compares their values against ints.
3. **Threading Architecture**:
   - Qt GUI runs on the main thread.
   - `HTTPEngine` uses an asyncio event loop running on a dedicated background thread (`idm-async`).
   - `TorrentEngine` uses `libtorrent` session managed via periodic Qt timer polls (`_poll_torrents`).
   - Background scans (antivirus) run on short-lived daemon threads (`scan-<id>`).
   - yt-dlp work runs on plain daemon threads (`ytdlp-download`, `yt-extract`). Never use `QThread` for
     it: destroying a `QThread` whose `run()` is still executing aborts the process, and detaching does
     not help because it is still destroyed at interpreter exit.
4. **Preserve User Settings & Environment (CRITICAL)**:
   - **NEVER** clear, wipe, or overwrite the user's live settings (`QSettings("MyIDM", "My-IDM").clear()`) during testing, debugging, or local development.
   - On Windows, un-isolated `QSettings("MyIDM", "My-IDM")` modifies the live Windows Registry (`HKEY_CURRENT_USER\Software\MyIDM\My-IDM`), which causes the user's default download folder, UI state, and configured preferences to be wiped repeatedly.
   - All tests must use isolated settings (enforced automatically in [`tests/conftest.py`](file:///d:/Projects/my-idm/tests/conftest.py) via a temporary `IniFormat` directory) or explicit temporary files (`QSettings(temp_file, QSettings.Format.IniFormat)`).
   - Never call `.clear()` on production registry settings.
5. **No Automatic Git Push (CRITICAL)**:
   - **NEVER** push automatically (`git push`).
   - Only commit changes locally (`git commit`).
   - Pushing to the remote repository must only be done if explicitly instructed by the user.
6. **Adding a Downloads-Table Column**:
   - **Append** a new `Col` constant. Never insert, because persisted `column_widths`,
     `header_state`, and `sort_column` in `ui_state` address columns by logical index.
   - Add a `NOT NULL DEFAULT ''` column guarded by `PRAGMA table_info` in `Database._create_tables()`.
     Migrations must stay additive and idempotent, and existing rows must read back with the default.
   - Record `column_count` in the saved UI state (already done). A state whose stored count differs
     from `Col.COUNT` predates the append, so the tail is re-pinned on restore and the user's own
     ordering of the older columns is preserved.
   - Add the column to `_DEFAULT_TAIL_COLUMNS` in `main_window.py` and set a default width in both
     `_setup_ui()` and `_on_reset_view()`. The tail helper must stay an **ascending** sweep:
     `moveSection()` shifts everything between source and target, so placing a column left of its
     target displaces an already-placed neighbour.
7. **File Deletion**: never call `Path.unlink()` or `os.remove()` on user data. Use
   `utils.send_to_trash()` and `utils.unlock_path()`, and stop any worker holding the file first — on
   Windows an open handle makes the file undeletable.
8. **Verify Against Real Data**: when a change touches persistence, column layout, or anything the user
   sees, confirm it against a copy of the real `~/.my-idm/downloads.db` or a rendered screenshot
   rather than trusting mocks alone.

