# Named Queues & Concurrency Budgets

**Implemented 2026-10-01.** This document now describes shipped behaviour. Two related
features from [`docs/todo/feature-pool-rdm.md`](file:///d:/Projects/my-idm/docs/todo/feature-pool-rdm.md)
were designed alongside it and are **still not implemented** — see [Not implemented](#not-implemented).

---

## Overview

My-IDM has named download queues, each with its own limit on how many of its downloads run at
once. The motivating case is a user with one 40-segment torrent and four small files: they can
put the torrent in its own queue with `max_concurrent = 1` and let the small files run together
in Default.

Three knobs answer that, in increasing order of power:

1. **Per-queue concurrency** — implemented. "At most 1 torrent at a time, but all the small
   files together."
2. **Per-queue bandwidth ceilings** — implemented. "Downloads in this queue may not exceed
   500 KB/s, so the torrent cannot eat the line."
3. **An off-peak schedule** — *not implemented*.

The global `GeneralConfig.max_concurrent_downloads` remains a **ceiling over everything**. A
download starts only when both its own queue's budget and the global budget allow it, so a user
with eight queues cannot accidentally run 24 transfers at once.

---

## Source queues

Two queues are seeded on every open alongside Default, so a user's AnimePahe and YouTube
downloads are separable with no setup:

| Queue | Fixed id | `max_concurrent` | Deletable? |
| :--- | :--- | :--- | :--- |
| **AnimePahe** | `queue-animepahe` | `0` (unlimited) | No |
| **YouTube** | `queue-youtube` | `0` (unlimited) | No |

Fixed ids, seeded with `INSERT OR IGNORE` in `_seed_default_queue` for the same idempotency
reason as Default. `max_concurrent = 0` because these exist for organisation, not capping — a
user who wants a limit sets one. They can be **renamed and re-limited** but not deleted:
deleting one would not break anything, it would silently send that whole source to Default and
look like the routing had stopped working.

### Routing

`DownloadManager._infer_queue_for_source` runs in `add_download` **only when the caller passed
no queue**, so an explicit choice is never overridden:

| Signal | Route |
| :--- | :--- |
| `metadata["source_type"]` contains `youtube` | YouTube |
| `metadata["added_by"]` contains `animepahe` | AnimePahe |
| URL host is `youtube.com` / `youtu.be` / `m.youtube.com` | YouTube |

The URL check is what catches a YouTube link pasted into the Add Download dialog or captured
from the browser, which carries no `source_type`.

### Backlog files

`queue=` on a download line names that line's queue; `queue:` on its own line is a **sticky
directive** applying to everything after it, exactly as `dir:` does. A per-line value wins over
the sticky one. Both forms are accepted:

```
# queue: YouTube
https://youtu.be/a
https://youtu.be/b

https://example.com/film.mkv | queue=AnimePahe
https://example.com/other.zip queue="Big files"
```

Names, not ids — a uuid in a hand-editable text file would be unusable. An **unknown name falls
back to Default and logs a warning** rather than creating the queue: backlog files can be
machine-generated, and auto-create plus a generator is how you end up with "Queue1", "Queue2".

---

## Data model

### The `queues` table

```sql
CREATE TABLE IF NOT EXISTS queues (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    max_concurrent  INTEGER NOT NULL DEFAULT 3,
    position        INTEGER NOT NULL DEFAULT 0,
    is_default      INTEGER NOT NULL DEFAULT 0,
    color           TEXT NOT NULL DEFAULT '',
    download_limit  INTEGER NOT NULL DEFAULT 0,
    upload_limit    INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT ''
);
```

`color`, `download_limit` and `upload_limit` are each added by an explicit `ALTER TABLE` guard in
`Database.open`, because `CREATE TABLE IF NOT EXISTS` will not add a column to a table that already
exists — an install that opened before a column was introduced would otherwise fail on every queue
read. `_row_to_queue` reads all three defensively for the same reason.

Declared in `Database._create_tables` at `database.py:604`. Four decisions in it:

**No foreign key from `downloads`.** `PRAGMA foreign_keys=ON` is set and `segments` already uses
`ON DELETE CASCADE`, so an FK is tempting — but SQLite resolves a forward `REFERENCES` only for a
`DEFERRABLE` constraint, and `downloads` is created *first* in the `executescript` block. An FK
would need `downloads` moved after `queues`, or adding it by `ALTER`, which SQLite cannot do. It
would buy nothing: queue deletion is a manager-level operation that reassigns rows before
deleting, in one transaction.

**`max_concurrent <= 0` means the queue adds no cap of its own** — it is displayed as
**`Global`** in the queue manager and resolved to "unlimited *within this queue*" by
`QueueInfo.effective_max_concurrent` (`database.py:357`). The global limit always applies on top,
which is why the word is `Global` and **not** `No limit`: a user who reads "No limit", sets four
concurrent downloads, and sees the global cap of three silently win has been given no way to
understand why. The **default queue is seeded at `0`** for the same reason — it holds everything
unclaimed and should follow the global limit rather than impose a second, redundant ceiling.

`INSERT OR IGNORE` means an existing profile keeps whatever its Default limit already was; the
seed only decides the value for a fresh install.

`position` and `is_default` exist because the default queue is pinned. `get_queues()` orders by
`is_default DESC, position, name`, so Default is always first and cannot be reordered or renamed
or deleted — but its **limit is editable like any other queue's**.

### `downloads.queue_id`

Appended to the DDL (`database.py:584`) and migrated additively:

```python
# database.py:643
if "queue_id" not in cols:
    self._conn.execute("ALTER TABLE downloads ADD COLUMN queue_id TEXT NOT NULL DEFAULT ''")
```

`DEFAULT ''` rather than the default queue's id, because `ALTER TABLE ADD COLUMN` can only take a
constant — never a subquery. The backfill is therefore a second statement, in
`_seed_default_queue` (`database.py:665`), and it is **idempotent**:

```sql
INSERT OR IGNORE INTO queues (id, name, max_concurrent, position, is_default, created_at)
VALUES ('default', 'Default', 3, 0, 1, ?);
UPDATE queues SET is_default = 0 WHERE id != 'default';
UPDATE downloads SET queue_id = 'default' WHERE queue_id = '' OR queue_id IS NULL;
UPDATE downloads SET queue_id = 'default' WHERE queue_id NOT IN (SELECT id FROM queues);
```

The last statement is the one that matters: a queue deleted out from under its downloads (by
hand in the DB) would otherwise leave rows pointing at nothing, invisible to every scoped read.

`_seed_default_queue` runs on **every** `open()`, not behind a one-shot migration flag, because
all four statements are idempotent. `DEFAULT_QUEUE_ID` is a module constant rather than a
generated uuid precisely so that `INSERT OR IGNORE` makes it so.

### `queue_order` is priority, not membership

`queue_order` survives untouched, and that is the point: `0` is a meaningful "not in the active
queue" sentinel (`pause_download` resets it), and every sort key maps it to `999999` to push the
row last. `queue_id` answers "which group?", `queue_order` answers "in what order within it?".
`get_next_queue_order(queue_id)` is now queue-scoped (`database.py`), so a torrent promoted to
position 1 of its own queue does not push everything in Default down by one.

### The write paths normalise, so no reader has to guess

`Database.resolve_queue_id` (`database.py:897`) maps a blank or dangling id onto the default, and
is called from `add_download` and `update_download`. Without it a row added with the default
`queue_id=""` — which every pre-queue caller passes — would be invisible to every queue-scoped
read until the next `open()` re-ran the backfill.

---

## The start gate

Three call sites reach `_start_entry` without passing through each other: `_process_queue`,
`add_download` and `resume_download`. A check written into only one of them is a check that will
be bypassed, so the decision lives in one method:

```python
# manager.py:1830
def _may_start(self, entry, counts, limits, global_max) -> bool:
    if sum(counts.values()) >= global_max:
        return False
    limit = limits.get(self._db.resolve_queue_id(entry.queue_id), 0)
    if limit > 0 and counts.get(self._db.resolve_queue_id(entry.queue_id), 0) >= limit:
        return False
    return True
```

`force_start_download` deliberately bypasses it — that is the user's "I know, just start it".

### Counts are one grouped query

```python
# manager.py:1807
def _active_counts_by_queue(self) -> dict[str, int]:
    counts = self._db.get_active_counts_by_queue()   # one GROUP BY, indexed on queue_id
    for did in self._starting_downloads:
        entry = self._db.get_download(did)
        if entry:
            key = self._db.resolve_queue_id(entry.queue_id)
            counts[key] = counts.get(key, 0) + 1
    return counts
```

This replaces the previous `_get_active_download_count()`, which did a **full table scan per
candidate** — `_process_queue` read it once up front and again per row, so one dispatch pass was
O(n²) `SELECT *` round-trips. Per-queue accounting made the grouped form *cheaper* than the
single global count it replaced.

`_starting_downloads` is added in because it covers the window between deciding to start a
download and the engine reporting it as `downloading`. Without it a queue's first start always
overshoots its own budget by exactly that set's size.

### Dispatch order

`_process_queue` (`manager.py:1847`) sorts **within** each queue by
`(queue_order, added_at)`, and walks queues in this order:

1. queues that still have budget, by switcher order;
2. then saturated queues, also by switcher order.

The second rule is what stops one busy queue starving the rest: a saturated queue *sinks* rather
than being dropped, so it is picked up the moment a slot frees.

Ties break on the switcher order (`enumerate(get_queues())`), **never** on the raw id. Ids are
uuids, so an id tie-break made the dispatch order differ between two runs with identical state —
caught by a test that runs the same scenario repeatedly.

`GeneralConfig.effective_max_concurrent` (`config.py:100`) replaced the `<= 0 → 3` fallback that
had been retyped at four call sites and had already drifted once.

### Startup

`DownloadManager.start()` walks all downloads in priority order calling `resume_download`, which
enforces the budgets. Each queue therefore fills to its own limit and the overflow stays queued
for the 1 Hz tick, rather than being started and then blocked.

---

## Priority reordering

`move_queue_up` / `move_queue_down` (`manager.py:2709`) are **scoped to the download's own
queue**. The previous implementation sorted the entire history — completed rows included — so the
control let a row appear to climb across queues, which does nothing.

The implementation moves the entry in the list and then renumbers **the whole queue** densely:

```python
moving = siblings.pop(idx)
siblings.insert(target_idx, moving)
for position, item in enumerate(siblings, start=1):
    if item.queue_order != position:
        self._db.update_queue_order(item.id, position)
```

Two bugs found while writing this, both now pinned by tests:

- Renumbering only the *affected run* left the rows before it holding the value the moved row had
  vacated, producing **two downloads with the same priority**.
- Renumbering the already-sorted slice without reordering it is the identity permutation, so the
  move silently did nothing.

---

## View scoping

A queue scope is a **filter, not a tab bar**. It has to compose with search, the header filter
chips and segregated view simultaneously, which is `_matches_filter`'s job:

```python
# download_model.py:563
def _in_queue_scope(self, entry) -> bool:
    if not self._queue_scope:
        return True
    return (entry.queue_id or DEFAULT_QUEUE_ID) == self._queue_scope
```

It is its own method rather than a line inside `_matches_filter` because the three `get_*_counts`
members hand-roll their own filter chains over `_all_entries`. A scope applied in only one of them
would make the header chip counts disagree with the rows they filter.

`is_filtered()` (`download_model.py:668`) includes the scope, so the UI shows the "filtered"
state and the Reset control clears it.

### The default scope is "All Queues"

Deliberate and non-breaking. History is the product: a user must never find downloads they
already had missing because a queue is selected. The scope is persisted in `ui_state` under
`active_queue_id` and restored by `MainWindow._init_queue_scope` (`main_window.py:2367`) after
`_load_history()` — after, so a filtered-out download never flashes during startup.

---

## Colours

Every queue has a swatch colour (`queues.color`, `#rrggbb`), set in **Manage Queues** by
clicking the swatch beside the queue's name.

- **Seeded distinctly.** Default `#4a9eff`, AnimePahe `#ff7b72`, YouTube `#ff5c8a`. Distinct
  hues rather than shades of one, because a colour is an identifier at a glance and "slightly
  different grey" is not one.
- **A new queue is never colourless.** `Database.create_queue` assigns the first unused colour
  from `QUEUE_COLOR_PALETTE`, so a queue is visible in the list immediately rather than being an
  invisible swatch the user has to think to go and colour.
- **An existing choice is never overwritten.** `_seed_default_queue` backfills colour only
  `WHERE color IS NULL OR color = ''`, so reopening the app cannot repaint a queue the user
  coloured.
- `normalize_queue_color` accepts `#rgb`, `#rrggbb`, a missing `#`, and surrounding space, and
  returns `""` for anything else — so a hand-edited or junk value cannot end up as an
  unparseable colour that paints as an invisible swatch.

### In the downloads list

`Col.QUEUE_NAME` is painted by **`QueueColumnDelegate`** (`delegates.py`): a rounded swatch
beside the queue name, and **the swatch alone when the column is too narrow**.

The fallback is the point. A half-elided name next to a swatch is worse than the swatch alone —
the colour still identifies the queue, whereas `Torr…` does not. The decision is made against
the measured text width rather than a guessed column width, so a short queue name keeps its
label at the same column width at which a long one collapses.

The colour reaches the delegate through a dedicated model role, `QUEUE_COLOR_ROLE`
(`Qt.ItemDataRole.UserRole + 1`), not `UserRole` — which is already column-specific, answering
the source *domain* for that column — and not by decoding a colour back out of the cell text,
which would tie the delegate to a presentation detail. The model has no database handle, so
`MainWindow._refresh_queue_ui` supplies both `set_queue_names` and `set_queue_colors`; a queue
with no colour falls back to plain text rather than to a blank cell.

## User interface

| Surface | Location |
| :--- | :--- |
| **Queue** column in the downloads table — swatch + name | `Col.QUEUE_NAME`, painted by `QueueColumnDelegate` |
| **Edit → Queues**: the scope switcher — "All Queues" plus one row per queue, New Queue…, Manage Queues… | `main_window.py` `_setup_menubar` |
| **Edit → Move to Queue** — re-home the selected rows | `main_window.py` `_setup_menubar` |
| **Move to Queue** on the row context menu | `main_window.py` `_show_context_menu` |
| **Queue of the selection**, in the status bar | `MainWindow._update_queue_status` |
| Colour swatch, in the Queue column of the manager dialog | `QueueManagerDialog._name_cell` |
| `QueueManagerDialog` — create, rename, reorder, limit, colour, delete | `dialogs.py` |

### Why the menus are under Edit

Every queue operation acts on the **selected downloads** — re-home them, change their order,
create a group for them — which is what the Edit menu is for. View is for how the list is
*drawn*, and the Queues menu originally lived there on the reasoning that switching the visible
scope is a view operation. Putting it in Edit instead groups it with **Move Up** / **Move Down**,
which are the other two ways of saying "these rows are in the wrong order", and gives the
keyboard path a home alongside the row context menu.

`Edit → Move to Queue` is the menu twin of the context menu's. Its entries are plain actions
rather than checkmarks, because a multi-row selection can span queues and there is no single
current queue to tick.

Both the submenu **and** its actions grey out when nothing is selected, re-evaluated from
`_on_table_selection_changed` rather than only when the menu is rebuilt. Two separate mistakes
there: enabling only at rebuild time left the menu greyed out *with rows selected*, because
nothing re-ran it between the user clicking a row and reaching for the menu; and disabling only
the submenu left the actions inside it looking live, which is what made it read as "not
clickable" rather than "nothing is selected".

Dimming needed a palette entry of its own: there was **no `QMenu::item:disabled` rule at all**,
so Qt's own disabled rendering applied — which on this palette is barely dimmer than the enabled
text. `Colors.TEXT_DISABLED` is deliberately darker than `TEXT_DIM` (dark `#4d5560` on
`#161b22`, 2.3:1, against `TEXT_DIM`'s 3.8:1). `TEXT_DIM` is for *de-emphasised* text;
`TEXT_DISABLED` is for a control that cannot be used right now, and reusing the dimmer of the
two is what left the greyed-out item looking live.

A queue's limit is not on the scope menu — the entries are bare names, so a checkmark is never
next to a number that has to be re-read. The limit lives where it is edited: the **Max at once
(0 = Global)** column of `QueueManagerDialog`, with the meaning of `0` in the column header, the
cell tooltip and the note under the table.

There was also a toolbar combo showing the limits inline (`Default  (Global)`,
`Torrents  (max 1)`), which made a limit visible without opening anything. It was removed on
2026-10-02 with the rest of the strip's non-transport controls; the queue column in the downloads
table and the status-bar queue of the selection are what remain visible at a glance.

### `QueueManagerDialog`

Five columns — **Queue**, **Downloads**, **Max at once (0 = Global)**, **Download limit (0 =
Global)**, **Upload limit (0 = Global)** — with each limit edited in place by a `QSpinBox`
**in the column it edits**. There is deliberately no separate edit column: it used to hold the
editor while its own column showed read-only text, which made the column look like two unrelated
things and left the default queue as the one row with a blank cell, which read as "not editable"
rather than "deliberately pinned".

The colour swatch in the Queue column carries the queue's initial, upper-cased rather than taken
from the name as typed — a queue can be called "work downloads", and a lowercase `w` beside a
colour reads as a different queue from `W`.

The bandwidth ceilings are **stored in bytes/sec and edited in KB/s**, because KB/s is the unit
the global limit is set in everywhere else in the application, and a queue limit typed in a
different unit from the global one it is compared against is a limit nobody can reason about.

**Column widths are measured, not declared.** Three things had to be measured, and getting only
the first two is what trimmed the headers in the first place:

- `QHeaderView.setStretchLastSection` defaults to **True**, which forces the last section to the
  leftover width and overrides `ResizeToContents`.
- The **text**: header width follows the user's font, its size and the display's DPI, so a
  hard-coded width cannot be right on more than one machine. `_fit_width_to_headers()` measures
  each header with `fontMetrics().horizontalAdvance`.
- **The chrome around the table**: the layout margins, the table's frame and a scrollbar
  allowance. The table does not get the whole window, so a window sized from the header text alone
  comes up short by exactly that much — and Qt resolves the shortfall by shrinking every section to
  fit the *viewport*, which clipped the first and last letter of every header (`OWNLOADS`,
  `UPLOAD LIMIT (0 = GLOBAL`). Computed rather than measured, because a widget that has never been
  shown has no reliable viewport width; `tests/test_queues.py` checks the result after `show()`.

`(0 = Global)` sits on its **own header line**. On one line the three limit headers are the widest
thing in the dialog, so the window has to grow to hold text that is mostly punctuation — which is
how the headers got squeezed in the first place. The header section is therefore sized from the
live font too, since `QHeaderView` otherwise allows for one line and clips the second.

`MAX_DIALOG_WIDTH` clamps the demand so a small screen gets a scrollbar rather than a window wider
than the desktop.

The limit fields show **plain numbers, never a substituted word**. An earlier version used
`setSpecialValueText("Global")`, which was a mistake twice over: typing `0` displayed `Global`,
and typing `Global` was rejected outright — the field stopped agreeing with itself, so "what I
typed" was no longer "what I saw". The meaning of `0` now lives in the **column header**, the cell
tooltip and the note, none of which are edited.

A note under the table states the **live** global limits (not an abstract description of them) and
spells out that a queue limit is a ceiling and never a reservation, since a download starts only
when both limits allow it. It refreshes whenever a limit changes.

Deleting a queue is **safe by construction**: the dialog states how many downloads will move, and
`Database.delete_queue` reassigns them to Default in the same transaction as the delete.
Downloads are never deleted with their queue.

### Bandwidth ceilings

`queues.download_limit` and `queues.upload_limit`, in bytes/sec, migrated in alongside `color` for
the same reason: `CREATE TABLE IF NOT EXISTS` does not add a column to a table that already
exists, so an existing install's queue list would otherwise raise on read.

`0` means **Global** — the queue adds no ceiling of its own — which is deliberately the same word
as `max_concurrent = 0`. Negative input is clamped rather than stored: a negative ceiling reads as
unlimited while looking like a setting.

Resolution lives in **one** place, `utils.effective_rate_limit(queue_limit, global_limit,
allocation)`, because it used to be duplicated in `http_engine` and `torrent_engine` and the two
had already drifted on the "no global limit" case. The rule:

1. The ceiling is the **tightest non-zero** of the queue's and the global one, so a queue can lower
   the global limit and never raise it — the whole point of the feature.
2. Both being `0` means unlimited, and that is the only thing an allocation must not change.
3. Otherwise the ceiling is scaled by the download's `bandwidth_allocation` share.

Step 3 used to be **unreachable whenever the global limit was `0`**, which is the default:
`http_engine._get_effective_download_limit` returned `0` from an early exit before reading the
allocation, so a download set to "low" ran at full speed and nothing said so. A queue limit is now
enough on its own for the allocation to mean something.

The manager keeps a **snapshot** of `queue_id → (download_limit, upload_limit)` in both engines
(`_push_queue_limits`) and re-pushes it on create, delete and every limit edit. A snapshot rather
than a database handle because the HTTP chunk pacers read the ceiling once per chunk, and a query
per chunk to learn a limit that changes when a user edits a dialog would be the hottest query in
the transfer path. A blank `queue_id` resolves to Default, matching `Database.get_queue("")`.

On the torrent side `TorrentEngine.apply_handle_limits` turns the ceiling into
`handle.set_download_limit` / `set_upload_limit`, and is called on allocation change, on a limit
edit, on a global-limit change, and when a handle appears. With no ceiling at all a `max` download
is set unlimited (`-1`) and a reduced one keeps the long-standing 10 MB/s stand-in: a share of
nothing is not zero, and zero would stall seeding outright.

`AddQueueDialog` offers the two ceilings at creation, in KB/s, defaulting to 0.

### Periodic Bandwidth Quotas (Daily / Weekly / Monthly)

In addition to instantaneous speed caps (bytes/sec), My-IDM supports periodic total bandwidth limits (quotas):
- **Cadence Options**: Daily, Weekly, or Monthly.
- **Scope**: Can be applied to specific queues or globally across all queues combined.
- **Global Precedence**: Global limits sum usage across all queues and **strictly override** per-queue limits. If a global limit is reached, all queue transfers are halted even if an individual queue has not reached its local budget.
- **Warning Threshold**: Configurable warning percentage (default 80%). When reached, an indicator badge appears at the **top right corner of the menubar** (`menubar.setCornerWidget(...)`) and the status bar displays a transient notification. Clicking this badge opens Preferences directly on the **Bandwidth Limit** tab.
- **Limit Exceeded (100%)**: When 100% of the quota is reached, active downloads and uploads are automatically paused, the menubar badge shifts to a distinct red limit-exceeded indicator, and the start gate (`_may_start`) refuses to launch any queued transfers.
- **Preferences UI**: Configured under the **Bandwidth Limit** tab (`TAB_BANDWIDTH`), which displays a 7-column management table: `Queue`, `Enabled`, `Limit`, `Limit Type`, `Progress`, `Percetage for Warning`, `Actions`.

### Off-Peak Download Scheduler & Force Start

My-IDM supports an off-peak download scheduler configured in Preferences → **⏱️ Scheduler** (`TAB_SCHEDULER`):
- **Gating**: When enabled, queued downloads only start within the defined recurring local-time window (`start_time` to `end_time`), on configured active days of the week.
- **Clock**: Evaluated using local timezone (`datetime.now().astimezone()`) with injectable clock `now` in `is_within_schedule_window()` and `_may_start()`. Overnight windows spanning midnight (e.g. 23:00 to 07:00) are fully supported.
- **Window Transitions**: Monitored by a periodic timer (`_scheduler_timer`). Entering the off-peak window triggers `_process_queue()`. Exiting the window automatically pauses active downloading transfers when `pause_when_ended` is enabled.
- **Force Start Override**: Users can force start any download via the **Force Start** button (icon-only green play button containing an 'F' on the toolbar, Edit menu, and download context menu). Force-started items set `metadata["force_started"] = True`, bypassing the scheduler and concurrency limits, and remain active when off-peak hours end. Manually pausing an item clears the `force_started` flag.

### Seeing which queue a download is in

Two places, because this is the question the feature exists to answer:

- **`Queue` column** in the downloads table — `Col.QUEUE_NAME`, the last column. Appended, so
  every existing profile's persisted column indices keep their meaning. **Visible by default**
  (deliberately *not* in `DEFAULT_HIDDEN_COLUMNS`): a queue is invisible in the list without it.
  The model has no database handle, so `MainWindow._refresh_queue_ui` supplies
  `set_queue_names` and `set_queue_colors`; a rename or recolour refreshes every affected row at
  once. An unknown id renders as **Default**, never as a raw uuid.
- **Hover tooltip**, carrying the queue name. Not cosmetic: the column collapses to the swatch
  alone when narrow, and a coloured square with no legend is unreadable — the same reason the
  swatch-only fallback exists applies to hover. Served from the model's `ToolTipRole` rather
  than from the delegate, so it does not depend on the column's width. The colour hex is
  deliberately **not** included: nobody identifies a queue by its hex, and it made the tooltip
  read like a debug field.
- **Status bar**, for the current selection: `🗃️ Torrents` for one row, `🗃️ Torrents` for several
  rows all in one queue, `🗃️ 2 queues` for a mixed selection. It is its own label rather than a
  write to `_status_label`, which carries transient action messages ("Copied URL", "Created
  queue") that a selection change would immediately wipe.
- **Move to Queue** submenu, which checkmarks the download's current queue.

### Filtering by queue

The **Queue** column is a filterable header column, alongside Status / Size / Download Type. It
stores **queue ids**, not names: a rename would otherwise leave the filter holding a label that
matches nothing and the view silently empty. The popup is shown with **names** — the pairs come
from `DownloadTableModel.queue_filter_items()` and reach `MultiselectFilterPopup` through a new
`items` argument, since its key→label maps are otherwise hard-coded per column.

- Ticking **every** queue resolves to *no filter*, the same way the other filter columns treat
  "Select All". Otherwise "all ticked" would leave a funnel icon lit with nothing hidden.
- `get_queue_counts()` deliberately does **not** apply the queue filter to itself: a popup that
  counted only ticked queues would show the rest as `0` and read as if they were empty. It does
  honour the other filters, so the numbers agree with the rows.
- Distinct from the **scope**: the scope narrows to one queue, this picks any number, and both
  can be active at once.

`MultiselectFilterPopup` grew an optional `items` parameter rather than a fourth hard-coded map
specifically so this stayed possible — a fifth `if column == ...` branch would have meant the
popup's key space growing one place per filterable column.

`_refresh_queue_ui` rebuilds the Edit ▸ Queues menu wholesale rather than diffing, and the menu is
rebuilt by `clear()` + re-adding the same `QAction` objects in order. An earlier version removed
actions individually with `list.clear()` *inside* the loop iterating it, which ended the iteration
after one action — so every refresh left a stale duplicate queue entry and shuffled "All Queues"
down the menu. It used to refill the toolbar combo in the same pass; with the combo gone the
exclusive `QActionGroup` is the single source of truth for which queue is selected.

> **A Qt slot swallows its exceptions.** `New Queue…` passed `QLineEdit.EditRole.Normal` to
> `QInputDialog.getText`; `QLineEdit` has no `EditRole`, so the call raised `AttributeError`,
> Qt discarded it, and the button simply did nothing — in the context menu *and* in the manager
> dialog's Add and Rename. The correct echo mode is `QLineEdit.Normal`.
>
> The lesson generalises past this bug: an exception inside a slot leaves **no trace in the log
> and no failed test**, and any check that only *prints* a widget's value will report the stale
> previous one and pass. Both handlers now have tests that call them directly, and
> `_queue_display_name` has one for blank and dangling ids because a missing import in that path
> failed exactly this way during development.

Deleting a queue is **safe by construction**: the dialog states how many downloads will move, and
`Database.delete_queue` reassigns them to Default in the same transaction as the delete.
Downloads are never deleted with their queue.

### Which source targets which queue

| Source | Queue |
| :--- | :--- |
| **Add Download** dialog, clipboard monitor | the active queue — both are foreground actions like Ctrl+V |
| Browser capture (`POST /add`), backlog loader | always Default — a background source should not follow whichever view the user happens to be looking at |
| **Move to Queue** context menu | explicit |

---

## Manager API

Signals: `queues_changed` (the list, its limits or its membership changed) and
`queue_scope_changed(str)` (the active scope, `""` for all) — `manager.py:347-348`.

| Method | Notes |
| :--- | :--- |
| `get_queues()` / `get_queue(id)` | `get_queue("")` or an unknown id resolves to Default, never `None` |
| `get_active_queue()` / `set_active_queue(id)` | scope; `""` means all |
| `create_queue` / `rename_queue` / `delete_queue` | return `(ok, message)`; Default is protected |
| `set_queue_max_concurrent(id, n)` | applies on the next 1 Hz tick rather than pre-emptively — pausing a running download to free a slot would be a worse surprise than a one-second delay |
| `set_queue_limits(id, dl, ul)` | bytes/sec; `0` = no ceiling of its own. Reaches a running HTTP download on its next chunk and a torrent on its next handle refresh |
| `move_queue_in_list(id, delta)` | reorder in the switcher |
| `move_downloads_to_queue(ids, id)` | preserves the user's selection order and **continues** the target queue's numbering rather than restarting at 1, which would collide |

One subtlety worth knowing: `get_active_queue()` validates the stored id and falls back to `""`
when it no longer resolves. `delete_queue` therefore compares against the **stored** value, not
the getter — otherwise asking "is the queue I just deleted selected?" always answers no, the
stale id survives in `ui_state`, and the view stays filtered to a queue that does not exist.

---

## Tests

`tests/test_queues.py` — 232 tests: schema and migration from a pre-queue database (including one
with no `color` column and no bandwidth columns), CRUD refusals, queue-scoped reads, budget
enforcement, dispatch ordering (asserted stable across repeated runs), reorder density, model
scoping, the bandwidth ceilings end to end (resolution rule, both engines, the manager's snapshot,
the migration), the manager dialog's editors and header-fitting, and the window wiring.

Useful seams when extending: `Database.resolve_queue_id`, `manager._may_start` and
`manager._active_counts_by_queue` are all directly callable, so budget behaviour can be asserted
without starting an engine.

---

## Not implemented

Designed in this document's original form, still to be built:

- **Absolute per-download bandwidth caps.** Per-download rates exist as a fraction of whatever
  ceiling applies, so a download cannot be given its own rate independent of its queue and the
  global setting — only its `bandwidth_allocation` share of one of them. A real per-download cap
  wants `rate_limit_bps` in `metadata_json` on top of `effective_rate_limit`, and an honest note
  that the four chunk pacers are approximate and overshoot by roughly the segment count.

## Related documents

- [Architecture Guide](main.md) — `Concurrency & Queue Prioritization`.
- [Database & Persistence](database.md) — schema and the migration mechanism.
- [Table Views & Segregation](table-views.md) — the filter machinery the scope composes with.
- [Window Lifecycle & System Tray](window-system-tray.md) — `ui_state` persistence.
