# Bandwidth Statistics — Architecture & Implementation Design

**Status: implemented.** `my_idm/stats_dialog.py` (`StatisticsView`) embedded as the **📊 Stats**
tab of the bottom details panel (`my_idm/details_panel.py`), and
`Database.get_download_stats()` / `Database.record_bandwidth()` in `my_idm/database.py`. Covered by
`tests/test_statistics.py`. This document is the design; where the implementation had to
diverge from it, the divergence is called out below.

The reference implementation's claim is one line — *"Live status bar with
daily/monthly/yearly/lifetime download totals"* — and the reason it is worth doing here is
that **the data is already in the database**. This is a `GROUP BY` and a small panel, not a
schema migration.

> **Divergence 1 — it is a details-panel tab, not a popup.** The original design shipped
> `StatisticsPopup(QDialog)`; since 2026-10-09 the live widget is `StatisticsView`, docked as the
> **📊 Stats** side tab of the bottom panel beside Queues and Console, with three subtabs
> (**Volumes / Current Speed / Totals**). The class survives as a thin legacy wrapper and is no
> longer instantiated anywhere. See §5.
>
> **Divergence 2 — a real history table replaced derivation from `downloads`.** Since 2026-10-10
> the volumes are read from the `bandwidth_history` table (§2.4), written on every progress
> delta, instead of being derived from `added_at`. The `downloads` aggregation is still there as
> the fallback for a fresh install that has moved no bytes yet, and still supplies the *counts*.
> This retires design limit 9.2 ("no time series").

---

## 1. Scope

In scope:

- A read-only aggregation over the existing `downloads` table, and over the
  **`bandwidth_history`** 5-minute transfer log (§2.4) once it exists.
- Totals for **today / this week / this month / this year / lifetime**, split into
  *downloaded* and *uploaded*, plus a completed-file count.
- **Graphs** — see §4. Three tiers: a daily-volume chart that the existing table already
  supports, and a live speed chart sampled while the panel tab is open.
- A **Statistics** entry in **Tools**, grouped with **Preferences**, opening the bottom panel on
  the Stats tab.

Out of scope (deliberately, and why):

| Excluded | Reason |
| --- | --- |
| Per-host / per-extension breakdowns | Cheap to add later on top of the same query shape; excluded to keep the first cut small. |
| Cross-session speed *samples* | The 5-minute `bandwidth_history` table (§2.4) now records volumes, not instantaneous rates — a true speed series is still out of scope. |
| Any write path from the UI | The whole feature is read-only by design — a statistics view that can corrupt download state is a bad trade. The one writer is the progress callback that already writes bandwidth usage (§2.4). |

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

### 2.4 The 5-minute transfer log (`bandwidth_history`)

Added 2026-10-10 (`ed09b99`, `feb6114`, `665dffe`). A second table, written by the writers rather
than derived from the rows:

```sql
CREATE TABLE IF NOT EXISTS bandwidth_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    queue_id        TEXT NOT NULL DEFAULT '',
    period_start    TEXT NOT NULL,  -- 'YYYY-MM-DD HH:MM' in local time (5-min bucket)
    downloaded_bytes INTEGER NOT NULL DEFAULT 0,
    uploaded_bytes   INTEGER NOT NULL DEFAULT 0,
    UNIQUE(queue_id, period_start)
);
CREATE INDEX IF NOT EXISTS idx_bandwidth_history_period ON bandwidth_history(period_start);
```

**The write path already existed.** `DownloadManager.record_bandwidth_usage()`
(`manager.py:3463`) is called from both progress callbacks — `_on_http_progress`
(`manager.py:4265`) with the byte **delta** per progress event, and `_on_torrent_progress`
(`manager.py:4308`) with the download and upload deltas — where the delta is the difference
against `_last_progress_bytes`, not the cumulative total. It feeds `Database.record_bandwidth`,
which has always written the daily/weekly/monthly quota rollups; the same call now also writes
the 5-minute bucket (`database.py`, in `record_bandwidth`). So the statistics table costs **no new
write path and no new call sites** — it is a second insert inside the one place that already
counts bytes.

Three properties make it usable as a time series:

- **The bucket key is local time**, truncated to 5 minutes (`Database._get_5min_bucket`), so a
  host in any timezone still buckets on the user's own clock — the same rule §3 established for
  the day buckets, moved into the write path.
- **`ON CONFLICT … DO UPDATE … downloaded_bytes = downloaded_bytes + excluded.downloaded_bytes`**
  makes the write idempotent per interval: a progress event is added, never overwritten, so a
  repeated callback cannot erase an interval.
- **It is backfilled once.** `_backfill_bandwidth_history_if_empty()` seeds the table from
  `downloads` on `open()` — downloaded bytes (falling back to `total_size` for complete rows),
  bucketed at `completed_at or added_at` — but **only when the table is empty**, so a fresh
  history never competes with a backfilled one.

`get_download_stats` prefers it when it has any rows at all, for both the fixed summary buckets
and the chart series, and falls back to the `downloads` aggregation on a fresh install. The
fallback matters: the first launch after upgrading has an empty history and must not show zeros
for a year of downloading.

| Field | Series source | Count source |
| :--- | :--- | :--- |
| `downloaded` / `uploaded` | `bandwidth_history` (transferred bytes) | — |
| `count` / `completed` | `downloads` (`added_at` buckets) | `downloads` |

For the chart series the two are **unioned by bucket** (`all_buckets = sorted(set(bw) | set(dl))`):
an interval that moved bytes but added no download still draws, and one that added a download
without moving bytes still counts. Merging them into one row per bucket is what lets a range be
wide *and* fine-grained at once — §4.2.

`bandwidth_history` is documented in [`database.md`](database.md); the write path is part of the
bandwidth-quota feature in [`queues.md`](queues.md).

---

## 3. Aggregation layer

New read-only methods on `Database` (`database.py`). They follow the existing pattern:
`_conn.execute(...)` under `_LOCK`, returning plain data.

> **Buckets are local calendar days, rows are stamped in UTC.** `added_at` is written by
> `_now_iso()` as UTC, while the cut-offs are built from a **local** `today`, because the
> buckets are labelled in the user's own calendar. A bare `substr(added_at,1,10)` therefore
> compares a *UTC* date against a *local* one, and it is wrong for part of every day: at
> 02:00 in UTC+05:30 a download added two minutes ago is stamped `2026-09-29T20:30` and
> lands in **Yesterday**. The shipped query re-bases each row first —
> `substr(datetime(added_at, 'localtime'), 1, 10)` — which is exactly the conversion
> `download_model.get_entry_date_category()` performs with `astimezone()`, so the table's
> "Today" section and the popup's "Today" figure finally agree. `localtime` goes through
> the C library, so it is DST-correct per row and needs no offset plumbed in from Python.
>
> This is the one place the doc's "no date functions" rule below was **not** followed, and
> deliberately so: a `substr` slice cannot express a timezone conversion at all. The
> version concern that motivated the rule does not apply, because `localtime` operates on
> an already-accepted ISO value and the acceptance is verified by the `IS NOT NULL` guard
> below rather than assumed.
>
> **Two additions the design did not anticipate.**
>
> 1. *Corrupt timestamps must not sort into a bucket.* The cut-off predicate is a string
>    comparison, and a hand-edited `'not-a-date' >= '2026-09-24'` is **true**, because `'n'`
>    sorts after `'2'`. A `GLOB` guard requiring a `YYYY-MM-DD` prefix keeps such a row out
>    of every dated bucket; it still counts towards the lifetime total, where an undatable
>    download honestly belongs.
> 2. *`GLOB` alone is not enough.* `2026-13-45T00:00:00+00:00` **matches** the shape but
>    makes `datetime()` return `NULL`, and `GROUP BY` on a NULL expression still forms a
>    group — which surfaced on the chart as a bucket literally labelled `"None"`. The guard
>    therefore also requires `datetime(added_at, 'localtime') IS NOT NULL`. This only bites
>    the **All time** range: the bounded ranges carry a `day >= ?` comparison and
>    `NULL >= x` is `NULL`, i.e. false, so those rows already dropped out on their own.

```sql
-- lifetime / all-time — the fallback aggregation, used when bandwidth_history is empty
SELECT COUNT(*), COALESCE(SUM(total_size), 0), COALESCE(SUM(uploaded_size), 0)
FROM downloads;

-- bucketed, one row per local day (or month, with width 7)
SELECT substr(datetime(added_at, 'localtime'), 1, 10)   AS bucket,
       COUNT(*),
       COALESCE(SUM(total_size), 0),
       COALESCE(SUM(uploaded_size), 0)
FROM downloads
WHERE added_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'
  AND datetime(added_at, 'localtime') IS NOT NULL
  AND substr(datetime(added_at, 'localtime'), 1, 10) >= ?   -- 'YYYY-MM-DD' cutoff
GROUP BY bucket
ORDER BY bucket;
```

The four constants in `Database` — `_STATS_LOCAL_5MIN` (width 16),
`_STATS_LOCAL_HOUR` (width 13), `_STATS_LOCAL_DAY` (width 10) and
`_STATS_LOCAL_MONTH` (width 7) — are written out verbatim in both the SELECT list and the
WHERE clause. SQLite will not let a `WHERE` reference a `SELECT` alias, so the expressions
are duplicated by necessity; keep them identical or the filter and the group key diverge.

Because the conversion reads the host timezone, injecting `today` alone cannot make the
SQL deterministic across machines. Tests must compute expectations with
`datetime.fromisoformat(stamp).astimezone()` rather than hard-coding a UTC date, and one
test asserts directly that `_STATS_LOCAL_DAY` contains `localtime` — a whitebox pin, so the
bug still fails the suite on a machine that happens to sit on UTC+00:00 where it would
otherwise be invisible.

Shipped shape — one call, all buckets, so the panel needs a single round trip:

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

`Database.get_download_stats(today: date, since=..., bucket=..., fill_gaps=True) -> StatsSnapshot`,
with `today` **injected** rather than read from the clock. Every other aggregation-shaped API in
the codebase takes its clock input as a parameter, and it is what makes the tests below possible.

Cost: one `GROUP BY` over `bandwidth_history` plus one over `downloads`. At the ~1k rows a heavy
user accumulates over a year this is sub-millisecond, so it can run inline on the Qt thread. §8
covers when that stops being true.

---

## 4. Graphs

RDM ships a live status-bar chart, so this is not optional for parity. What it ships is
one graph; the honest position here is that **two of the three useful graphs can be built
from data we already have**, and the third needs per-sample records that still do not exist.

| Graph | Data source | Verdict |
| --- | --- | --- |
| Volume over any range, any granularity | `bandwidth_history` (§2.4) — 5-minute transferred bytes | **Shipped** |
| Live speed while the tab is open | in-memory samples from the manager's speed label | **Shipped** |
| Speed across past *sessions* | not recorded anywhere — `bandwidth_history` stores volume per interval, not rate samples | Not built — §4.4 |

### 4.1 Drawing it: no new dependency

`matplotlib` and `numpy` are importable in this environment but are **absent from
`requirements.txt`** — they arrive transitively, which is not a contract. Relying on them
would work on the author's machine and fail for anyone who installs from the requirements
file. Everything else in this codebase draws its own widgets with `QPainter` (see the
progress and speed delegates), so the charts follow suit:

- `StatsChartWidget(QWidget)` — `paintEvent` only, no layout, no axes objects. The volume
  chart draws either stacked bars or two lines from one shared axis (§4.2).
- ~150 lines for a bar chart, ~90 for a sparkline.
- Colours from `styles.Colors` (`ACCENT`, `BG_SECONDARY`, `TEXT_MUTED`, …) so the charts
  match the app in both themes rather than shipping matplotlib's own style.

If the project later decides to standardise on matplotlib, that is a `requirements.txt`
change and a deliberate one, not something to discover at runtime.

### 4.2 Tier 1 — volume chart

Data comes from a `GROUP BY` on the **local** 5-minute interval (`_STATS_LOCAL_5MIN`), hour
(`_STATS_LOCAL_HOUR`), day (`_STATS_LOCAL_DAY`), or month (`_STATS_LOCAL_MONTH`) — see §3
for why the stored UTC stamp has to be converted first. Two independent pickers drive it —
**Range** (14 entries: Today, this/last week, last 7 days, this/last month, last 30 days,
this/last quarter, last 90 days, this/last year, last 12 months, all time) and **Group by**
(per 5 minutes / per hour / per day / per month) — because they are independent questions:
“the last 7 days” and “per hour” have to be combinable, or the chart is useless at both extremes.
`Database.get_download_stats(today, since=..., bucket=..., fill_gaps=True)` takes both explicitly,
and the fixed summary rows are deliberately unaffected, so a chart range cannot silently redefine
“this week”.

```
Downloads per day — last 30 days
 600 MB ┤                    ▄▄
 400 MB ┤            ▄▄▄▄▄▄▄█
 200 MB ┤  ▄▄▄▄▄▄▄▄█       █ █ █
       0 ┼──────────────────────────
         01  04  07  10  13  16  19  22  25  28
```

Stacked bars: downloaded and uploaded share a bucket column, so a seeding session's upload
shows against the period it happened. Empty intervals with no activity are populated with
zero values (`fill_series_gaps`) rather than skipped: dropping them compresses the time axis
and makes sparse activity look identical across different granularities. Instead, intervals
occupy their true proportional slots along the timeline.

Detail worth pinning: **the volumes are transferred bytes, the counts are `added_at`.** A large
file added on the 1st and finished on the 5th had its progress deltas counted on the days the
bytes actually moved, so the *volume* belongs to those days — while the *count* of downloads
still belongs to the 1st, because a count is a property of the row. Labelling the chart
"Traffic per day" is accurate for the volumes; the Totals tab's completed-files figure is the
one that answers "what was queued".

#### The bars ⇄ line toggle

The Volumes tab also carries a **bars / line** toggle (`StatisticsView._chart_kind_btn`,
`StatsChartWidget.set_chart_kind()`), persisted in the same `statistics_dialog_size` UI
state as the cumulative choice. Bars are the default; the button is checkable with checked
meaning *line*, and its label names the view currently shown, mirroring **Cumulative**.

The reason it exists: a long range flattens bars into a comb — ninety 4 px wide bars cannot
be read, while the shape of the trend still can. Two invariants keep the two views
comparable, so switching is a viewing change and not a data change:

- **shared axis.** Gridlines, value labels and peak are computed once and used by both
  (`_paint_grid`), so the y scale never rescales on toggle and the tick labels stay honest;
- **two independent series, not a stacked area.** Drawing uploaded on top of downloaded
  would hide whichever is smaller — always uploads here. The legend already labels both
  halves of the pair.

`paintEvent` is therefore split into `_paint_grid` / `_paint_labels` plus `_paint_bars` /
`_paint_lines`, with the empty-data and single-bucket cases kept: one bucket has no line to
draw, so it renders a dot rather than nothing.

#### Cumulative

The **📈 Cumulative** toggle (`StatisticsView._cumulative_btn`) re-draws the same series as
running totals — the chart's own `set_cumulative` accumulates down the series — and is
persisted beside the chart kind in the same UI state, so the chosen view survives a restart.
It is a viewing change too: the underlying series is untouched, which is what lets the two
views be compared without re-querying.

### 4.3 Tier 2 — live speed chart

While the Stats tab is open, a `QTimer` at 1 Hz samples the manager's aggregate rate and
appends to an in-memory `deque(maxlen=120)` (two minutes of history). Drawn as a filled
sparkline with a peak label.

**In-memory only, on purpose.** Nothing is written to the database, so there is no
invalidation problem and no schema change; switching tabs discards the samples. The
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
patch. It is listed here so it is a known next step rather than an oversight. Note that
`bandwidth_history` (§2.4) covers the *volume* half of this table already; what remains
missing is the per-sample rate, which is the part a sparkline across sessions would need.

---

## 5. UI

### 5.1 Tools-menu entry

Grouped with **Preferences**, at the head of `_setup_menubar()`'s Tools section, separated from
**Export Selected as CSV…** below it:

```python
self._act_tools_stats = QAction(_create_emoji_icon("📊"), "Statistics…", self)
self._act_tools_stats.setToolTip(
    "Statistics: download and upload totals for today, this week, this month and this year"
)
self._act_tools_stats.triggered.connect(self._on_show_statistics)
```

`📊` matches the existing emoji-icon convention (`⚙` for Preferences, `🧅` for Tor).
No shortcut: this is a read-only view, not an action, and <kbd>Ctrl</kbd>, is already taken.

It was a **toolbar** button until 2026-10-02, sitting immediately before Preferences. The
toolbar is now a row of transport and file commands, and two things about the button argued
against it there: a strip button reads as an action *on the selection* when the view reports
on the whole history, and the abbreviated label it had to wear (`Stats…`, see
[`.agents/workflows/qt-ui.md`](file:///d:/Projects/my-idm/.agents/workflows/qt-ui.md)) read as a
different feature from the Tools entry of the same name. Preferences keeps its toolbar button,
because it opens a window about the selected download's *settings*; the abbreviation is the
only concession that strip makes.

### 5.2 It is a details-panel tab, not a popup

Since 2026-10-09 (`722a176`) the widget lives in the **bottom details panel** as the **📊 Stats**
side tab, beside Queues and Console. `MainWindow._on_show_statistics()` un-hides the panel if
needed, checks `_act_toggle_details`, and calls `DetailsPanel.show_stats()`; the side tab itself
switches mode directly.

Refresh discipline is split, deliberately:

- **Volumes and Totals** re-query when the tab is entered — `show_stats()`, `_on_mode_tab_changed`
  and the panel's `showEvent` all call `StatisticsView.refresh()`. They do **not** re-query on a
  timer: the volumes move slowly, and a 1 Hz query on a hidden page is pure cost.
- **Current Speed** samples at 1 Hz (`StatisticsView.start_speed_timer`, fed by
  `DetailsPanel._get_speed_sample`), and is started and stopped by the panel on mode change and
  on `hideEvent` / `showEvent`, so the timer runs only while the page is actually visible.

Both rules come from the old popup's failure modes: a dialog had to be closed on shutdown or its
timer fired against a torn-down model, and a modeless dialog could be opened twice. A panel page
has neither problem by construction — which is why the class-based fix was deleted rather than
ported.

`StatisticsView` (`stats_dialog.py:393`) hosts three subtabs:

| Subtab | Content |
| :--- | :--- |
| **Volumes** | The chart plus its **Range** (14 entries), **Group by**, **Cumulative** and **bars ⇄ line** controls |
| **Current Speed** | The live 1 Hz sparkline (§4.3) |
| **Totals** | The label/value grid for the fixed summary buckets |

`StatisticsPopup(QDialog)` still exists as a thin legacy wrapper that embeds a
`StatisticsView`, and is **no longer instantiated anywhere** — `grep` for it in `my_idm/` finds
only the class definition. Its `showEvent` / `closeEvent` / `reject` still start and stop the
speed timer, which is why the wrapper is harmless rather than dead weight: it is the shape the
timer lifecycle was written against. Several rules in this section survive from that form and are why the
panel form works:

- **The reference must be held.** The old `_stats_dialog` attribute existed because a popup whose
  only reference is a local variable is collectable — and its 1 Hz `QTimer` with it. A panel tab
  has no such problem, but the 1 Hz speed timer must still stop when the tab is hidden, or it
  keeps sampling against a torn-down model — which is exactly what
  `_start_stats_timer` / `_stop_stats_timer` on mode change and `hideEvent` guarantee.
- **A second click must raise, not duplicate.** The old `WA_DeleteOnClose` dance existed because
  nothing deleted the dialog on close. A tab is a page in an existing widget, so the class of bug
  is gone by construction.
- **Failure is logged, never silent.** `refresh()` catches everything around
  `get_download_stats()` and renders an em-dash in each cell, because a statistics view must not
  be able to take the app down. It **logs at `warning` with `exc_info=True`** — swallowing it
  silently leaves the user with an empty panel and nothing for a bug report to work from, and the
  unused `except ... as exc` binding was the only sign that logging had been intended. A failed
  speed sample logs at `debug` and reads as `0 B/s`; a transient failure recovers on the next
  `refresh()`.

`StatisticsView` **does** live-refresh, unlike the old popup: an open dialog that re-queried on a
timer would have needed to be closed on shutdown to avoid touching a deleted `Database`, but the
panel is owned by the window, so the question does not arise.

Sizes are shown as `"1.2 GB / 340 MB"` with a tooltip carrying the exact byte count, so the user
is never told "1.2 GB" when it means 1,204,182,912. `humanize.naturalsize` is already a
dependency and is the right formatter, with `binary=True` to match the details panel.

### 5.3 Persistence

Two different keys, because they are two different lifetimes:

| State | Key | Notes |
| :--- | :--- | :--- |
| Active panel mode + Stats subtab | `window_state` DB blob → `details_state.current_mode` / `stats_subtab` | `DetailsPanel.get_state` / `restore_state`; default subtab `"Volumes"` |
| Cumulative, chart kind (bars/line) | UI state `statistics_dialog_size` | `StatisticsView._restore_cumulative_preference` / `_restore_chart_kind_preference` |

### 5.4 Status bar (optional, cheap)

The status bar already has a speed label (`_status_label`, `main_window.py:947`). Adding a
permanent `Today: 1.2 GB` chip is a two-line change to `_update_count_label` and delivers
the "live status bar" half of the reference feature without any of §5.2. Worth doing
alongside, not instead — the panel is for the breakdown, the chip is for ambient awareness.

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

| Test class | Asserts |
| --- | :--- |
| `TestStatsBuckets` | Empty DB reports zeroes not `None`; the today/week/month/year windows nest outwards; byte-exact totals; seeding counts as completed; an in-flight download is not completed; `NULL` sizes cannot produce `None` |
| `TestStatsBucketByLocalDay` | The UTC-stamp-vs-local-cutoff bug, from every angle: the boundary at local midnight, SQLite `localtime` ≡ Python `astimezone()`, and **whitebox pins** that `_STATS_LOCAL_DAY` / `_MONTH` / `_HOUR` / `_5MIN` still contain `localtime` — so the bug fails the suite on a UTC+00:00 host too, where it is otherwise invisible |
| `TestStatsRejectsUnbucketableTimestamps` | A corrupt `added_at` never lands in a dated bucket (the string-comparison trap), still counts towards lifetime, and never produces a bucket literally labelled `None` |
| `TestCompletedAtIsNotRestamped` | The §2.2 fix: the first completion wins, and a seeding session ending cannot move the date |
| `TestStatsValueObjects` | `DownloadStats` / `StatsSnapshot` round-trips through `to_dict` / `from_dict` |
| `TestStatsChart` / `TestSparkline` | Every shape paints: empty, all-zero, single bucket, upload-only, very large values, zero width; the sparkline's bounded history, oldest-dropped order, clamping and peak |
| `TestStatisticsPopup` / `TestStatisticsPopupFailureHandling` | The panel renders every bucket; a broken query **degrades to dashes and logs at `warning`**; a broken speed provider samples `0 B/s` and stays diagnosable; a transient failure recovers on the next `refresh()` |
| `TestToolbarAction` + panel tests | The entry lives in the Tools menu (full label, no shortcut, not the toolbar); opening it **activates the Stats tab on the bottom panel**; the subtabs exist; switching away, hiding the panel and shutting the window all stop the speed timer |
| `TestChartSelectors` | The 14 ranges and 4 granularities, the 30-day/day default, range narrowing, all-time having no lower bound, and per-bucket merging |
| `TestStatsGapFilling` | `fill_series_gaps` fills every interval of the range, including 5-minute granularity over seven days |
| `TestTransferredBytesStatistics` | The `bandwidth_history` path: a download queued in the past tracks bytes **transferred today**; 5-minute and hourly bucketing; the legacy backfill from `downloads` |
| `TestCumulativeStats` | The Cumulative toggle accumulates the series, persists in UI state and paints |
| `TestChartKindToggle` | Bars are the default, an unknown kind falls back, the two views are independent of Cumulative, and the choice survives a restart |

The placement matters more than it looks — a strip button reads as an action on the selection
when the view reports on the whole history — and without a test the next toolbar or menu edit
silently moves it. The panel wiring has the same property: `test_opening_it_activates_stats_tab_on_bottom_panel`
is what stops the Tools entry from silently reopening a window that no longer exists.

Timezone note: the conversion reads the host clock, so these fixtures are built as **UTC
instants that resolve to a chosen local moment** (local noon → `.astimezone(utc)`), and
expectations are computed with `datetime.fromisoformat(...).astimezone()` rather than
hard-coded. A naive `datetime(...).isoformat()` reads as UTC in SQLite and is shifted a
second time, which drifts past midnight on any machine far enough from Greenwich — the
fixtures would then assert the wrong day on someone else's machine while passing on the
author's.

---

## 8. Performance trigger for revisiting

Re-measure when any of these becomes true:

- `bandwidth_history` rows > ~50k (5-minute buckets × queues — about 288 rows per queue per day,
  so roughly six months of one queue), or
- the Stats tab is opened more than a few times an hour, or
- `get_download_stats` exceeds ~50 ms in a profile.

At that point: `idx_bandwidth_history_period` already exists on `period_start`; extend it to a
covering index `(queue_id, period_start)` and add a `stats_daily` rollup refreshed lazily (one row
per day per bucket, invalidated by the newest `period_start`). The `bandwidth_history` table is
itself append-only apart from the upsert, so a retention policy (delete rows older than N months
on startup) is the other lever — and unlike a rollup it cannot drift.

---

## 9. Known limits of this design

1. **`completed_at` is unreliable** until §2.2 is fixed. The design routes around it by
   bucketing counts on `added_at`; it does not fix it.
2. **No *rate* time series.** `bandwidth_history` (§2.4) records transferred bytes per 5-minute
   interval, not a sample of the instantaneous rate, so a speed graph across sessions still needs
   a different table — a `download_events` row written per sample — with a retention policy. What
   §2.4 does retire is the narrower claim this section used to make, that *no* history existed.
3. **Bytes are counted when they move, not when a download is added.** A long download that
   spanned a window boundary has its volume split across the days the bytes actually arrived,
   which is the intended behaviour for traffic and the reason the volume and count columns must
   be read as answering different questions (§4.2).
4. **`downloaded_size` is a floor while in flight** (§2.3), so the *fallback* aggregation — used
   only until the first history row exists — under-reports an in-flight total. Once
   `bandwidth_history` has rows this no longer applies.
5. **Deleted downloads are gone.** `Database.delete_download` removes the row, so the count
   columns shrink when the user tidies up; `bandwidth_history` keeps the volume, because it is
   keyed by interval and queue rather than by download id. If permanent history is wanted, that
   is a `deleted_at` column and a policy decision, not a query change.

---

## 10. Implementation order

Historical — the order the feature was actually built in, kept because several of the steps
exist only because of the one before them:

1. Fix `completed_at` re-stamping (§2.2) — small, and it unblocks everything else.
2. `DownloadStats` / `StatsSnapshot` dataclasses.
3. `Database.get_download_stats(today)` + the data-layer tests from §7.
4. `StatsChartWidget` — the bar chart first (§4.2), since its data already exists.
5. The totals grid plus the chart in a popup, plus the widget tests.
6. `_act_tools_stats` in `_setup_menubar`, grouped with Preferences, plus the placement test.
7. The live speed sparkline (§4.3), reading the same aggregate as the status bar.
8. Optional: the status-bar chip (§5.4).
9. Update `docs/architecture/main.md` and the README documentation index.
10. **Since the original doc:** the range/granularity pickers, `bandwidth_history` and the
    transferred-byte series, gap filling, cumulative and the bars⇄line toggle — and finally the
    move from the popup into the bottom details panel as the 📊 Stats tab (§5.2).
