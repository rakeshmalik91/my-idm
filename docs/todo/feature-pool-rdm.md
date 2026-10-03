# Feature Pool

Gap analysis against a reference implementation, for prioritisation only — not a commitment
to build any of it.

- **Reference:** `https://github.com/ratdon/RDM/blob/main/README.md` — "Raw Download Manager (RDM)", a Go + Wails
  download manager. Read as a *feature checklist*; nothing from its architecture, language
  or licence is applicable to this Python/Qt codebase.
- **Method:** every verdict below was checked against `my_idm/` source, not against our own
  README. That was still not sufficient — two verdicts had to be reversed on re-audit, so see
  [Re-audit notes](#re-audit-notes) before trusting any single entry here.
- **Date:** first pass 2026-09-30 (suite then 1724 tests, 85% coverage); **re-audited
  2026-10-01** against 2335 tests; §12 implemented 2026-10-02.

## Re-audit notes

Two verdicts in the first pass were wrong, and both failed the same way: the entry described
the *absence of a name* rather than the absence of a feature.

- **§4 Categories was recorded MISSING while the extension table implementing it sat at the line
  number the entry itself cited.** `get_entry_date_category()` was named as the thing to mirror,
  and `download_model.py:288` — inside that entry — is a row of `TYPE_CATEGORY_EXTENSIONS`.
- **The Method line claimed our README over-claims.** It does not. The actual over-claim was in
  `docs/architecture/state-machines.md`, and the README's 5-Tab Details Panel is real.

So, if you extend this document:

1. **Check the whole neighbourhood, not the obvious name.** `grep` for the concept, not for the
   identifier you expect. Every MISSING verdict below has been re-checked against a specific
   symbol this way.
2. **Verify every `file:line` before committing it.** Roughly a dozen were stale in the first
   pass, several by more than thirty lines. A wrong line number is worse than none: it reads as
   verified. The corrections are tabulated under "Claims to correct".
3. **Count tests, don't recall them.** Two test-count claims here were wrong.
4. **A feature shipped in the same period this document was written is the likeliest source of a
   wrong verdict.** Re-audit before prioritising anything.

## Summary

Re-audited 2026-10-01. The original pass was written against a stale snapshot: it recorded
**categories** as missing when they are implemented (§4), and counted **bandwidth stats** as
missing in the table while §3 documented them as done.

| Verdict | Count | Features |
| --- | --- | --- |
| **HAVE** | 6 | duplicate detection, history + search, exponential backoff, Firefox extension, resume *detection*, **categories / file-type segregation** |
| **DONE** | 6 | **disk-space check** (2026-09-30), **bandwidth stats**, **global hotkey** (2026-10-01), **clipboard monitoring** (2026-10-01), **named queues** (2026-10-01), **segment start staggering** (2026-10-02) |
| **PARTIAL** | 7 | resume UI gating, CLI, keep-alive reuse, per-download limits, segment sizing, memory-mapped merge\*, CLI subcommands\* |
| **MISSING** | 4 | HTTP/2, mmap merge, scheduler, URL editing |

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
  A download starts only when both allow it. The check is one method (`manager._may_start`)
  because three call sites reach the engines without passing through each other.
- **The per-queue count replaced an O(n²) scan.** `_get_active_download_count()` did a full
  table scan per candidate; the grouped `GROUP BY queue_id` is *cheaper* than the single
  global count it replaced.
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
`HTTPEngine._enforce_disk_space` (right after the probe, when the size is authoritative)
and `TorrentEngine._enforce_disk_space` (first poll where a magnet's size is known — it
cannot live in `add_torrent`, because the manager overwrites the status and message a
failure there would set). Settings: `GeneralConfig.disk_space_check` (on by default) and
`disk_space_headroom_mb` (256), exposed in Preferences → General beside the BitTorrent
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

- `Database.get_download_stats(today, daily_days=30)` (`database.py:808`) reads every bucket in
  one call and returns a `StatsSnapshot` (`database.py:67`, frozen value objects). `today` is
  **injected** (`stats_dialog.py:294`), which is what makes the date arithmetic assertable
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
  column is empty (`database.py:673`: `CASE WHEN completed_at = '' THEN ? ELSE completed_at
  END`), so the first completion wins.

Covered by `tests/test_statistics.py` (130 tests).

### 4. Categories — auto-sort downloads by file type — HAVE
RDM: "Auto-sort downloads by file type (Videos, Archives, Documents, etc.)."

Was recorded here as MISSING. That verdict was **stale**: the feature shipped, and it has the
exact shape this entry proposed to build — a `category_for_filename()` helper plus a
segregation mode, mirroring `get_entry_date_category()`. Re-checked 2026-10-01.

- `TYPE_CATEGORY_EXTENSIONS` (`download_model.py:272-311`) maps ~95 lower-case extensions into
  six buckets. `get_entry_type_category()` (`:353-385`) reads `filename`, falls back to the
  leaf of `file_path` so a torrent whose *root folder* carries the extension still classifies,
  and honours a per-entry `metadata["type_category"]` override.
- `"type"` is one of the three segregation modes (`SEGREGATED_MODES`, `:324`), so it is
  reachable from **View → Segregated View** and **Preferences → Views**, and composes with the
  header filter chips. Covered by `tests/test_views_tab.py`.

The trap that made this look absent: there are **two unrelated "type" notions** in the model.
The filter chip is `TYPE_FILTER_LABELS` (`:197-200`) and is keyed on *engine*
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

No time-of-day gate exists in `manager._process_queue()` (`manager.py:1787-1827`). The
only "schedule" in the codebase is retry backoff (`metadata["next_retry_at"]`, an absolute
epoch instant) and the AnimePahe scraper's 6-hour re-run cadence. Neither is a recurring
local-time window, so there is no precedent to copy — the design has to introduce one.

Shape: two config fields plus a guard in `_process_queue`. The manager already has a 1 Hz
`QTimer` (`_retry_timer`, `manager.py:410-412`) driving the queue, so there is a natural place
to gate it. Note the guard must **also** be applied to the two inline gates that bypass
`_process_queue()` entirely — `add_download()` (`manager.py:1708-1712`) and
`resume_download()` (`manager.py:2074-2078`) — or the window leaks.

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

We support per-download, but only as a **fixed fraction of the global limit**
(`http_engine.py:80-86`: low 25% / medium 50% / high 75% / max 100%). Two consequences:

- if the global limit is 0 (unlimited, and the default), the per-download setting is a
  **silent no-op** — the early return at `http_engine.py:81-82` fires before the allocation
  is ever read;
- there is no way to say "this one file gets 2 MB/s".

Enforcement is a chunk pacer, duplicated four times (`http_engine.py:870`, `:985`, `:1124`,
`:1269`), so it is approximate and each segment independently sleeps `chunk_len / limit` — a
segmented download overshoots its nominal cap by roughly `num_segments`× (8 by default). There
is no token bucket and no aggregation across concurrent downloads anywhere in the app.

Design is in [Named Queues & Concurrency Budgets](../architecture/queues.md) §Part 3, including why
`rate_limit_bps` lives in `metadata_json` rather than a column, why `unset` and `0` must stay
distinct, and why the shared fraction table (already duplicated between `http_engine.py:84` and
`torrent_engine.py:559`) has to be hoisted before a third copy is added.

### 9. Resume auto-detection surfaced in the UI — PARTIAL
RDM: "Auto-detects resumable downloads; disables pause/cancel for non-resumable ones."

Detection exists and is correct: `http_engine.py:686-741` probes and reads `Accept-Ranges`
(`:707`, `:727`, `:740` for the three transport paths). But `supports_range` is a **local
variable** — never persisted to the entry, never shown, and `_on_pause`
(`main_window.py:1726`) fires unconditionally.

Shape: persist the flag, disable Pause/Delete in the row/context menu when false. Small
and it removes a class of user confusion ("why did it restart from zero?").

---

## LOW value / large effort

### 10. HTTP/2 multiplexing — MISSING
RDM: "Single connection, multiple streams for same-origin segments."

Every segment is an independent HTTP/1.1 request (`http_engine.py:943` for the aiohttp path,
`:837` for the `curl_cffi` one). No `h2` or `httpx[http2]` dependency.

Assessment: **expensive and a genuine downgrade risk here.** We deliberately fall back to
`curl_cffi` with `impersonate="chrome124"` for Cloudflare-protected hosts
(`http_engine.py:701`, `:734`, `:837`), and impersonation is the whole reason curl_cffi is in
the dependency tree. Hand-rolling HTTP/2 would mean re-implementing browser TLS fingerprints,
which is a large, fragile surface. Not recommended without a concrete user demand.

### 11. Memory-mapped merge — MISSING (largely N/A)
RDM: "Uses mmap for large file segment merges."

There is no merge phase to optimise: `_segmented_download` pre-allocates the target with
`truncate(total_size)` (`http_engine.py:778`) and each segment `seek()`s into place
(`:860`, `:975`). Segments are written **directly at their final offset**, so a separate mmap
merge would be an extra full-file pass, not a faster one.

Worth noting in the pool so it is not re-proposed: the design RDM optimises for is not the
design we have.

### 12. Segment start staggering — DONE
RDM: "Staggered segment starts" (`segment_start_delay_ms: 150`).

**Implemented 2026-10-02.** `GeneralConfig.segment_start_delay_ms`, read by the engine through
the config object `set_general_config_sync` already passes, and exposed in Preferences →
General beside the segment count.

- `_segmented_download` ranks the **pending** segments in `idx` order and hands segment *n* a
  start delay of *n* × the step; `_download_one_segment` sleeps for that slice *before* its
  retry ladder. Both transports get it for free — the curl path is called from inside the
  same method.
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

Covered by `TestSegmentStagger` in `tests/test_http_engine_download.py` (12 tests).

Its own MISSING entry cited `http_engine.py:762-769` for the create_task burst; re-checked
against the tree before writing this, the launch is `http_engine.py:857-861` and the wait is
`:963`. The verdict's *behaviour* claim held; the line number had drifted, as so many in this
file did.

### 13. Segment count adapted to file size — PARTIAL
RDM: `max_segments_per_file`.

Segment *boundaries* come from the probed size (`http_engine.py:634` gates on it, `:860`
places each segment) but the *count* is fixed by config (`DEFAULT_SEGMENTS = 8`,
`http_engine.py:36`) and never adapts. There is one coarse gate —
`total_size > CHUNK_SIZE * 4` (256 KiB, `:634`, `CHUNK_SIZE` = 64 KiB at `:37`) — after which
a 300 KiB file still gets 8 segments. A `min_segment_size` (e.g. 1 MiB) would make the count
adaptive.

### 14. CLI subcommands — PARTIAL
We have `[project.scripts] my-idm = "my_idm.main:main"` with argparse
(`main.py:49-80`), but no subcommands: no `add`, no `status`. The GUI always launches
(`main.py:99`, `:212`), so it cannot be scripted against.

Shape: a `--json`/`status` flag that prints state and exits before `QApplication` is
constructed, which keeps the existing GUI path untouched.

### 15. Absolute per-download limits on disk + multiple named queues
The named-queue half shipped with #1. The *absolute per-download limits* half is still designed
only, together with #5, in
[Named Queues & Concurrency Budgets](../architecture/queues.md#not-implemented).

### 16. Firefox `browser.*` namespace with `chrome.*` fallback — PARTIAL
`background.js` uses the `chrome.*` namespace throughout. Gecko aliases it so this works,
but there is no fallback for anything Gecko has not aliased, and
`background.js:106-107` already notes the magnet `targetUrlPatterns` menu item is
best-effort on Firefox.

### 17. Cross-platform (macOS / Linux) — MISSING by decision
Windows-only by intent, not an oversight — recorded so it is not mistaken for a gap. Note
`pyproject.toml` carries **no** `sys_platform` marker and no platform-specific dependency
guards, so the "Windows-only" status comes from the ctypes/Win32 usage throughout the app, not
from packaging. (An earlier version of this entry claimed a `sys_platform == "win32"` marker
existed; it does not.)

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
- **Stale line references throughout this file**, corrected 2026-10-01 against the tree. These
  are point-in-time; `my_idm/` moves, so re-check before citing any of them. Note the second
  column drifted again the moment this file's own §1 body was found still quoting the pre-fix
  values — which is exactly why the table exists:

  | This doc said | Actually |
  | --- | --- |
  | `queue_order` at `database.py:473` | `database.py:543` (DDL), `:475` (`_DOWNLOAD_DB_COLUMNS`), `:353` (dataclass) |
  | "one global semaphore (`manager.py:1742-1751`)" | there is **no** semaphore; the gate is a DB row count in `_get_active_download_count()`, `manager.py:1777-1785` |
  | `get_entry_date_category()` at `download_model.py:288` | `download_model.py:417` |
  | no time gate in `_process_queue()` (`manager.py:1742-1782`) | `_process_queue()` is `manager.py:1787-1827` |
  | `move_queue_up`/`move_queue_down` at `manager.py:2579-2609` | `manager.py:2608-2638` |
  | `http_engine.py:79-85` fraction table | `http_engine.py:80-86` |
  | `http_engine.py:843-847` chunk pacer | the four pacers are `:868-872`, `:983-987`, `:1122-1126`, `:1267-1271`; `843-847` is an error raise |
  | per-segment requests at `http_engine.py:803`, `Accept-Ranges` at `:587` | `:943` (aiohttp) and `:837` (curl_cffi); `Accept-Ranges` at `:707`/`:727`/`:740` |
  | `Accept-Ranges` under `http_engine.py:456` | the probe is `:686-741`, not `:456` |
  | `pyproject.toml` is `sys_platform == "win32"` | there is **no** platform marker in `pyproject.toml` |

- **Two wrong test counts**: `tests/test_statistics.py` is 130 tests, not 71.
  (`tests/test_disk_space.py` at 50 and `tests/test_views_tab.py` at 109 were correct.)
- **Two unrelated "type" concepts** in `download_model.py` (`TYPE_FILTER_LABELS` = engine,
  `SECTION_TYPE_*` = file extension). Any future audit must distinguish them.
- `docs/todo/feature-pool-rdm.md` counted **bandwidth stats** in the MISSING row while §3
  documented it as DONE. Corrected in the summary table.

---

## Already rejected

| Idea | Why not |
| --- | --- |
| mmap merge (#11) | No merge phase exists; segments are written at final offsets. Would add a pass, not remove one. |
| HTTP/2 (#10) | Would forfeit the `curl_cffi` browser-impersonation path that gets Cloudflare downloads through. |
| `find_by_info_hash` case-insensitivity | Already tracked in `tests/test_infra_hardening.py`; libtorrent hashes are lowercase hex, so the case-sensitive SQL is correct as written. |
