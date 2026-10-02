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
- A **Statistics** entry in **Tools**, grouped with **Preferences**, opening a popup.

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
-- lifetime / all-time
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

The two constants in `Database` — `_STATS_LOCAL_DAY` (width 10) and
`_STATS_LOCAL_MONTH` (width 7) — are written out verbatim in both the SELECT list and the
WHERE clause. SQLite will not let a `WHERE` reference a `SELECT` alias, so the expressions
are duplicated by necessity; keep them identical or the filter and the group key diverge.

Because the conversion reads the host timezone, injecting `today` alone cannot make the
SQL deterministic across machines. Tests must compute expectations with
`datetime.fromisoformat(stamp).astimezone()` rather than hard-coding a UTC date, and one
test asserts directly that `_STATS_LOCAL_DAY` contains `localtime` — a whitebox pin, so the
bug still fails the suite on a machine that happens to sit on UTC+00:00 where it would
otherwise be invisible.

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
| Daily volume (down / up) | `GROUP BY` on the local day (§3) — already there | **Build now** |
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

Data comes from a `GROUP BY` on the **local** day (`_STATS_LOCAL_DAY`) or local month
(`_STATS_LOCAL_MONTH`) — see §3 for why the stored UTC stamp has to be converted first. Two
independent pickers drive it — **Range** (last 7 days / 30 days / 12 months / all time) and
**Group by** (per day / per month) — because they are independent questions: “the last year”
and “per month” have to be combinable, or the chart is useless at both extremes.
`Database.get_download_stats(today, since=..., bucket=...)` takes both explicitly, and the
fixed summary rows are deliberately unaffected, so a chart range cannot silently redefine
“this week”.

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
against it there: a strip button reads as an action *on the selection* when the popup reports
on the whole history, and the abbreviated label it had to wear (`Stats…`, see
[`.agents/workflows/qt-ui.md`](file:///d:/Projects/my-idm/.agents/workflows/qt-ui.md)) read as a
different feature from the Tools entry of the same name. Preferences keeps its toolbar button,
because it opens a window about the selected download's *settings*; the abbreviation is the
only concession that strip makes.

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

#### 5.2.1 Ownership — one popup at a time

`MainWindow._on_show_statistics()` is the only creator, and the popup is modeless
(`show()`, not `exec()`). Two consequences that had to be handled explicitly:

* **The reference must be held.** The `QAction.triggered` connection discards the
  handler's return value, so a popup whose only reference is a local variable is
  collectable — and its 1 Hz `QTimer` with it. `_stats_dialog` holds it for the window's
  lifetime, and `MainWindow.closeEvent` closes it before the manager stops, so the timer can
  never fire against a torn-down model.
* **A second click must raise, not duplicate.** Nothing deleted the dialog on close, so
  every toolbar click used to leave another live `QDialog` — widgets and a timer each — on
  screen for the rest of the session, and re-opening showed a *second* window rather than
  the existing one. The popup is now marked `WA_DeleteOnClose` and `_stats_dialog` is
  cleared from `finished`, so the next click starts clean. A `RuntimeError` from an
  already-destroyed C++ object is treated as "build a fresh one" rather than propagated.

#### 5.2.2 Failure is logged, never silent

`refresh()` catches everything around `get_download_stats()` and renders an em-dash in each
cell, because a statistics view must not be able to take the app down. It **logs at
`warning` with `exc_info=True`** — swallowing it silently leaves the user with an empty
dialog and nothing for a bug report to work from, and the unused `except ... as exc` binding
was the only sign that logging had been intended. A failed speed sample logs at `debug` and
reads as `0 B/s`; a transient failure recovers on the next `refresh()`.

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
| `test_today_excludes_yesterday` | The cut-off is inclusive of today only |
| `test_a_file_added_at_local_midnight_is_today` | The boundary case the UTC bug lived on |
| `test_sqlite_and_python_agree_on_the_converted_day` | SQLite `localtime` ≡ Python `astimezone()` |
| `test_the_day_expression_converts_to_local_time` | Whitebox pin: catches the bug on a UTC host too |
| `test_a_row_with_no_added_at_is_excluded` | `''` must not bucket into the epoch |
| `test_a_corrupt_timestamp_never_lands_in_a_dated_bucket` | Skipped, not crash-prone |
| `test_a_corrupt_timestamp_still_counts_towards_lifetime` | An undatable row is still the user's download |
| `test_a_corrupt_timestamp_never_produces_a_none_bucket_on_the_chart` | The `IS NOT NULL` guard, on the All-time range |
| `test_the_today_headline_agrees_with_the_table_today_section` | The popup and the table cannot disagree |
| `test_month_and_year_buckets_span_the_right_range` | Off-by-one on the cutoffs |
| `test_completed_counts_seeding_rows` | Seeding is a completed payload that is still uploading |
| `test_null_sums_become_zero` | A row with `NULL` size cannot produce `None` in the UI |
| `test_unicode_and_very_long_filenames_do_not_affect_totals` | Only byte columns are summed |
| `test_stats_popup_renders_every_bucket` | Widget test: five rows, correct text |
| `test_a_broken_query_is_logged_at_warning` | Degrades to dashes *and* stays diagnosable |
| `test_a_second_click_reuses_the_open_popup` | Modeless + no deletion used to stack dialogs |
| `test_the_popup_is_marked_for_deletion_on_close` | Otherwise the C++ dialog outlives every click |
| `test_the_grid_is_defined_exactly_once` | `_populate_grid` was defined twice; the first was dead |
| `test_it_lives_in_the_tools_menu_and_not_the_toolbar` | The placement is a real requirement, both ways |

The placement matters more than it looks — a strip button reads as an action on the selection
when the popup reports on the whole history — and without a test the next toolbar or menu edit
silently moves it.

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
6. `_act_tools_stats` in `_setup_menubar`, grouped with Preferences, plus the placement test.
7. The live speed sparkline (§4.3), reading the same aggregate as the status bar.
8. Optional: the status-bar chip (§5.3).
9. Update `docs/architecture/main.md` and the README documentation index.
