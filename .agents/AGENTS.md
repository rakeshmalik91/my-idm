# Agent Guidelines & Repository Architecture

This file provides context and navigation for AI agents working in the **My-IDM** repository.

---

## 🏛️ Architecture Documentation

The canonical architecture documentation is organized under [`docs/architecture/`](file:///d:/Projects/my-idm/docs/architecture):

| Document | File | Description |
| :--- | :--- | :--- |
| **System Architecture** | [`main.md`](file:///d:/Projects/my-idm/docs/architecture/main.md) | Threading model (asyncio + libtorrent + Qt), schema, component workflows. Start here. |
| **Tor Network Privacy** | [`tor.md`](file:///d:/Projects/my-idm/docs/architecture/tor.md) | SOCKS5 routing, service lifecycle, executable discovery, startup gating. |
| **VPN & Kill Switch** | [`vpn.md`](file:///d:/Projects/my-idm/docs/architecture/vpn.md) | Adapter binding, live monitoring, instant kill switch, proxy support. |
| **Antivirus & Security** | [`antivirus.md`](file:///d:/Projects/my-idm/docs/architecture/antivirus.md) | Pre-download warnings, double-extension inspection, Defender scanning, quarantine. |
| **BitTorrent Engine** | [`torrent.md`](file:///d:/Projects/my-idm/docs/architecture/torrent.md) | `libtorrent` session, magnet/`.torrent` handling, priorities, swarm tracking, fastresume. |
| **State Machines & Lifecycle** | [`state-machines.md`](file:///d:/Projects/my-idm/docs/architecture/state-machines.md) | Mermaid state diagrams for HTTP and BitTorrent, transition triggers, backoff/seeding. |
| **Backlog Processing** | [`backlog.md`](file:///d:/Projects/my-idm/docs/architecture/backlog.md) | Multi-location discovery, location delimiters & directives, queue lifecycle, IPC ingestion. |
| **Database & Persistence** | [`database.md`](file:///d:/Projects/my-idm/docs/architecture/database.md) | Schema, constraints, indexing, `metadata_json` contract, migrations, self-healing. |
| **Queues & Concurrency** | [`queues.md`](file:///d:/Projects/my-idm/docs/architecture/queues.md) | Named queues, per-queue concurrency budgets, the start gate, priority ordering, the seeded AnimePahe/YouTube source queues, and `queue=` in backlog files. |
| **Browser Integration** | [`browser-integration.md`](file:///d:/Projects/my-idm/docs/architecture/browser-integration.md) | MV3 extension, loopback REST server (127.0.0.1:19582), interception, cookies. |
| **Blob URL Handling** | [`blob-urls.md`](file:///d:/Projects/my-idm/docs/architecture/blob-urls.md) | Evaluation of browser `blob:` URLs, process-isolation limits, extension-assisted transfer, and native browser fallbacks. |
| **Capture (Hotkey & Clipboard)** | [`capture.md`](file:///d:/Projects/my-idm/docs/architecture/capture.md) | System-wide `RegisterHotKey` via a native event filter, clipboard capture, the `intercept_all` toggle. |
| **Window & System Tray** | [`window-system-tray.md`](file:///d:/Projects/my-idm/docs/architecture/window-system-tray.md) | Tray lifecycle, minimize/close-to-tray, completion toasts. |
| **Table Views & Segregation** | [`table-views.md`](file:///d:/Projects/my-idm/docs/architecture/table-views.md) | Table model, delegates, status/date/type segregation, section headers, context menus. |
| **YouTube Scraper** | [`youtube-scraper.md`](file:///d:/Projects/my-idm/docs/architecture/youtube-scraper.md) | `yt-dlp`, Mode A (direct CDN) vs Mode B (yt-dlp + ffmpeg), playlists, rate budgeting. |
| **Bandwidth Statistics** | [`statistics.md`](file:///d:/Projects/my-idm/docs/architecture/statistics.md) | `get_download_stats()` local-day bucketing, totals grid, volume chart, sparkline. |

> `queues.md` also designs two things that are **not** implemented: an off-peak scheduler and
> absolute per-download bandwidth caps. Its "current state" sections are accurate; the rest is a
> plan. Do not cite those as existing behaviour.

> When adding a subsystem doc, add a row here **and** to the index in
> [`README.md`](file:///d:/Projects/my-idm/README.md) so the two do not drift.

---

## 📚 General Documentation & References

- **[User Guide](file:///d:/Projects/my-idm/docs/user-guide.md)** — Installation, GUI navigation, settings, workflows.
- **[API Reference](file:///d:/Projects/my-idm/docs/api-reference.md)** — Engines, database schema, manager signals, delegates.
- **[Feature Pool](file:///d:/Projects/my-idm/docs/todo/feature-pool-rdm.md)** — Gap analysis against a reference manager. Read [`docs/todo/`](file:///d:/Projects/my-idm/docs/todo/) for the rest.
- **[Project README](file:///d:/Projects/my-idm/README.md)** — Feature summary, screenshots, setup.
- **Workflows**: [Testing](file:///d:/Projects/my-idm/.agents/workflows/testing.md) · [Qt & UI conventions](file:///d:/Projects/my-idm/.agents/workflows/qt-ui.md)

---

## 🛠️ Key Conventions for Agents

1. **Commit Convention**: Follow standard conventional commit prefixes (`feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `chore:`).
2. **Testing**: Two tiers. Run the **full** suite before committing; run **basic sanity** while
   working, because it is safe to leave in the background.

   | Tier | Command | Scope | Time |
   | :--- | :--- | :--- | :--- |
   | Basic sanity | `run_all_tests.bat basic` | 1599 tests. No window, no tray, no real clipboard. | ~54 s |
   | Full | `run_all_tests.bat` | All 2546 tests. | ~4–7 min |

   **Read [`workflows/testing.md`](file:///d:/Projects/my-idm/.agents/workflows/testing.md) before writing a test or debugging a flake.** It holds the two-tier setup and the `ui` marker, the enforced hermeticity fixtures, the traps that have actually bitten this suite (destructive helpers resolving blank paths to the CWD, the silent `deleteLater()` widget leak, the locked-clipboard flake), the UTC-vs-local determinism rules, and the known-flaky areas.

   Two rules that apply outside the suite as well:
   - **NEVER** clear or write the user's live `QSettings`. `QSettings("MyIDM", "My-IDM").clear()` wipes `HKEY_CURRENT_USER\Software\MyIDM\My-IDM` on Windows, destroying their download folder, UI state and preferences. This applies to debug scripts too, not just the suite. Use isolated settings or an explicit temp `IniFormat` file.
   - **Never read or write the real system clipboard from a test.** `conftest` replaces it with an in-process fake for everything except `ui`-marked tests; the old snapshot/clear/restore implementation clobbered the developer's clipboard on all 2400+ tests, which is why the tier split exists.
3. **Threading Architecture**:
   - Qt GUI runs on the main thread.
   - `HTTPEngine` uses an asyncio event loop running on a dedicated background thread (`idm-async`).
   - `TorrentEngine` uses `libtorrent` session managed via periodic Qt timer polls (`_poll_torrents`).
   - Background scans (antivirus) run on short-lived daemon threads (`scan-<id>`).
   - yt-dlp work runs on plain daemon threads (`ytdlp-download`, `yt-extract`). Never use `QThread` for
     it: destroying a `QThread` whose `run()` is still executing aborts the process, and detaching does
     not help because it is still destroyed at interpreter exit.
4. **No Automatic Git Push (CRITICAL)**:
   - **NEVER** push automatically (`git push`).
   - Only commit changes locally (`git commit`).
   - Pushing to the remote repository must only be done if explicitly instructed by the user.
5. **Adding a Downloads-Table Column**: **Append** a `Col` constant — never insert, or persisted
   `column_widths` / `header_state` / `sort_column` in `ui_state` re-point at the wrong field.
   `NOT NULL DEFAULT ''` column guarded by `PRAGMA table_info`; add it to `_DEFAULT_TAIL_COLUMNS`
   with a default width in both `_setup_ui()` and `_on_reset_view()`; the tail helper must stay an
   **ascending** sweep. → [`workflows/qt-ui.md`](file:///d:/Projects/my-idm/.agents/workflows/qt-ui.md#1-adding-a-downloads-table-column)
6. **File Deletion**: never call `Path.unlink()` or `os.remove()` on user data. Use
   `utils.send_to_trash()` and `utils.unlock_path()`, and stop any worker holding the file first —
   on Windows an open handle makes the file undeletable.
7. **Adding or reordering a Preferences Page**: append to `TAB_ORDER` **and** `TAB_TITLES` in
   `settings_dialog.py`. **Never hard-code a page index** — resolve with `tab_index()`, because
   `SettingsDialog` clamps a stale index and opens the *wrong* page with no error. →
   [`workflows/qt-ui.md`](file:///d:/Projects/my-idm/.agents/workflows/qt-ui.md#2-adding-or-reordering-a-preferences-page)
8. **Adding a `GeneralConfig` Field**: push it to **every** consumer in
   `DownloadManager.set_general_config()`. Both engines hold a `_general_config` and
   `SettingsDialog` hands back a **copy**, so an engine you forget keeps the object it was built
   with at startup and enforces the old value until the next launch — a half-applied preference
   where nothing in the UI looks wrong. Mirror it in `to_dict`, `from_dict`, `save` and `load`.
9. **A `QDialog` opened with `show()` must be owned**: hold a reference on the window, set
   `Qt.WA_DeleteOnClose`, raise the existing instance, and close it in `MainWindow.closeEvent`. →
   [`workflows/qt-ui.md`](file:///d:/Projects/my-idm/.agents/workflows/qt-ui.md#3-a-qdialog-opened-with-show-must-be-owned)
10. **Never let a right-click activate a destructive menu entry**: `_RightClickGuard` must consume
    press, release **and** double-click — filtering only the release still lets the press reach
    `QMenu`'s activation logic. →
    [`workflows/qt-ui.md`](file:///d:/Projects/my-idm/.agents/workflows/qt-ui.md#4-never-let-a-right-click-activate-a-destructive-menu-entry)
11. **A stretched list must not end mid-row**: snap `_WholeRowListWidget` from the **widget's**
    height, never the viewport's, or the transform is non-monotonic and the list can never grow
    back. →
    [`workflows/qt-ui.md`](file:///d:/Projects/my-idm/.agents/workflows/qt-ui.md#5-a-stretched-list-must-not-end-mid-row)
12. **Toolbar labels may be abbreviated; menu labels may not.** A shortcut lives on exactly one
    `QAction`, so a menu may keep its own copy of a shared label. →
    [`workflows/qt-ui.md`](file:///d:/Projects/my-idm/.agents/workflows/qt-ui.md#6-toolbar-labels-may-be-abbreviated-menu-labels-may-not)
13. **Apply the theme before building widgets, never after** — `Colors` resolves at stylesheet
    *construction*, so the wrong order leaves widgets drawn in the previous palette. →
    [`workflows/qt-ui.md`](file:///d:/Projects/my-idm/.agents/workflows/qt-ui.md#7-apply-the-theme-before-building-widgets-never-after)
14. **Do not bundle a palette refactor into a theme selector.** Dark is byte-for-byte unchanged and
    must stay provably so; any palette conversion must be 1:1. →
    [`workflows/qt-ui.md`](file:///d:/Projects/my-idm/.agents/workflows/qt-ui.md#8-do-not-bundle-a-palette-refactor-into-a-theme-selector)
15. **Verify Against Real Data**: when a change touches persistence, column layout, or anything the user
    sees, confirm it against a copy of the real `~/.my-idm/downloads.db` or a rendered screenshot
    rather than trusting mocks alone.
16. **The AnimePahe settings buttons are intentionally silent on success** — the button label and
    the footer badge already show the new state, and only failures raise a dialog. Do not "fix"
    this by adding a success alert.
17. **Verify every `file:line` you write into a doc.** Roughly a dozen were stale in the first pass
    of `docs/todo/feature-pool-rdm.md`, several by 30+ lines. A wrong line number is worse than
    none: it reads as verified. Re-check against the tree, and mark any table of corrections as
    point-in-time.
