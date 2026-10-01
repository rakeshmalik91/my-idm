# Qt & UI Conventions — the long form

Each rule here is a **bug that shipped**. AGENTS.md keeps a one-line trigger for all of them;
this file is the "why", so none of them gets re-introduced by someone who never saw the
original. See [`../AGENTS.md`](../AGENTS.md) for the index.

---

## 1. Adding a downloads-table column

**Trigger:** the downloads table needs a new column.

- **Append** a new `Col` constant. **Never insert**, because persisted `column_widths`,
  `header_state` and `sort_column` in `ui_state` address columns by *logical index*. Inserting
  silently re-points every saved column width at the wrong field.
- Add a `NOT NULL DEFAULT ''` column guarded by `PRAGMA table_info` in
  `Database._create_tables()`. Migrations stay additive and idempotent, and existing rows read
  back with the default.
- Record `column_count` in the saved UI state (already done). A state whose stored count differs
  from `Col.COUNT` predates the append, so the tail is re-pinned on restore and the user's own
  ordering of the older columns survives.
- Add the column to `_DEFAULT_TAIL_COLUMNS` in `main_window.py` **and** set a default width in
  both `_setup_ui()` and `_on_reset_view()`.
- The tail helper must stay an **ascending** sweep. `moveSection()` shifts everything between
  source and target, so placing a column left of its target displaces an already-placed
  neighbour — and the result looks correct until a column vanishes.

---

## 2. Adding or reordering a Preferences page

**Trigger:** a new Preferences tab, or a reorder of the existing ones.

- Append to `TAB_ORDER` **and** add a matching entry to `TAB_TITLES` in `settings_dialog.py`.
  `TAB_ORDER` *is* the insertion order; `_setup_ui` walks it. Tests pin the registry against the
  dialog's real page order, titles and count, so the two cannot drift.
- **Never hard-code a page index.** Callers pass a `TAB_*` name and resolve it with
  `tab_index()`.

Why the second rule is marked rather than stylistic: `SettingsDialog.__init__` clamps an integer
with `0 <= initial_tab < count()`, so a stale index opens the **wrong page** silently — no
exception, no log. That is exactly what the 6 → 9 tab split did to all six Tools-menu entries:
`initial_tab=5` meaning "AnimePahe" quietly became "Tor". `tab_index()` raises `ValueError` on an
unknown name, which turns that silent failure into a loud one.

---

## 3. A `QDialog` opened with `show()` must be owned

Hold a reference on the window (`self._stats_dialog`), set `Qt.WA_DeleteOnClose`, **raise** the
existing instance instead of building another, and close it in `MainWindow.closeEvent` so its
timer cannot outlive the manager.

A modeless dialog referenced only by a local is collectable, timer and all: `QAction.triggered`
discards the handler's return value, so nothing holds the only reference. A modeless dialog
nothing deletes then stacks up one window per click.

---

## 4. Never let a right-click activate a destructive menu entry

`QMenu` treats a right-button press + release as an ordinary activation. On the tray menu —
where the bottom two rows are Restart and **Exit** — a reflexive right-click used to shut the app
down mid-download with no confirmation.

`_RightClickGuard` in `main_window.py` consumes `MouseButtonPress`, `MouseButtonRelease` **and**
`MouseButtonDblClick` for `RightButton`.

Swallowing **all three** is required. Filtering only the release still lets the press reach
`QMenu`'s activation logic, so a partial guard looks installed and changes nothing. Left/middle/
back and every non-mouse event must pass through — a test triggers the real item to prove the
menu is still usable.

---

## 5. A stretched list must not end mid-row

`_WholeRowListWidget` in `settings_dialog.py` constrains the viewport to a whole multiple of the
row height, so the leftover from a stretched layout becomes blank space instead of a **bisected**
row — a half-height checkbox and a name cut through the middle, which reads as a broken control.

Two rules make it work:

- **Derive the snap from the widget's height, never the viewport's.** The viewport is already
  clamped by the previous snap, so measuring it makes the transform non-monotonic: once shrunk, a
  taller window can never grow the list back.
- **Keep a row-count assertion next to the no-partial-row one.** A snap that keeps shrinking
  loses rows the window had room for, which is worse than the defect it replaced.

---

## 6. Toolbar labels may be abbreviated; menu labels may not

The Statistics button next to the playback controls reads `Stats…` — `Statistics…` ate a third of
the toolbar strip. The Preferences button spells itself out (`Preferences…`): the toolbar has the
room, and the former `Prefs…` read as a different feature from the Tools menu entry of the same
name.

The Tools menu keeps its own `_act_tools_preferences` for a second reason: **a shortcut lives on
exactly one `QAction`**, so `Ctrl+,` stays on the toolbar copy alone.

Anything keying off `action.text()` — notably `TestKeyboardShortcuts._shortcuts()` in
`tests/test_infra_hardening.py` — must be updated with the label.

---

## 7. Apply the theme before building widgets, never after

`Colors` is resolved when an inline stylesheet is *constructed*, not when it is painted, so
`MainWindow.__init__` calls `_apply_persisted_theme()` **before** `_setup_ui()`. Applying it
afterwards leaves every widget that drew itself with the previous palette — which is how the
details-panel sidebar stayed dark in the light theme while the sheet around it went light.

`apply_theme` also returns early when the requested theme is already live: `setStyleSheet`
restyles the whole application, so calling it unconditionally would make every Preferences save a
full restyle.

---

## 8. Do not bundle a palette refactor into a theme selector

Light was added alongside the existing dark theme, which is **byte-for-byte unchanged**. An
attempt to also route the ~190 hard-coded hex values in the per-widget inline stylesheets through
the palette was **reverted**: it collapsed near-duplicate shades onto shared slots and visibly
altered the established dark theme — black toolbar buttons, black panel sides.

If that conversion is revisited it must be **1:1**: one palette key per distinct hex, dark value
equal to the original, so the dark theme is *provably* unchanged rather than argued about.