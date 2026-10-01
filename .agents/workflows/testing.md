# Testing Workflow & Conventions

Everything about writing, running and trusting tests in **My-IDM**. Split out of
[`.agents/AGENTS.md`](../AGENTS.md) so that file stays a short index of conventions; this is
the reference to read before adding a test or debugging a flake.

---

## 1. Running the suite

Two tiers. **Use basic sanity by default** — it is safe to leave running in the background.

| Tier | Command | Scope | Time |
| :--- | :--- | :--- | :--- |
| **Basic sanity** | `run_all_tests.bat basic` | 1599 tests. No window, no tray, no real clipboard. | **~54 s** |
| **Full** | `run_all_tests.bat` | All 2544 tests, including UI. | ~7 min |

All three measured on 2026-10-02 via the wrapper. The tiers are **not** proportional: the 945
`ui` tests alone take ~2 min 20 s, because each builds and tears down real Qt widget trees —
roughly 10x the per-test cost of everything else. A full run's slowest single test is
`TestThemeSelector` at ~1 s in isolation but ~14 s in a full run, which points at cross-file
widget accumulation (see §3) rather than at anything in the theme code itself.

Other useful invocations:

```powershell
run_all_tests.bat basic             # the wrapper; recommended
run_all_tests.bat ui                # UI only, ~2 min 20 s
python -m pytest tests/test_queues.py   # one file
python -m pytest -k "expression"        # by name
python -m pytest -m ui                  # UI only, without the wrapper
python -m pytest -x                     # stop at first failure
```

There is a wrapper: **`run_all_tests.bat [basic|ui|full]`** at the repo root, which activates the
venv, switches to its own directory, and prints the result. Prefer it over calling `pytest`
directly. There is no `sync.bat`, no `webapp/` and no Android project — ignore runbooks that
mention them. The suite is pure Python + PySide6 and needs no build step.

**Current state: full `2544 passed, 1 skipped`; basic `1599 passed, 1 skipped, 945 deselected`.**
The single skip is the opt-in real Windows Defender scan (`MYIDM_RUN_AV_TESTS=1`). Update these
numbers when you add or remove tests, and treat a *sudden* drop as a signal that a module failed
to import.

No randomisation plugin is installed, so test order is deterministic. Passing twice is not
strong evidence; see §6.

### What makes a test "UI"

Tests are marked `ui` **automatically**, from the module list `_INTERACTIVE_MODULES` in
[`tests/conftest.py`](file:///d:/Projects/my-idm/tests/conftest.py) — currently `test_capture`,
`test_delegates`, `test_details_panel`, `test_dialogs`,
`test_entrypoint_and_notifications`, `test_main_window`, `test_settings`, `test_splash`,
`test_ui_tor_and_utils`, `test_views_tab`, `test_youtube_ui`.

The list is declarative and coarse **on purpose**. Marking a whole module `ui` is honest; an
unmarked stray widget is what makes an unattended run disruptive. When you add a UI test module,
add its name to that list — do not decorate each test.

Inside an otherwise-safe module, an individual test can opt in with `@pytest.mark.ui`.

### Why the split exists

Two fixtures were disturbing the desktop on **every** test, ~2400 times per run:

- `isolate_system_clipboard` snapshotted, cleared and restored the **real** OS clipboard.
- `suppress_system_tray_notifications` called `_cleanup_windows_tray_ghosts()`, which walks the
  real `Shell_TrayWnd` posting `WM_MOUSEMOVE` across its toolbar on a 5 px grid — a visible
  taskbar flicker.

Both are now gated on there being `ui` tests in the run at all. See §2 and §3.

### When to run which

- **While working, or on a timer/CI:** basic sanity. Nothing pops up, nothing steals focus.
- **Before committing:** full. UI and tray regressions are invisible to the basic tier by
  construction — a change to `MainWindow` passes basic and fails full, which is the whole point.
- **After touching `MainWindow`, the tray, or anything that builds widgets:** full, regardless
  of tier.

---

## 2. Hermeticity is enforced, not hoped for

[`tests/conftest.py`](file:///d:/Projects/my-idm/tests/conftest.py) installs autouse fixtures
that make the suite refuse to touch the real machine. **A violation is a bug to fix, not
something to work around** — that is the whole point of enforcing it in a fixture rather than
in a review checklist.

| Fixture | Refuses |
| :--- | :--- |
| `block_non_loopback_network` | non-loopback TCP or DNS (`connect`, `connect_ex`, `getaddrinfo`) |
| `block_destructive_subprocess` | `taskkill`, `del`, `rd` and friends; violations are **recorded and reported at session teardown** so a production `except Exception` cannot swallow them |
| `block_desktop_shell_launches` | `os.startfile`, `explorer.exe`, and a local-file `QDesktopServices.openUrl`. Remote URLs stay allowed for the browser tests |
| `isolate_implicit_user_database` | redirects `my_idm.database.DB_PATH` to a temp file, so a bare `Database()` (e.g. `SettingsDialog._get_db()`) can never open the user's live database |
| `isolate_system_clipboard` | non-`ui` tests get a **fake** clipboard patched in-process; the OS clipboard is never read or written. `ui` tests round-trip the real one and restore it |
| `isolate_qsettings_session` / `isolate_qsettings_per_test` | redirects `QSettings` to a temp `IniFormat` directory |
| `suppress_system_tray_notifications` | stubs the tray so tests cannot pop real toasts, and **skips** the Explorer taskbar repaint entirely when the run contains no `ui` tests |

`ALLOW_DESTRUCTIVE_SUBPROCESS` is the only opt-out. A test that sets it **must** say why, or the
session report will flag the test by name.

### The clipboard fixture never touches the OS (except for `ui`)

Two problems it removes, both by *replacing* the clipboard rather than restoring it:

* Production code that prefills a URL field from the clipboard (`YouTubeDialog`) would read
  whatever the developer happened to have copied and fire a real yt-dlp network request.
* The previous snapshot/clear/restore implementation **clobbered the developer's own clipboard**
  on every one of 2400+ tests, which is what made an unattended background run disruptive.

A non-`ui` test now gets a `_FakeClipboard` — a `QObject`, because `ClipboardMonitor` subscribes
to `dataChanged`, so the fake must be one for the connect/disconnect to have production
semantics. It is strictly better than the OS clipboard for this purpose: deterministic, immune to
the clipboard being locked by another application, and incapable of being read or written at all.

Patch `QGuiApplication` rather than `QApplication` when a test needs its own fake — MRO makes
that cover both call sites, whereas patching only the subclass leaves a `QGuiApplication.clipboard()`
call untouched.

### The taskbar repaint is gated

`_cleanup_windows_tray_ghosts()` exists to clear ghost tray icons left by real tray tests, but it
does it by walking the live `Shell_TrayWnd` and posting `WM_MOUSEMOVE` across its toolbar on a
5 px grid — visible flicker on the user's desktop. It now runs only when
`pytest_collection_modifyitems` has seen at least one `ui` test.

### Never clear the user's live settings (CRITICAL)

**NEVER** call `QSettings("MyIDM", "My-IDM").clear()`. On Windows, un-isolated
`QSettings("MyIDM", "My-IDM")` writes the live registry key
`HKEY_CURRENT_USER\Software\MyIDM\My-IDM`, so the user's download folder, UI state and
preferences get wiped on every run. Use isolated settings (what the fixtures give you) or an
explicit `QSettings(temp_file, QSettings.Format.IniFormat)`.

This applies outside the suite too: a debug script that touches real `QSettings` will damage the
developer's install.

---

## 3. Traps that have actually bitten this suite

Each of these is a bug that shipped or a flake that cost real time. The rule is stated so the
next person does not rediscover it.

### Destructive helpers resolve blank paths to the CWD

`quarantine_or_delete_file("")` resolves `Path("")` to `Path(".")` — the process's **current
working directory** — and `shutil.rmtree`s it. Called unguarded from the repo root, it deletes
the repository.

Any test touching such a helper must `os.chdir` into its own temp tree in `setUp` and restore it
in cleanup. Prefer pinning the behaviour with a canary *inside* the temp dir over asserting on a
return value, because the same rule applies to any helper that resolves a blank or relative
path.

### `close()` + `deleteLater()` does not destroy a widget, and the leak is silent

`deleteLater()` posts a `DeferredDelete` event, and `QApplication.processEvents()` does **not**
deliver those by default. So the usual `close() / deleteLater() / processEvents()` idiom
destroys nothing at all. Worse, `MainWindow.closeEvent` *ignores* the close event and hides the
window whenever close-to-tray is on (it is by default), so a bare `close()` is doubly
ineffective.

Use the `_dispose()` helper from `tests/test_views_tab.py::ViewsTabTestCase` (and copy it
wherever you tear down widgets):

```python
try:
    widget.close()
    widget.deleteLater()
except RuntimeError:
    return          # already destroyed by an earlier cleanup
QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
QApplication.processEvents()
```

This is not academic. Ten undelivered `SettingsDialog` objects left **4,212 live widgets and
100 top-level ones**, and `styles.apply_theme` calls `app.setStyleSheet`, which restyles every
live top-level widget — so the leak turned `tests/test_views_tab.py` from 27 seconds into
**13 minutes 49 seconds**, with no error and no warning.

**If a UI test file suddenly gets slow, count `len(QApplication.instance().allWidgets())`
before blaming the code under test.**

### The real OS clipboard makes tests flaky

`QClipboard.setText` is a **silent no-op** when the clipboard is transiently locked — which is
exactly what made `test_copy_multiple_urls_to_clipboard` flaky. That is one of the reasons the
fixture replaces the clipboard rather than snapshotting it; see §2 for the current behaviour and
for why you should patch `QGuiApplication` (not `QApplication`) when a test needs its own fake.

### `time.sleep` is not synchronisation

Never use a sleep, a busy-wait, or a wall-clock budget to wait for something. Use a
`threading.Event` the test controls, `QSignalSpy`, or a bounded pump-until-predicate helper. The
only sleeps allowed are inside a Qt event-loop pump, where the **predicate**, not the sleep, is
the synchronisation.

### No real filesystem outside the temp dir

This includes `~/.my-idm`, `~/Downloads`, the repo tree (a `.xpi` once landed in
`browser_extension/`), and the real Recycle Bin. `tests/test_security.py` gates the one genuine
AV integration test behind `MYIDM_RUN_AV_TESTS=1`.

### Do not remove `_LockedConnection`

`Database` serialises its shared connection behind a reentrant lock, and a closed connection
degrades to inert empty results. Without the lock, concurrent `update_progress` from two threads
**segfaults the interpreter** — see `TestDatabaseThreadSafety` in `tests/test_database.py`.

The documented `test_delete_during_download_releases_lock` shutdown race is fixed on two sides:
`DownloadManager._await_timer_slots()` joins in-flight timer work, and the connection lock covers
the rest. If that flake reappears, find the real cause; do not re-introduce a sleep.

---

## 4. Determinism

### Build UTC instants, expect local days

`Database._now_iso()` writes `added_at` in **UTC**, and the statistics queries re-base it with
SQLite's `localtime` before bucketing into calendar days. A naive
`datetime(...).isoformat()` reads as UTC in SQLite and is then shifted a second time, so it
drifts past midnight on any host far enough from Greenwich — the suite would assert the wrong day
on someone else's machine while passing on the author's.

Convert a chosen *local* moment instead, and compute expectations the same way:

```python
stamp = datetime.combine(day, time(12)).astimezone(timezone.utc).isoformat()
expected = datetime.fromisoformat(stamp).astimezone()
```

Injecting `today` cannot make the SQL itself deterministic across hosts, because the conversion
necessarily reads the host clock. See `TestStatsBucketByLocalDay` in `tests/test_statistics.py`.

The same rule governs anything user-facing that reads a local clock: a "don't run between 09:00
and 17:00" schedule must convert with `astimezone()`, never compare against a UTC stamp.

### MagicMock can disable the code under test

Mocking a `libtorrent` handle with a bare `MagicMock()` silently disables the status path:
`get_status()` raises `TypeError` on `ti.total_size() > 0`, swallows it, and the test passes
without the code ever running. Drive `TorrentEngine.poll_all()` with the explicit `FakeStatus` /
`FakeHandle` shape instead, and use real `TorrentConfig` / `GeneralConfig` objects wherever the
engine compares their values against ints.

### No `QThread` for yt-dlp work

yt-dlp runs on plain daemon threads (`ytdlp-download`, `yt-extract`). Destroying a `QThread`
whose `run()` is still executing aborts the process, and detaching does not help because it is
still destroyed at interpreter exit.

---

## 5. Writing a test that can fail

**A test that cannot fail is worse than no test.**

- **Verify every new regression test by reverting the fix and confirming the test fails.**
  Several tests here passed for the wrong reason. One example: a `GROUP BY` NULL-bucket test
  only discriminates on the **All time** range, where `NULL >= x` is false and there is no
  `WHERE` to exclude it — so it passes against the buggy SQL too.
- **Pin the invariant directly as well as behaviourally** when a fix is timezone- or
  environment-dependent. The UTC-vs-local bucketing bug is invisible on a UTC+00:00 host, so one
  test asserts that `Database._STATS_LOCAL_DAY` still contains `localtime`.
- **A duplicated definition is dead code, not a no-op.** `StatisticsPopup._populate_grid` was
  defined twice and the bodies happened to match, so nothing misbehaved — but that class is
  exactly where a stale copy hides a real difference.
  `TestNoShadowedDefinitions` in `tests/test_infra_hardening.py` AST-walks the touched modules
  and fails on any name redefined in the same scope. A property and its `@name.setter` count as
  one definition and are excluded.
- **Assert the exact call, not the call count.** `assert_any_call(url, path, 8)` breaks the
  moment a parameter is threaded through — which is the point: it told us when
  `add_download` gained `queue_id`.

---

## 6. Known-flaky areas

| Area | Symptom | Status |
| :--- | :--- | :--- |
| ~20 tests call `DownloadManager.start()` | **Intermittent `Windows fatal exception: access violation` or a hang at the end of a full run.** The trace lands in `manager._run_loop` → `asyncio/windows_events.py` `select()`. | **Pre-existing and open.** Reproduced on 2026-10-01 with the queue work excluded: 3 full runs, run 3 crashed. `tests/test_youtube_tool.py` alone is stable across 4 runs, so it needs full-suite context — it is the `idm-async` thread teardown race, not any one test. Not caused by `tests/test_queues.py`. |
| Real Windows Defender scan | `test_security.py` AV tests | Opt-in via `MYIDM_RUN_AV_TESTS=1` |
| Anything touching `os.startfile` / Explorer | would open a window on the host | Refused by fixture; see §2 |

When a full run hangs or crashes with no test failure, check this table before investigating
your own change — and if it does turn out to be yours, say so rather than re-running until green.

---

## 7. Test file conventions

- One module per subsystem, named after it: `tests/test_queues.py`, `tests/test_capture.py`.
- `unittest.TestCase` classes throughout; `pytest` is the runner, not the style.
- A module-level `app = QApplication.instance() or QApplication(sys.argv)` — one shared
  instance, created once.
- `sys.path.insert(0, str(Path(__file__).resolve().parent.parent))` at the top, as the existing
  files do.
- Reuse helpers instead of copying them: `tests/fake_http.py` (`FakeResponse`, `FakeSession`,
  `run_async`), `tests/test_main_window.py::_MainWindowTestCase`, and
  `tests.conftest._FakeClipboard`.
- **A new UI test module must be added to `_INTERACTIVE_MODULES`** in `tests/conftest.py`, or it
  will run in the basic tier and pop windows during an unattended run.
- `pytest-randomly` is **not** installed. Do not add `-p randomly`.

---

## 8. Before you commit

1. `python -m pytest` — **full** tier green. Basic alone is not enough: a `MainWindow` change
   passes basic by construction.
2. Re-run once more if you touched threading, timers or teardown. A single green run is weak
   evidence given §6.
3. If you added or removed tests, update the counts in §1.
4. There is no lint config in this repo (`pyproject.toml` has no `[tool.ruff]`), so no lint step —
   but `python -m compileall` on touched files catches syntax slips cheaply.