# Feature Pool

Gap analysis against a reference implementation, for prioritisation only — not a commitment
to build any of it.

- **Reference:** `D:\Downloads\README.md` — "Raw Download Manager (RDM)", a Go + Wails
  download manager. Read as a *feature checklist*; nothing from its architecture, language
  or licence is applicable to this Python/Qt codebase.
- **Method:** every verdict below was checked against `my_idm/` source, not against our
  own README. Our README over-claims in at least one place (see "Claims to correct").
- **Date:** 2026-09-30. Suite at the time: 1724 tests, 85% coverage.

## Summary

| Verdict | Count | Features |
| --- | --- | --- |
| **HAVE** | 5 | duplicate detection, history + search, exponential backoff, Firefox extension, resume *detection* |
| **DONE** | 1 | **disk-space check** (was MISSING — implemented 2026-09-30) |
| **PARTIAL** | 7 | resume UI gating, CLI, keep-alive reuse, per-download limits, segment sizing, memory-mapped merge\*, CLI subcommands\* |
| **MISSING** | 10 | HTTP/2, mmap merge, clipboard monitor, global hotkey, bandwidth stats, scheduler, categories, URL editing, named queues, segment staggering |

---

## HIGH value — large, visible wins

### 1. Multiple named queues — MISSING
RDM: "Create custom queues with independent parallel download limits."

We have exactly one queue: a scalar `queue_order` column
(`database.py:473`) and one global semaphore (`manager.py:1742-1751`). The UI only offers
reordering *within* it.

Why it matters: a user downloading one 40-segment torrent alongside four small files
currently has no way to say "the big one gets the bandwidth". This is the single most
requested shape of a download manager after resume.

Shape: a `queues` table (`id`, `name`, `max_concurrent`), `downloads.queue_id` replacing
`queue_order`, and a queue switcher. **Migratable without data loss** — existing rows get
the default queue.

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

**Implemented** — see [Bandwidth Statistics](../../architecture/statistics.md) for the
design, and `my_idm/stats_dialog.py` for the implementation.

- `Database.get_download_stats(today, daily_days=30)` reads every bucket in one call and
  returns a `StatsSnapshot` (`DownloadStats` value objects, frozen and additive). `today` is
  **injected**, which is what makes the date arithmetic assertable without a frozen clock.
  Buckets use `substr(added_at,1,10)` rather than `strftime`, plus a `GLOB` guard on the
  date prefix: the cut-off is a *string* compare, so a hand-edited `not-a-date` sorts after
  `2026-09-24` and would otherwise be filed under *today*.
- A `📊 Statistics…` toolbar action beside **Preferences**, opening a read-only popup with
  the totals grid, a stacked per-day volume chart, and a live speed sparkline.
- Charts are `QPainter`-drawn, not matplotlib: matplotlib and numpy are importable here but
  are **absent from `requirements.txt`**, so depending on them would work on the author's
  machine and fail for anyone installing from the requirements file.
- **Prerequisite fixed:** `completed_at` was re-stamped whenever a download left `seeding`,
  so "completed today" was already wrong for every torrent whose seeding was stopped by
  hand, by the ratio limit or the duration limit. `update_status` now stamps only when the
  column is empty, so the first completion wins.

Covered by `tests/test_statistics.py` (71 tests).

### 4. Categories — auto-sort downloads by file type — MISSING
RDM: "Auto-sort downloads by file type (Videos, Archives, Documents, etc.)."

We have *date* segregation (Today / Yesterday / Last 7 Days) and *manual* filter chips for
size/status/type, but nothing derives a category from the file extension.

Shape: a `category_for_filename()` helper plus a segregation mode, mirroring
`get_entry_date_category()` (`download_model.py:288`). Cheap, and it composes with the
existing header filters.

---

## MEDIUM value

### 5. Download scheduler (off-peak hours) — MISSING
RDM: "Schedule downloads for off-peak hours" with `schedule_start` / `schedule_end`.

No time-of-day gate exists in `manager._process_queue()` (`manager.py:1742-1782`). The
only "schedule" in the codebase is retry backoff and the AnimePahe scraper's 6-hour re-run
cadence.

Shape: two config fields plus a guard in `_process_queue`. The manager already has a 1 Hz
`QTimer` driving the queue, so there is a natural place to gate it.

### 6. Global hotkey to toggle capture — MISSING
RDM: "Scroll Lock toggles capture on/off system-wide."

No `RegisterHotKey`, no `nativeEvent` override. The extension has no `commands` API block
either, so there is no in-browser equivalent.

Windows-only via `RegisterHotKey`; the extension-side `chrome.commands` block is the
cross-platform half and is the easier of the two.

### 7. Clipboard monitoring — MISSING
RDM: "Auto-detect downloadable URLs from clipboard."

We only *read* the clipboard to pre-fill a dialog (`dialogs.py:286-290`,
`youtube_dialog.py:461`). Nothing subscribes to `QClipboard.dataChanged`.

Shape: connect `dataChanged`, debounce, and feed `manager.add_download()`. Note the test
suite already snapshots and clears the clipboard per test
(`conftest.py`, `isolate_system_clipboard`), so this is testable hermetically.

### 8. Per-download bandwidth limit in absolute units — PARTIAL
RDM: "Global and per-download bandwidth limits."

We support per-download, but only as a **fixed fraction of the global limit**
(`http_engine.py:79-85`: low 25% / medium 50% / high 75% / max 100%). Two consequences:

- if the global limit is 0 (unlimited), the per-download setting is a **silent no-op**
  (`http_engine.py:80-81`);
- there is no way to say "this one file gets 2 MB/s".

Enforcement is also a chunk pacer (`http_engine.py:843-847`), so it is approximate and
shared across the entry's segments rather than a hard cap.

### 9. Resume auto-detection surfaced in the UI — PARTIAL
RDM: "Auto-detects resumable downloads; disables pause/cancel for non-resumable ones."

Detection exists and is correct: `http_engine.py:456` probes, `:587-588` reads
`Accept-Ranges`. But `supports_range` is a **local variable** — never persisted to the
entry, never shown, and `_on_pause` (`main_window.py:1442`) fires unconditionally.

Shape: persist the flag, disable Pause/Delete in the row/context menu when false. Small
and it removes a class of user confusion ("why did it restart from zero?").

---

## LOW value / large effort

### 10. HTTP/2 multiplexing — MISSING
RDM: "Single connection, multiple streams for same-origin segments."

Every segment is an independent HTTP/1.1 `session.get()` (`http_engine.py:803`). No `h2`
or `httpx[http2]` dependency.

Assessment: **expensive and a genuine downgrade risk here.** We deliberately fall back to
`curl_cffi` with `impersonate="chrome124"` for Cloudflare-protected hosts
(`http_engine.py:14-21`), and impersonation is the whole reason curl_cffi is in the
dependency tree. Hand-rolling HTTP/2 would mean re-implementing browser TLS fingerprints,
which is a large, fragile surface. Not recommended without a concrete user demand.

### 11. Memory-mapped merge — MISSING (largely N/A)
RDM: "Uses mmap for large file segment merges."

There is no merge phase to optimise: `_segmented_download` pre-allocates the target with
`truncate(total_size)` (`http_engine.py:636-638`) and each segment `seek()`s into place
(`:834-836`). Segments are written **directly at their final offset**, so a separate mmap
merge would be an extra full-file pass, not a faster one.

Worth noting in the pool so it is not re-proposed: the design RDM optimises for is not the
design we have.

### 12. Segment start staggering — MISSING
RDM: "Staggered segment starts" (`segment_start_delay_ms: 150`).

All pending segments are created in one burst (`http_engine.py:667-669`). A per-segment
`asyncio.sleep` before the first request would be a few lines. Low risk, mild benefit —
matters for servers that rate-limit connection bursts.

### 13. Segment count adapted to file size — PARTIAL
RDM: `max_segments_per_file`.

Segment *boundaries* come from the probed size (`http_engine.py:911-928`) but the *count*
is fixed by config (default 8, `:622`) and never adapts. There is one coarse gate —
`total_size > CHUNK_SIZE * 4` (256 KiB, `:494`) — after which a 300 KiB file still gets 8
segments. A `min_segment_size` (e.g. 1 MiB) would make the count adaptive.

### 14. CLI subcommands — PARTIAL
We have `[project.scripts] my-idm = "my_idm.main:main"` with argparse
(`main.py:49-80`), but no subcommands: no `add`, no `status`. The GUI always launches
(`main.py:99`, `:212`), so it cannot be scripted against.

Shape: a `--json`/`status` flag that prints state and exits before `QApplication` is
constructed, which keeps the existing GUI path untouched.

### 15. Absolute per-download limits on disk + multiple named queues
Listed with #1.

### 16. Firefox `browser.*` namespace with `chrome.*` fallback — PARTIAL
`background.js` uses the `chrome.*` namespace throughout. Gecko aliases it so this works,
but there is no fallback for anything Gecko has not aliased, and
`background.js:106-107` already notes the magnet `targetUrlPatterns` menu item is
best-effort on Firefox.

### 17. Cross-platform (macOS / Linux) — MISSING by decision
`pyproject.toml` is `sys_platform == "win32"`. Windows-only by intent, not an oversight —
recorded so it is not mistaken for a gap.

---

## Claims to correct

Found while auditing, and worth fixing in our own docs:

- Our README presents the **5-Tab Details Panel** as a headline feature; RDM's
  speed-graph screenshot has no counterpart in ours at all. Not a defect — just noting the
  asymmetry runs both ways.
- `docs/architecture/state-machines.md:52` mentions HTTP/2 in prose. There is no HTTP/2 in
  the codebase (see #10); the doc overstates it.

---

## Already rejected

| Idea | Why not |
| --- | --- |
| mmap merge (#11) | No merge phase exists; segments are written at final offsets. Would add a pass, not remove one. |
| HTTP/2 (#10) | Would forfeit the `curl_cffi` browser-impersonation path that gets Cloudflare downloads through. |
| `find_by_info_hash` case-insensitivity | Already tracked in `tests/test_infra_hardening.py`; libtorrent hashes are lowercase hex, so the case-sensitive SQL is correct as written. |
