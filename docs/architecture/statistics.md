# Bandwidth Statistics — Architecture & Implementation Design

**Status: implemented.** `my_idm/stats_dialog.py` and
`Database.get_download_stats()` in `my_idm/database.py`. Covered by
`tests/test_statistics.py`. This document is the design; where the implementation had to
diverge from it, the divergence is called out below.

The reference implementation's claim is one line — *"Live status bar with
daily/monthly/yearly/lifetime download totals"* — and the reason it is worth doing here is
that **the data is already in the database**. This is a `GROUP BY` and a small dialog, not a
schema migration.

---

## 1. Scope

In scope:

- A read-only aggregation over the existing `downloads` table.
- Totals for **today / this week / this month / this year / lifetime**, split into
  *downloaded* and *uploaded*, plus a completed-file count.
- **Graphs** — see §4. Three tiers: a daily-volume chart that the existing table already
  supports, and a live speed chart sampled while the popup is open.
- A **Statistics** button in the main toolbar beside **Preferences**, opening a popup.

Out of scope (deliberately, and why):

| Excluded | Reason |
| --- | --- |
| Persisted history rows | Totals are derived, so a rollup table would need invalidation on every edit. Premature until the row count hurts. |
| Per-host / per-extension breakdowns | Cheap to add later on top of the same query shape; excluded to keep the first cut small. |
| Cross-session speed history | Needs a new events table; §4.4 explains what it would take. |
| Any write path | The whole feature is read-only by design — a statistics view that can corrupt download state is a bad trade. |

---

## 2. The data that already exists

`downloads` (`database.py:452`) carries everything needed:

| Column | Type | Notes |
| --- | --- | --- |
| `added_at` | `TEXT NOT NULL DEFAULT ''` | `_now_iso()` → `2026-09-30T02:23:20.041653+00:00` (UTC, ISO-8601) |
| `completed_at` | `TEXT NOT NULL DEFAULT ''` | Empty until the download completes |
| `total_size` | `INTEGER` | From the probe / torrent metadata; 0 when unknown |
| `downloaded_size` | `INTEGER` | **Seeded, not written** — see §2.2 |
| `uploaded_size` | `INTEGER` | Cumulative seeded/uploaded bytes |
| `status` | `TEXT` | `completed`, `seeding`, `error`, … |
| `download_type` | `TEXT` | `http` / `torrent` |

### 2.1 Which timestamp to bucket by

`added_at` for everything. It is written once, at creation, and never re-stamped, so it is
the only timestamp with a stable meaning. `completed_at` is *not* currently safe — see §2.2.

### 2.2 Prerequisite bug: `completed_at` is re-stamped by seeding→completed

`TorrentEngine.pause()` maps a seeder's status to `completed`
(`torrent_engine.py:812`, `target_status = "completed" if is_seeding else "paused"`), and
`Database.update_status`'s `completed` branch writes `completed_at = _now_iso()`.

Measured on a torrent whose payload arrived 2026-09-28 and whose seeding session was ended
by hand two days later:

```
before pause: completed_at = 2026-09-28T02:00:00+00:00
after  pause: status = completed  completed_at = 2026-09-30T11:50:16+00:00
```

Every download whose seeding was stopped by hand — by the ratio limit, the duration limit,
or the user — therefore reports a completion date of *when the seeding ended*, not when the
bytes arrived. A "completed today" bucket built on `completed_at` would be wrong for all of
them, and a "this month's completed files" count would drift.

**This must be fixed before any statistics UI ships.** The cleanest fix is to stop reusing
`completed` for "a seeding session ended" — but the status vocabulary is consumed by the
UI, the manager queue and `state-machines.md`, so that is a real change with its own
migration. A narrower fix: make `update_status` only stamp `completed_at` when it is
currently empty, so the first completion wins and later transitions cannot move it.

Note this bug is *not* new — it is the long-standing consequence of `completed` doing double
duty for "finished" and "seeding session over". The statistics feature is simply the first
thing that makes it visible. Flagged, not fixed here.

### 2.3 `downloaded_size` is a seed, not a counter

`HTTPEngine._run_download` sets `downloaded_size` to the full `total_size` on completion
(`if total_size > 0 and downloaded_size < total_size: downloaded_size = total_size`). For a
completed download that is correct. For an *in-flight* one it is a lower bound that jumps.
Summing it is therefore "bytes known to be on disk", which is the right number for a
progress total and the wrong number for a throughput graph - see §2.3.

---

## 3. Aggregation layer

New read-only methods on `Database` (`database.py`). They follow the existing pattern:
`_conn.execute(...)` under `_LOCK`, returning plain data.

> Use `substr(ts, 1, 10)` / `substr(ts, 1, 7)`, **not** `strftime()`. `strftime` would need
> SQLite ≥ 3.38 to accept the `T` separator and `+00:00` suffix that `_now_iso()` emits, and
> a silent parse failure returns `NULL` — i.e. a statistics view that quietly shows zero.
> `substr` is exact regardless of version, and these are string slices on a fixed-width
> ISO prefix.
>
> **Implemented with one addition the design did not anticipate.** The cut-off predicate is
> a *string* comparison (`substr(added_at,1,10) >= ?`), so a row whose timestamp was
> hand-edited to something unparseable sorts into a bucket by accident: `'not-a-date' >=
> '2026-09-24'` is true, because `'n'` sorts after `'2'`. The shipped query adds a `GLOB`
> guard requiring a `YYYY-MM-DD` prefix, so such a row appears only in the lifetime total,
> where an undatable download honestly belongs. Without it, one corrupt row reads as a
> large download today.

```sql
-- lifetime / all-time
SELECT COUNT(*), COALESCE(SUM(total_size), 0), COALESCE(SUM(uploaded_size), 0)
FROM downloads;

-- bucketed, one row per day (or month, with substr(ts,1,7))
SELECT substr(added_at, 1, 10)          AS bucket,
       COUNT(*),
       COALESCE(SUM(total_size), 0),
       COALESCE(SUM(uploaded_size), 0)
FROM downloads
WHERE added_at != '' AND substr(added_at, 1, 10) >= ?   -- 'YYYY-MM-DD' cutoff
GROUP BY bucket
ORDER BY bucket;
```

Proposed shape — one call, all buckets, so the popup needs a single round trip:

```python
@dataclass(frozen=True)
class DownloadStats:
    count: int
    downloaded: int
    uploaded: int
    completed: int          # rows whose status is completed or seeding

@dataclass(frozen=True)
class StatsSnapshot:
    today: DownloadStats
    week: DownloadStats
    month: DownloadStats
    year: DownloadStats
    lifetime: DownloadStats
    daily: list[tuple[str, DownloadStats]]   # ascending, for a sparkline later
```

`Database.get_download_stats(today: date) -> StatsSnapshot`, with `today` **injected** rather
than read from the clock. Every other aggregation-shaped API in the codebase takes its clock
input as a parameter, and it is what makes the tests below possible.

Cost: one `GROUP BY` over the `downloads` table. At the ~1k rows a heavy user accumulates
over a year this is sub-millisecond, so it can run inline on the Qt thread. §8 covers when
that stops being true.

---

## 4. Graphs

RDM ships a live status-bar chart, so this is not optional for parity. What it ships is
one graph; the honest position here is that **two of the three useful graphs can be built
from data we already have**, and the third needs a schema change that is out of scope for
the first cut.

| Graph | Data source | Verdict |
| --- | --- | --- |
| Daily volume (down / up) | `GROUP BY substr(added_at,1,10)` — already in §3 | **Build now** |
| Live speed while open | in-memory samples from the manager's speed label | **Build now** |
| Speed / volume across past sessions | not recorded anywhere | Needs a new table — §4.4 |

### 4.1 Drawing it: no new dependency

`matplotlib` and `numpy` are importable in this environment but are **absent from
`requirements.txt`** — they arrive transitively, which is not a contract. Relying on them
would work on the author's machine and fail for anyone who installs from the requirements
file. Everything else in this codebase draws its own widgets with `QPainter` (see the
progress and speed delegates), so the charts follow suit:

- `StatsChartWidget(QWidget)` — `paintEvent` only, no layout, no axes objects.
- ~150 lines for a bar chart, ~90 for a sparkline.
- Colours from `styles.Colors` (`ACCENT`, `BG_SECONDARY`, `TEXT_MUTED`, …) so the charts
  match the app in both themes rather than shipping matplotlib's own style.

If the project later decides to standardise on matplotlib, that is a `requirements.txt`
change and a deliberate one, not something to discover at runtime.

### 4.2 Tier 1 — daily volume chart

Data comes from a `GROUP BY` on `substr(added_at, 1, 10)` (day) or `substr(added_at, 1, 7)` (month). Two independent pickers drive it — **Range** (last 7 days / 30 days / 12 months / all time) and **Group by** (per day / per month) — because they are independent questions: “the last year” and “per month” have to be combinable, or the chart is useless at both extremes. `Database.get_download_stats(today, since=..., bucket=...)` takes both explicitly, and the fixed summary rows are deliberately unaffected, so a chart range cannot silently redefine “this week”.

```
Downloads per day — last 30 days
 600 MB ┤                    ▄▄
 400 MB ┤            ▄▄▄▄▄▄▄█
 200 MB ┤  ▄▄▄▄▄▄▄▄█       █ █ █
       0 ┼──────────────────────────
         01  04  07  10  13  16  19  22  25  28
```

Stacked bars: downloaded and uploaded share a day column, so a seeding session's upload
shows against the day it happened. Bars for days with no activity are drawn at zero width
rather than skipped — a compressed time axis lies about spacing.

Detail worth pinning: `added_at` is when the download was *added*, so a large file added
on the 1st and finished on the 5th appears entirely in the 1st's column. The chart is a
record of *when things were queued*, not when bytes moved. Labelling it "Downloads per day"
is accurate; labelling it "Traffic per day" would not be.

### 4.3 Tier 2 — live speed chart

While the popup is open, a `QTimer` at 1 Hz samples the manager's aggregate rate and
appends to an in-memory `deque(maxlen=120)` (two minutes of history). Drawn as a filled
sparkline with a peak label.

**In-memory only, on purpose.** Nothing is written to the database, so there is no
invalidation problem and no schema change; closing the popup discards the samples. The
alternative — persisting samples — is what §4.4 is about.

The sample must be read from the same value the status bar shows, so the chart and the
status bar can never disagree. If that aggregate does not exist yet, summing
`entry.speed` over active rows is acceptable but should be replaced by one shared helper
rather than being computed twice.

### 4.4 Tier 3 — cross-session history (not in the first cut)

For speed over days, the schema needs a time series:

```sql
CREATE TABLE download_events (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,          -- _now_iso()
    download_id TEXT NOT NULL,
    kind        TEXT NOT NULL,          -- 'completed' | 'seeding_stopped' | 'sample'
    bytes       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_events_ts ON download_events(ts);
```

That is a schema migration, an insert on every completion and every seeding stop, and a
retention policy (samples older than N days deleted on startup) — a real project, not a
patch. It is listed here so it is a known next step rather than an oversight.

---

## 5. UI

### 5.1 Toolbar button

Placed immediately before Preferences, at the end of `_setup_toolbar()`
(`main_window.py:690`), which currently ends:

```python
toolbar.addWidget(self._tor_toolbar_container)
toolbar.addSeparator()
...
toolbar.addAction(self._act_preferences)      # main_window.py:773
```

The action belongs in `_setup_actions()` next to `_act_preferences`
(`main_window.py:632`) so the menubar can pick it up the same way:

```python
self._act_stats = QAction(_create_emoji_icon("📊"), "Statistics…", self)
self._act_stats.setToolTip("Download and upload totals for today, this week, month and year")
self._act_stats.triggered.connect(self._on_show_statistics)
```

`📊` matches the existing emoji-icon convention (`⚙` for Preferences, `🧅` for Tor).
No shortcut: this is a read-only view, not an action, and <kbd>Ctrl</kbd>, is already taken.

### 5.2 The popup

A `QDialog` in the style of the existing `RenameDialog` / `FirefoxInstallGuideDialog`
(`dialogs.py`, `settings_dialog.py`): frameless-ish, dark theme from `styles.Colors`,
`Qt.WA_DeleteOnClose`, constructed parentless and given the main window as `parent` so it
centres on it.

Layout — a single `QGridLayout` of label/value pairs, one row per bucket:

```
┌─────────────────────────────────────────┐
│  📊  Statistics                          │
├──────────────────┬──────────────────────┤
│  Today           │  1.2 GB down · 340 MB up │
│  This week       │  8.7 GB down · 2.1 GB up │
│  This month      │  31 GB down · 9 GB up   │
│  This year       │  402 GB down · 118 GB up │
│  All time        │  1.4 TB down · 390 GB up │
├──────────────────┴──────────────────────┤
│  1,204 files completed                    │
│                          [ Refresh ] [ ✕ ] │
└─────────────────────────────────────────┘
```

`sizes shown as "1.2 GB / 340 MB" with a tooltip carrying the exact byte count, so the
user is never told "1.2 GB" when it means 1,204,182,912. `humanize.naturalsize` is already a
dependency and is the right formatter; add `humanize.naturalsize(..., binary=True)` to match
what the details panel shows.

`Refresh` re-runs the query. The popup does **not** live-refresh: an open dialog that
re-queries on a timer would need to be closed on shutdown to avoid touching a deleted
`Database`, and the numbers move slowly enough not to need it.

### 5.3 Status bar (optional, cheap)

The status bar already has a speed label (`_status_label`, `main_window.py:947`). Adding a
permanent `Today: 1.2 GB` chip is a two-line change to `_update_count_label` and delivers
the "live status bar" half of the reference feature without any of §5.2. Worth doing
alongside, not instead — the popup is for the breakdown, the chip is for ambient awareness.

---

## 6. Threading

`Database` is guarded by a module-level `threading.Lock` and every query already goes
through it, so calling `get_download_stats` from the Qt thread while the manager writes is
safe.

The Qt thread rule the codebase follows — *no engine or database work inside a slot that
runs on a worker thread* — is respected trivially here: the query is a few hundred
microseconds. If the table ever grows past ~50k rows, move the call to a
`QThreadPool` job with a signal back; the API shape above does not change, because
`today` is already injected.

---

## 7. Tests

All hermetic, in the style of `tests/test_database.py`. `tmp_path`-equivalent temp DB,
`today` injected, no clock reads, no sleeping.

| Test | Asserts |
| --- | --- |
| `test_empty_database_reports_all_zeroes` | A fresh install shows zeros, not `None` or a crash |
| `test_lifetime_totals_sum_every_row` | One row, exact byte counts |
| `test_today_excludes_yesterday` | The `substr` cutoff is inclusive of today only |
| `test_a_row_added_at_midnight_utc_counts_for_that_day` | The boundary case, pinned exactly |
| `test_a_row_with_no_added_at_is_excluded` | `''` must not bucket into the epoch |
| `test_month_and_year_buckets_span_the_right_range` | Off-by-one on the cutoffs |
| `test_completed_counts_seeding_rows` | Seeding is a completed payload that is still uploading |
| `test_null_sums_become_zero` | A row with `NULL` size cannot produce `None` in the UI |
| `test_unicode_and_very_long_filenames_do_not_affect_totals` | Only byte columns are summed |
| `test_bucketing_survives_a_malformed_timestamp` | A hand-edited `added_at` is skipped, not crash-prone |
| `test_stats_popup_renders_every_bucket` | Widget test: five rows, correct text |
| `test_stats_action_is_in_the_toolbar_before_preferences` | The placement is a real requirement |

The last one matters more than it looks: "beside Preferences" is the requirement, and
without a test the next toolbar edit silently moves it.

---

## 8. Performance trigger for revisiting

Re-measure when any of these becomes true:

- `downloads` rows > ~50k (a heavy user over several years), or
- the popup is opened more than a few times an hour, or
- `get_download_stats` exceeds ~50 ms in a profile.

At that point: add a covering index on `added_at`, or a `stats_daily` rollup table
refreshed lazily (one row per day per bucket, invalidated by the newest `added_at`).

---

## 9. Known limits of this design

1. **`completed_at` is unreliable** until §2.2 is fixed. The design routes around it by
   bucketing on `added_at`; it does not fix it.
2. **No time series.** `added_at` gives when a download was *added*, not a running total per
   day. A speed graph needs a different table entirely — a `download_events` row written on
   every completion and on every seeding-stop — and that is a schema change, not a query.
3. **Bytes are counted at completion time, in hindsight.** A long download that started
   before the window and finished inside it counts entirely in the window.
4. **`downloaded_size` is a floor while in flight** (§2.3), so a "downloaded so far this
   month" figure moves as downloads progress. Acceptable for a retrospective total; wrong
   for anything live.
5. **Deleted downloads are gone.** `Database.delete_download` removes the row, so the
   history shrinks when the user tidies up. If permanent history is wanted, that is a
   `deleted_at` column and a policy decision, not a query change.

---

## 10. Implementation order

1. Fix `completed_at` re-stamping (§2.2) — small, and it unblocks everything else.
2. `DownloadStats` / `StatsSnapshot` dataclasses.
3. `Database.get_download_stats(today)` + the data-layer tests from §7.
4. `StatsChartWidget` — the bar chart first (§4.2), since its data already exists.
5. `StatisticsPopup` with the totals grid and the chart, plus the widget tests.
6. `_act_stats` in `_setup_actions`, `toolbar.addAction` before Preferences, placement test.
7. The live speed sparkline (§4.3), reading the same aggregate as the status bar.
8. Optional: the status-bar chip (§5.3).
9. Update `docs/architecture/main.md` and the README documentation index.
