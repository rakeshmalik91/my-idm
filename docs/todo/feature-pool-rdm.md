# Feature Pool

Gap analysis against a reference implementation, for prioritisation only — not a commitment
to build any of it.

- **Reference:** `https://github.com/ratdon/RDM/blob/main/README.md` — "Raw Download Manager (RDM)", a Go + Wails
  download manager. Read as a *feature checklist*; nothing from its architecture, language
  or licence is applicable to this Python/Qt codebase.
- **Method:** every verdict below was checked against `my_idm/` source, not against our own
  README. That was still not sufficient — two verdicts had to be reversed on re-audit, so see
  [Re-audit notes](#re-audit-notes) before trusting any single entry here.
- **Date:** first pass 2026-09-30 (suite then 1724 tests, 85% coverage); re-audited
  2026-10-01 against 2335 tests; §12 implemented 2026-10-02; **re-audited 2026-10-07**
  against 3075 tests.

## Re-audit notes

The audit has been verified across multiple iterations as features landed and tests expanded:

- **2026-10-01:** §4 Categories was corrected from MISSING to HAVE (implemented via
  `TYPE_CATEGORY_EXTENSIONS` and `get_entry_type_category`). Bandwidth stats (§3) was corrected in
  the summary table from MISSING to DONE.
- **2026-10-02:** §12 Segment start staggering was implemented and flipped to DONE.
- **2026-10-07:**
  - **§17 Cross-Platform was recorded "MISSING by decision"** while Phases 0–3 of cross-platform
    support were fully designed and shipped across Linux and macOS (3-OS CI matrix green,
    `my_idm/paths.py`, `my_idm/proc.py`, `my_idm/autostart.py`, native tray & notifications, ClamAV
    auto-detection, font resolvers). Flipped to **DONE (Phases 0–3 shipped)**.
  - **Per-queue bandwidth limits** (`download_limit`, `upload_limit` on `QueueInfo`) shipped, and
    the allocation fraction table was hoisted to `utils.effective_rate_limit` (`utils.py:170`),
    resolving the dead fraction calculation bug when global limit is 0.
  - **Test suite count updated:** grew from 2335 tests to 3075 tests collected.
  - **Line numbers refreshed:** all `file:line` citations throughout this document re-verified
    against the active codebase (see [Claims to correct](#claims-to-correct)).

So, if you extend this document:

1. **Check the whole neighbourhood, not the obvious name.** `grep` for the concept, not for the
   identifier you expect. Every MISSING verdict below has been re-checked against a specific
   symbol this way.
2. **Verify every `file:line` before committing it.** Line numbers drift as the codebase grows.
   Every citation below is point-in-time verified against current sources.
3. **Count tests, don't recall them.** Always verify exact numbers against `pytest --collect-only`.
4. **A feature shipped in the same period this document was written is the likeliest source of a
   wrong verdict.** Re-audit before prioritising anything.

## Summary

Re-audited 2026-10-07 against 3075 tests.

| Verdict | Count | Features |
| --- | --- | --- |
| **HAVE** | 5 | duplicate detection, history + search, exponential backoff, Firefox extension base, categories / file-type segregation (§4) |
| **DONE** | 8 | **disk-space check** (§2), **bandwidth stats** (§3), **global hotkey [Win]** (§6), **clipboard monitoring** (§7), **named queues** (§1), **segment start staggering** (§12), **cross-platform core [Phases 0–3]** (§17), **URL editing / refresh expired link** (§18) |
| **PARTIAL** | 5 | resume UI gating (§9), adaptive segment sizing (§13), CLI subcommands / headless (§14), per-download absolute limits (§8), Firefox extension `browser.*` fallback (§16) |
| **MISSING** | 1 | **download scheduler (off-peak hours)** (§5) |
| **REJECTED / N/A** | 2 | HTTP/2 multiplexing (§10), memory-mapped segment merge (§11) |

---

## HIGH value — large, visible wins

### 1. Multiple named queues — DONE
RDM: "Create custom queues with independent parallel download limits."

**Implemented 2026-10-01.** Full design and semantics in
[Named Queues & Concurrency Budgets](../architecture/queues.md).

- Every download carries a `queue_id`; a queue carries its own `max_concurrent`. This is a
  *new column beside* `queue_order`, not a replacement for it: `queue_order` is priority
  *within* a queue and its `0` is a meaningful "not queued" sentinel.
- `max_concurrent_downloads` stays as a **global ceiling**; a queue's budget is a local ceiling.
  A download starts only when both allow it. The check is one method (`manager._may_start`,
  `manager.py:2098`) because three call sites reach the engines without passing through each
  other (`add_download` at `manager.py:1958`, `_process_queue` at `:2170`, and `resume_download` at
  `:2474`).
- **The per-queue count replaced an O(n²) scan.** `_get_active_download_count()` did a full
  table scan per candidate; the grouped `GROUP BY queue_id` is *cheaper* than the single
  global count it replaced (`database.py:925`, `manager.py:2085-2092`).
- Dispatch order: priority within a queue; a queue with budget before a saturated one; ties on
  the user's switcher order, never on the queue id (uuids would make it nondeterministic).
- UI: **Edit → Queues** (the scope switcher, which also holds New Queue… / Manage Queues… — a
  toolbar combo beside the search box was removed on 2026-10-02), a **Move to Queue** context
  submenu, and a
  `QueueManagerDialog`. `queue_id=''` resolves to Default on every write, so no reader has to
  treat empty as meaningful.

Deliberate non-breaking choice: the scope starts at **"All Queues"**, because history is the
product and a user must never find existing downloads missing because a queue is selected.

Covered by `tests/test_queues.py` (81 tests).

### 2. Disk-space check before starting — DONE
RDM: "Validates free space before starting download."

Was: nothing called `shutil.disk_usage` anywhere; `psutil` was only used for NIC
enumeration (`network.py:171-173`).

Implemented as `utils.check_disk_space` / `utils.get_free_disk_space`, enforced by
`HTTPEngine._enforce_disk_space` (`http_engine.py:829`, right after the probe, when the size is
authoritative) and `TorrentEngine._enforce_disk_space` (`torrent_engine.py:465`, first poll where a
magnet's size is known — it cannot live in `add_torrent`, because the manager overwrites the status
and message a failure there would set). Settings: `GeneralConfig.disk_space_check` (on by default)
and `disk_space_headroom_mb` (256), exposed in Preferences → General beside the BitTorrent
suspend timeout.

Two rules the tests pin:

- only the **remaining** bytes are required, so a resume holding 30 GB of a 40 GB file needs
  10 GB more, not 40 — otherwise every resume of a large download is refused;
- a volume whose free space **cannot be read** never blocks anything. Refusing a download
  because we could not obtain a number would be worse than letting it try.

A refused torrent is paused and stripped of `auto_managed`, so libtorrent can neither keep
writing nor restart it on its own, and the check is re-run on resume rather than being
marked done. Covered by `tests/test_disk_space.py` (50 tests).

### 3. Bandwidth statistics — DONE
RDM: "Live status bar with daily/monthly/yearly/lifetime download totals."

**Implemented** — see [Bandwidth Statistics](../architecture/statistics.md) for the
design, and `my_idm/stats_dialog.py` for the implementation.

- `Database.get_download_stats(today, daily_days=30)` (`database.py:1302`) reads every bucket in
  one call and returns a `StatsSnapshot` (`database.py:72`, frozen value objects). `today` is
  **injected** (`stats_dialog.py:420`), which is what makes the date arithmetic assertable
  without a frozen clock. Buckets use `substr(added_at,1,10)` rather than `strftime`, plus a
  `GLOB` guard on the date prefix: the cut-off is a *string* compare, so a hand-edited
  `not-a-date` sorts after `2026-09-24` and would otherwise be filed under *today*.
- A `📊 Statistics…` entry in **Tools**, grouped with **Preferences**, opening a read-only popup
  with the totals grid, a stacked per-day volume chart, and a live speed sparkline. (It was a
  toolbar button beside Preferences until 2026-10-02; the strip is transport and file commands
  only now.)
- Charts are `QPainter`-drawn, not matplotlib: matplotlib and numpy are importable here but
  are **absent from `requirements.txt`**, so depending on them would work on the author's
  machine and fail for anyone installing from the requirements file.
- **Prerequisite fixed:** `completed_at` was re-stamped whenever a download left `seeding`,
  so "completed today" was already wrong for every torrent whose seeding was stopped by
  hand, by the ratio limit or the duration limit. `update_status` now stamps only when the
  column is empty (`database.py:1156`: `CASE WHEN completed_at = '' THEN ? ELSE completed_at
  END`), so the first completion wins.

Covered by `tests/test_statistics.py` (130 tests).

### 4. Categories — auto-sort downloads by file type — HAVE
RDM: "Auto-sort downloads by file type (Videos, Archives, Documents, etc.)."

Was recorded here as MISSING. That verdict was **stale**: the feature shipped, and it has the
exact shape this entry proposed to build — a `category_for_filename()` helper plus a
segregation mode, mirroring `get_entry_date_category()`. Re-checked 2026-10-01 and 2026-10-07.

- `TYPE_CATEGORY_EXTENSIONS` (`download_model.py:286-325`) maps ~95 lower-case extensions into
  six buckets. `get_entry_type_category()` (`:367-399`) reads `filename`, falls back to the
  leaf of `file_path` so a torrent whose *root folder* carries the extension still classifies,
  and honours a per-entry `metadata["type_category"]` override.
- `"type"` is one of the three segregation modes (`SEGREGATED_MODES`, `:338`), so it is
  reachable from **View → Segregated View** and **Preferences → Views**, and composes with the
  header filter chips. Covered by `tests/test_views_tab.py`.

The trap that made this look absent: there are **two unrelated "type" notions** in the model.
The filter chip is `TYPE_FILTER_LABELS` (`:211`) and is keyed on *engine*
(`http` / `torrent`), not on file extension. The extension-derived one is the
`SECTION_TYPE_*` family. Do not read the first and conclude the second is missing.

Residual, none of which is "implement the feature":

- **Multi-file torrents are never categorised.** A torrent is one `downloads` row; its file
  list lives inside `metadata_json["files"]`. One torrent therefore lands in exactly one
  section. Closing this needs a schema decision (a `files` table), not a helper.
- **The `type_category` override has no UI writer** — only the read and the tests. A
  mis-detected item can be filed by hand only through `metadata_json`.

*(Note: Expand All / Collapse All and section header accent colors for File Type mode were resolved on 2026-10-03).*

---

## MEDIUM value

### 5. Download scheduler (off-peak hours) — MISSING
RDM: "Schedule downloads for off-peak hours" with `schedule_start` / `schedule_end`.

No time-of-day gate exists in `manager._process_queue()` (`manager.py:2115-2175`). The
only "schedule" in the codebase is retry backoff (`metadata["next_retry_at"]`, an absolute
epoch instant) and the AnimePahe scraper's 6-hour re-run cadence. Neither is a recurring
local-time window, so there is no precedent to copy — the design has to introduce one.

Shape: two config fields plus a guard in `_process_queue`. The manager already has a 1 Hz
`QTimer` (`_retry_timer`, `manager.py:497`, driving `_process_retry_queue` at `:3785`) driving
the queue, so there is a natural place to gate it. Note the guard must **also** be applied to the
two inline gates that bypass `_process_queue()` entirely — `add_download()` (`manager.py:1800`,
gate at `:1958`) and `resume_download()` (`manager.py:2437`, gate at `:2474`) — or the window
leaks.

Designed together with the other two members of this cluster; see
[Named Queues & Concurrency Budgets](../architecture/queues.md) — which specifies the
`_may_start_now()` gate, the injectable clock, and the UTC-vs-local trap, in full.

### 6. Global hotkey to toggle capture — DONE
RDM: "Scroll Lock toggles capture on/off system-wide."

**Implemented 2026-10-01** as `my_idm/hotkey.py`. Full design, including the parsing traps and
the extension-sync limit, in [Capture Subsystem](../architecture/capture.md).

- `HotkeyRegistration` owns one `RegisterHotKey` chord and turns it into a Qt
  `Signal`. Qt never surfaces `WM_HOTKEY`, so a `QAbstractNativeEventFilter` picks the message
  out of the native event stream (`hotkey.py:_NativeHotkeyFilter`) and re-emits it. Raw
  `ctypes` behind `sys.platform == "win32"`, matching every other Windows call in the app —
  no `pywin32` dependency.
- **Parsing works on the stored string, not on `QKeySequence`.** Qt drops a Meta/Win chord
  entirely (`QKeySequence("Win+D")` is empty) and encodes F-keys and arrows in a private
  `0x01xxxxxx` range that does not line up with Win32 virtual-key codes. Letters go through
  `VkKeyScanW` in lower case, because `VkKeyScanW('D')` reports "needs shift" for the
  character `D`, which would silently turn `Ctrl+Alt+D` into `Ctrl+Alt+Shift+D`.
- **A chord with no Ctrl/Alt/Win is refused**, not registered. Claiming a bare key system-wide
  would swallow it in every other application on the desktop.
- `ERROR_HOTKEY_ALREADY_REGISTERED` is reported to the user rather than swallowed — a chord
  owned by another app must say so instead of leaving a key that does nothing.
- The toggle flips `BrowserIntegrationConfig.intercept_all`, **not** `enabled`. `enabled=False`
  tears the loopback REST server down, so the extension could no longer reach the app to be
  told *why* it was declined. `intercept_all` is the field the extension already gates
  interception on, so it is the one that means "stop intercepting".
- Exposed as a Preferences → General toggle with a `QKeySequenceEdit` (the first in this
  codebase) and as a checkable **🎯 Download Capture** tray row.
- Known limit, stated rather than hidden: the extension re-syncs `intercept_all` from
  `GET /config` on a 30 s timer, so **browser-native** interception converges within that
  window. Anything sent to the app is declined immediately, because
  `BrowserServer._handle_add` reads the same config object.

Not done: the extension-side `chrome.commands` block (the cross-platform half). It would be
overwritten by the 30 s config sync on every tick unless the app-side state agreed, so it adds
little while the hotkey is the primary mechanism.

Covered by `tests/test_capture.py`.

### 7. Clipboard monitoring — DONE
RDM: "Auto-detect downloadable URLs from clipboard."

**Implemented 2026-10-01** as `my_idm/clipboard_monitor.py`. Full design, including the
suppression queue and the per-line (not per-payload) length cap, in
[Capture Subsystem](../architecture/capture.md).

- Subscribes to `QClipboard.dataChanged`, debounces with a **single-shot** `QTimer` restarted
  on every change (a burst of writes coalesces into one read), and hands captureable text to
  `manager.add_download()`.
- **The gate is all-or-nothing:** a copy is accepted only when *every* non-blank line is a
  downloadable URL. This is the policy `AddDownloadDialog._prefill_url` already used; both now
  call one `looks_like_download_url()`, so the pre-fill gate and the auto-capture gate cannot
  drift apart.
- **The app's own clipboard writes are suppressed.** `MainWindow._on_copy_url` puts selected
  rows' URLs on the clipboard. Deduping inside `add_download` is *not* enough: re-adding a
  **paused** download calls `resume_download` on it, so Ctrl+C on a paused row would silently
  start it. `suppress()` is therefore called by the writer.
- Per-copy ceiling, configurable (default 20) with a hard `ABSOLUTE_MAX_URLS` of 200, so a
  pasted generated list cannot become thousands of rows at once.
- **Off by default.** Reading the clipboard without being asked is surveillance; a user who is
  surprised by it turns it off and does not turn it on again.
- `GeneralConfig.clipboard_monitor_enabled` / `..._max_urls`, a Preferences → General group,
  and a checkable **📋 Clipboard Capture** tray row.

Covered by `tests/test_capture.py`.

### 8. Per-download bandwidth limit in absolute units — PARTIAL
RDM: "Global and per-download bandwidth limits."

We support per-download priority scaling, but only as a **fixed fraction of the tighter of the
queue or global limit** (`utils.BANDWIDTH_ALLOCATION_FRACTIONS`, `utils.py:163-167`:
low 25% / medium 50% / high 75% / max 100%, resolved in `utils.effective_rate_limit`, `:170-194`).
Per-queue bandwidth ceilings (`download_limit`, `upload_limit` on `QueueInfo`) shipped, but two
limitations remain:

- there is no way to specify an absolute limit for a specific file ("this one download gets 2 MB/s")
  independent of the queue or global budget;
- enforcement is a per-segment chunk pacer, duplicated four times (`http_engine.py:961`, `:1090`,
  `:1232`, `:1377`), so it is approximate and each segment independently sleeps
  `chunk_len / eff_limit` — a segmented download overshoots its nominal cap by roughly
  `num_segments`× (8 by default). There is no aggregated token bucket across concurrent segments.

Design is in [Named Queues & Concurrency Budgets](../architecture/queues.md) §Part 3, including why
`rate_limit_bps` lives in `metadata_json` rather than a column, and why `unset` and `0` must stay
distinct.

### 9. Resume auto-detection surfaced in the UI — PARTIAL
RDM: "Auto-detects resumable downloads; disables pause/cancel for non-resumable ones."

Detection exists and is correct: `http_engine.py:764-825` (`_probe_url`) probes and reads
`Accept-Ranges` (`:787`, `:807`, `:820` for the three transport paths). But `supports_range` is a
**local variable** — never persisted to the entry, never shown, and `_on_pause`
(`main_window.py:2108`) fires unconditionally.

Shape: persist the flag in `metadata["supports_range"]`, disable Pause in the row/context menu (or
prompt a confirmation warning that pausing will restart the download from zero) when false. Small
and it removes a class of user confusion ("why did it restart from zero?").

---

## LOW value / large effort

### 10. HTTP/2 multiplexing — REJECTED
RDM: "Single connection, multiple streams for same-origin segments."

Every segment is an independent HTTP/1.1 request (`http_engine.py:1047` for the aiohttp path,
`:927` for the `curl_cffi` one). No `h2` or `httpx[http2]` dependency.

Assessment: **expensive and a genuine downgrade risk here.** We deliberately fall back to
`curl_cffi` with `impersonate="chrome124"` for Cloudflare-protected hosts
(`http_engine.py:781`, `:814`, `:927`, `:1190`), and impersonation is the whole reason curl_cffi is
in the dependency tree. Hand-rolling HTTP/2 would mean re-implementing browser TLS fingerprints,
which is a large, fragile surface. Rejected.

### 11. Memory-mapped merge — REJECTED (N/A)
RDM: "Uses mmap for large file segment merges."

There is no merge phase to optimise: `_segmented_download` pre-allocates the target with
`truncate(total_size)` (`http_engine.py:858`) and each segment `seek()`s into place
(`:953`, `:1082`). Segments are written **directly at their final offset**, so a separate mmap
merge would be an extra full-file pass, not a faster one.

Worth noting in the pool so it is not re-proposed: the design RDM optimises for is not the
design we have.

### 12. Segment start staggering — DONE
RDM: "Staggered segment starts" (`segment_start_delay_ms: 150`).

**Implemented 2026-10-02.** `GeneralConfig.segment_start_delay_ms`, read by the engine through
the config object `set_general_config_sync` already passes, and exposed in Preferences →
General beside the segment count.

- `_segmented_download` ranks the **pending** segments in `idx` order and hands segment *n* a
  start delay of *n* × the step (`http_engine.py:892`); `_download_one_segment` sleeps for that
  slice *before* its retry ladder. Both transports get it for free — the curl path is called from
  inside the same method.
- **Rank 0 does not sleep at all**, not even `asyncio.sleep(0)`. A 0 ms preference has to
  leave the request pattern byte-for-byte what it always was, including costing no
  event-loop turn.
- **The step is scaled down, not the tail truncated**, so the last segment still starts
  within `MAX_SEGMENT_STAGGER_TOTAL_S` (2 s). Truncating the tail would bunch the remaining
  segments back up at the ceiling — the exact burst the feature exists to remove. The
  scaling is logged, not silent.
- **Default is 0, not RDM's 150 ms.** The benefit is speculative and unobserved in this
  codebase; the cost is certain — the last of N segments waits (N-1) × delay before its
  first byte, which is a large fraction of a small download. Raise it when a host actually
  answers a starting download with 429/503.
- A pause landing during the wait is honoured: the retry ladder's `cancel_evt` check that
  already opens each attempt covers it, so a waiting segment never issues its request.
  The un-delayed first segment is the documented exception — it has no wait to interrupt.
- **The wait sits outside the retry ladder on purpose.** Two traps fall out of that: a retry
  is not charged the stagger twice, and a cancel during the wait stays a `CancelledError`
  instead of being caught as a transport failure. It is also what keeps the 416/403
  single-stream fallback from waiting out the stagger — the parked tasks are cancelled while
  parked, which `tests/fake_http.py`'s one-turn sleep could not have proven, so that test
  parks for real.

Covered by `TestSegmentStagger` in `tests/test_http_engine_download.py` (12 tests). Launch is
`http_engine.py:893-896` and the gather is `:899`.

### 13. Segment count adapted to file size — PARTIAL
RDM: `max_segments_per_file`.

Segment *boundaries* come from the probed size (`http_engine.py:714` gates on it, `:1164`
places each segment) but the *count* is fixed by config (`DEFAULT_SEGMENTS = 8`,
`http_engine.py:40`) and never adapts. There is one coarse gate —
`total_size > CHUNK_SIZE * 4` (256 KiB, `:714`, `CHUNK_SIZE` = 64 KiB at `:41`) — after which
a 300 KiB file still gets 8 segments. A `min_segment_size` (e.g. 1 MiB or 2 MiB) would make the
count adaptive (`num_segments = min(configured, max(1, total_size // min_segment_size))`).

### 14. CLI subcommands — PARTIAL
We have `[project.scripts] my-idm = "my_idm.main:main"` with argparse
(`main.py:53-89`), but no subcommands: no `add`, no `status`. The GUI always launches
(`main.py:108`, `:240`), so it cannot be scripted against.

Shape: a `--json`/`status` flag or headless subcommands (`status`, `add`) that interact with the
database/IPC and exit before `QApplication` is constructed.

### 15. Absolute per-download limits on disk + multiple named queues
The named-queue half shipped with #1 (and queue bandwidth limits shipped with commit `cd737fc`).
The *absolute per-download limits* half is still designed only, together with #5, in
[Named Queues & Concurrency Budgets](../architecture/queues.md#not-implemented).

### 16. Firefox `browser.*` namespace with `chrome.*` fallback — PARTIAL
`background.js` uses the `chrome.*` namespace throughout. Gecko aliases it so this works,
but there is no fallback for anything Gecko has not aliased, and
`background.js:115-119` already notes the magnet `targetUrlPatterns` menu item is
best-effort on Firefox.

### 17. Cross-platform (macOS / Linux) — DONE (Phases 0–3 shipped)
Was recorded in earlier passes as "MISSING by decision". That verdict was **reversed**: My-IDM is
now fully cross-platform with Phases 0–3 implemented and active. Full design and specification in
[Cross-Platform Architecture](../architecture/cross-platform.md).

- **CI Matrix:** 3-OS GitHub Actions matrix (`ubuntu-latest`, `macos-latest`, `windows-latest`)
  running hermetically and passing on all three.
- **Ported Subsystems:**
  - Reveal / open file across OS-native handlers (`xdg-open`, `open`, `explorer`).
  - Native notification backends (`notify-send` on Linux, `osascript` on macOS, `win10toast` on Windows).
  - Data paths: `my_idm/paths.py` handles XDG base directories on Linux, `~/Library/Application Support`
    on macOS, and `%LOCALAPPDATA%` / `~/.my-idm` on Windows.
  - Process spawning: `my_idm/proc.py` detaches processes portably on POSIX without Win32 `creationflags`.
  - Launch at login: `my_idm/autostart.py` supports XDG `.desktop` autostart files (Linux) and `launchd`
    user agents (macOS) alongside Windows registry `HKCU\...\Run`.
  - Antivirus: tri-state fail-closed verdict and ClamAV auto-detection on POSIX.
  - Desktop identity: `my-idm.desktop` and `app.setDesktopFileName("my-idm.desktop")` for GNOME/KDE.
  - Typography: `my_idm/fonts.py` resolves platform-appropriate font stacks.
- **Residual backlog:** Platform-native global capture hotkeys for Linux (X11 / Wayland portal) and
  macOS (Carbon), and standalone distribution packaging (AppImage, DMG).

### 18. URL editing / Refresh expired address — DONE
RDM: "Refresh download link / Update download address for expired URLs."

When an HTTP download URL expires mid-transfer (common with Google Drive, cloud storage, and
token-gated CDNs answering 403 or 410), users can now update the download URL on existing
`DownloadEntry` instances directly from the GUI or manager without losing already downloaded
bytes or segments.

- **Implemented Components:**
  - `Database.update_download_url(download_id, new_url)` updates the `url` column in SQLite
    in-place while preserving all segment progress.
  - `DownloadManager.update_download_url(download_id, new_url, resume=False)` validates HTTP/HTTPS
    scheme, ensures `explicit_filename: True` is stored in entry metadata (so resuming against the
    new URL does not overwrite the existing partial target filename or on-disk path), resets error
    states and retry counters if the download previously failed with an expired link error, emits
    `download_url_updated`, and optionally resumes immediately.
  - `DownloadTableModel.update_url(download_id, new_url)` updates in-memory model entries and emits
    `dataChanged` for instant table and details view updates.
  - `RefreshAddressDialog` in `my_idm/dialogs.py` displays current address, provides input validation,
    and a checkbox to resume download immediately upon acceptance.
  - `MainWindow` integrations in both the Edit menu and table context menu (**"Refresh Address…"**),
    enabled for single non-active HTTP downloads.

---

## Claims to correct

Found while auditing, and worth fixing in our own docs:

- ~~Our README over-claims.~~ **Retracted 2026-10-01.** The original pass asserted this in the
  Method line without evidence. The README's **5-Tab Details Panel** headline is accurate —
  Overview / Files / Peers & Swarm / Trackers / Segments all exist — and RDM's speed-graph
  screenshot having no counterpart here is an asymmetry, not a defect. The genuine over-claim was
  `docs/architecture/state-machines.md`, below.
- ~~`docs/architecture/state-machines.md:52` mentions HTTP/2 in prose.~~ **Fixed
  2026-10-01.** The sentence now says HTTP/1.1 over `aiohttp`, with the `curl_cffi`
  impersonation fallback named instead. Verified by grep: the string `HTTP/2` has no hit in
  `my_idm/` at all.
- **Resolved:** §4 Categories was recorded MISSING against a stale snapshot. See that
  section — the feature is implemented and the doc's own cited anchor pointed *into* the
  extension table that implements it.
- **Stale line references throughout this file**, re-audited and updated **2026-10-07** against the
  tree. These are point-in-time; `my_idm/` moves, so re-check before citing any of them:

  | Symbol / Reference | 2026-10-01 Citation | Current Location (2026-10-07) |
  | --- | --- | --- |
  | `queue_order` DDL & dataclass | `database.py:543`, `:475`, `:353` | `database.py:454` (dataclass), `:580` (`_DOWNLOAD_DB_COLUMNS`), `:648` (DDL), `:700` (migration) |
  | `StatsSnapshot` | `database.py:67` | `database.py:72` |
  | `Database.get_download_stats` | `database.py:808` | `database.py:1302` (injected in `stats_dialog.py:420`) |
  | `completed_at` first-completion wins | `database.py:673` | `database.py:1156` |
  | `manager._may_start()` gate | `manager.py:1777` | `manager.py:2098-2113` (called at `:1958`, `:2170`, `:2474`) |
  | `_process_queue()` | `manager.py:1787-1827` | `manager.py:2115-2175` |
  | `_retry_timer` / `_process_retry_queue` | `manager.py:410-412` | `manager.py:497` (`_retry_timer`), `:3785` (`_process_retry_queue`) |
  | `move_queue_up` / `move_queue_down` | `manager.py:2608-2638` | `manager.py:3005-3023` |
  | `get_entry_date_category()` | `download_model.py:417` | `download_model.py:431-455` |
  | `get_entry_type_category()` | `download_model.py:353-385` | `download_model.py:367-399` |
  | `TYPE_CATEGORY_EXTENSIONS` | `download_model.py:272-311` | `download_model.py:286-325` |
  | `TYPE_FILTER_LABELS` (engine) | `download_model.py:197-200` | `download_model.py:211` |
  | Bandwidth allocation fraction table | `http_engine.py:80-86` | `utils.py:163-167` (`BANDWIDTH_ALLOCATION_FRACTIONS`), resolved in `utils.effective_rate_limit` (`:170-194`) |
  | Chunk pacers (4 sites) | `http_engine.py:868`, `:983`, `:1122`, `:1267` | `http_engine.py:961`, `:1090`, `:1232`, `:1377` |
  | HTTP segment probe & `Accept-Ranges` | `http_engine.py:686-741`, `:707` | `http_engine.py:764-825` (`Accept-Ranges` at `:787`, `:807`, `:820`) |
  | Segment task creation & gather | `http_engine.py:857-861`, `:963` | `http_engine.py:893-896` (launch), `:899` (gather) |
  | File pre-allocation `truncate` / `seek` | `http_engine.py:778`, `:860`, `:975` | `http_engine.py:858` (`truncate`), `:953`, `:1082` (`seek`) |
  | `_on_pause()` in UI | `main_window.py:1726` | `main_window.py:2108` |
  | CLI `parse_args` and GUI exec | `main.py:49-80`, `:99`, `:212` | `main.py:53-89` (`parse_args`), `:108` (`QApplication`), `:240` (`app.exec`) |

- **Cross-platform verdict reversal:** §17 was originally marked "MISSING by decision". It was
  updated to **DONE (Phases 0–3 shipped)** to reflect the ported subsystems, 3-OS CI matrix green,
  and documentation in `docs/architecture/cross-platform.md`.
- **Suite growth:** The test suite expanded from 1724 (first pass) to 2335 (2026-10-01) to **3075
  tests** (2026-10-07).

---

## Already rejected

| Idea | Why not |
| --- | --- |
| mmap merge (#11) | No merge phase exists; segments are written at final offsets. Would add a pass, not remove one. |
| HTTP/2 (#10) | Would forfeit the `curl_cffi` browser-impersonation path that gets Cloudflare downloads through. |
| `find_by_info_hash` case-insensitivity | Already tracked in `tests/test_infra_hardening.py`; libtorrent hashes are lowercase hex, so the case-sensitive SQL is correct as written. |

---

## Actionable Task List from Feature Pool

The table below consolidates the remaining actionable feature tasks identified by the audit, prioritized by user value and implementation feasibility:

| # | Task | Target Subsystems | Complexity | Priority | Description |
|---|---|---|---|---|---|
| **T1** | **Download scheduler (off-peak hours)** | `manager.py`, `config.py`, `settings_dialog.py` | Medium | **High** | Recurring time-of-day window (`schedule_start`, `schedule_end`) gating `_process_queue()`, `add_download()`, and `resume_download()`. Automatically wakes queued downloads when entering the window and pauses/holds them when leaving. |
| ~~**T2**~~ | ~~**URL editing / Refresh expired address**~~ | `database.py`, `manager.py`, `main_window.py` | Low–Med | **DONE** | **Shipped:** `Database.update_download_url`, `DownloadManager.update_download_url`, `RefreshAddressDialog`, and `MainWindow` Edit & context menu actions. Preserves on-disk bytes/segments and resumes cleanly. |
| **T3** | **Resume capability surfaced in GUI** | `http_engine.py`, `database.py`, `main_window.py` | Low | **Medium** | Persist `supports_range` flag from probe into entry metadata. Warn user before pausing non-resumable downloads (which restart from 0%), or disable the pause button. |
| **T4** | **Adaptive segment count based on file size** | `http_engine.py`, `config.py`, `settings_dialog.py` | Low | **Medium** | Implement `min_segment_size` (e.g. 1–2 MiB) so small downloads (<1 MiB) do not spawn 8 redundant connections. `num_segments = min(configured, max(1, total_size // min_segment_size))`. |
| **T5** | **Per-download absolute bandwidth limits** | `http_engine.py`, `utils.py`, `main_window.py` | Medium | **Medium** | Allow setting specific download caps (e.g. 2 MB/s) via entry metadata `rate_limit_bps` independent of global limits. Implement token-bucket pacing across segments to prevent N× overshoot. |
| **T6** | **CLI headless scripting subcommands** | `main.py`, `manager.py` | Medium | **Low** | Provide CLI subcommands (`my-idm status --json`, `my-idm add <url>`) that query/interact with the database/IPC and exit without launching `QApplication`. |
| **T7** | **Firefox extension fallback handling** | `browser_extension/background.js` | Low | **Low** | Dual-namespace wrapper or polyfill for Firefox WebExtension compatibility where Gecko does not alias Chrome APIs (e.g., scheme restrictions in `targetUrlPatterns`). |
| **T8** | **Native global hotkeys on Linux/macOS** | `my_idm/hotkey.py` | High | **Backlog** | Add X11 / XDG GlobalShortcuts portal (Wayland) and macOS Carbon event listeners to bring global capture hotkey parity with Windows `RegisterHotKey`. |
