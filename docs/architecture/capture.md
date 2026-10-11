# Capture Subsystem (Global Hotkeys & Clipboard Monitoring)

**Implemented 2026-10-01; the hotkeys became lock-key switches on 2026-10-11.**
Covers [`my_idm/hotkey.py`](file:///d:/Projects/my-idm/my_idm/hotkey.py)
and [`my_idm/clipboard_monitor.py`](file:///d:/Projects/my-idm/my_idm/clipboard_monitor.py): the
two ways My-IDM notices a download the user did not hand it explicitly, plus the single
"capture is on/off" switch they share with [browser interception](browser-integration.md).

---

## Overview

Three capture sources feed the same queue:

| Source | Module | Opt-in? |
| --- | --- | --- |
| Browser extension (automatic + right-click) | [`browser_server.py`](file:///d:/Projects/my-idm/my_idm/browser_server.py) | `BrowserIntegrationConfig.enabled` (`config.py:886`) |
| System-wide hotkeys | [`hotkey.py`](file:///d:/Projects/my-idm/my_idm/hotkey.py) | `GeneralConfig.capture_hotkey_enabled`, `GeneralConfig.clipboard_hotkey_enabled` |
| Clipboard | [`clipboard_monitor.py`](file:///d:/Projects/my-idm/my_idm/clipboard_monitor.py) | `GeneralConfig.clipboard_monitor_enabled` |

There are **two hotkey bindings**, one per capture:

| Binding | Controls | Config |
| :--- | :--- | :--- |
| `BINDING_CLIPBOARD` | the clipboard monitor | `clipboard_hotkey_*` |
| `BINDING_CAPTURE` | `intercept_all`, i.e. browser capture | `capture_hotkey_*` |

The clipboard one exists because the two switches are independent: the browser's *Take downloads
from the browser* and the clipboard monitor are separate opt-ins, and a single key that flips both
is not a way to silence the one the user meant. `HotkeyManager.triggered` carries the binding's
name (`main_window.py:_on_capture_hotkey_triggered`), which is why a handler can tell them apart
at all — a bare signal would make both bindings look identical to the slot.

**These are not toggles.** A capture follows its key's *state*: Scroll Lock on means the capture
is on, and off means off. `_on_capture_hotkey_triggered` reads the key and sets the capture to
match (`lock_key_state`), and the reverse holds too — changing a capture from the tray or
Preferences drives the key so its light states what the capture is doing
(`_mirror_capture_lock_key`).

**All three are off by default.** A global hotkey claims a chord system-wide and the clipboard
monitor reads what the user copies; neither is something to switch on behind a user's back.

---

## 1. Global hotkey (`my_idm/hotkey.py`)

### Why a `QAbstractNativeEventFilter`

Windows exposes `user32.RegisterHotKey(hwnd, id, mods, vk)`, which binds a chord to a process
and then posts **`WM_HOTKEY` (0x0312)** to that process' message queue. Qt never surfaces
`WM_HOTKEY` as an event of its own and has no API for the registration, so two things are
needed:

1. `HotkeyRegistration` owns the registration and releases it on demand.
2. `_NativeHotkeyFilter` (a `QAbstractNativeEventFilter`) is installed on the `QApplication`,
   matches `msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID`, and re-emits the press as
   the Qt signal `HotkeyRegistration.triggered`. The filter returns `(True, 0)` so the message
   is consumed and never reaches the rest of Qt.

`PySide6` binds `installNativeEventFilter` as returning **`None`**, so success is "did not
raise"; coercing the return value to `bool` refuses every registration.

**The trap that made this look like a Python/Qt crash.** `ctypes.wintypes` is a *submodule*:
`import ctypes` does not make `ctypes.wintypes` exist, it only appears once some module has
imported it. The filter used to decode with `ctypes.wintypes.MSG`, so in the running app every
native message raised `AttributeError` and the filter returned `(False, 0)` — no hotkey press was
ever recognised, logged as *"module 'ctypes' has no attribute 'wintypes'"* once per message.
The unit tests could not see it, because the test module itself imports `ctypes.wintypes`, which
makes the attribute exist for the whole process; and it "fixed itself" as soon as the user opened
Preferences, because something on that path imports the submodule. The message is now decoded with
a module-level `_MSG` struct, and the regression test *deletes* `ctypes.wintypes` from the
namespace before feeding the filter a real message.

Raw `ctypes` behind `sys.platform == "win32"`, matching every other Windows API call in the app
(`single_instance.py:43`, `details_panel.py:472`). No `pywin32` — it is not in
`requirements.txt`, and a hotkey is not worth a dependency.

### The only keys are the two lock keys

`LOCK_KEYS` is `("ScrollLock", "NumLock")` and `parse_hotkey` resolves nothing else. That is the
rule the rest of the design depends on: a capture follows the key's LED, so the key has to be one
whose state can be read — and a chord has no state to read. A chord is refused with a message
that says which keys *are* usable, rather than one that only says what is wrong.

`lock_key_vk` accepts both spellings ("ScrollLock" and "scroll lock") because Qt writes them
without a space and people type them with one; `canonical_lock_key` normalises to the stored form,
and `LOCK_KEY_LABELS` gives the pretty name the tray and the drop-down show. `QKeySequence` is not
involved at all: the stored string is compared, and `lock_key_vk` maps it to the virtual key.

### The LED is the switch — and a press is not a read

`lock_key_is_on(vk)` reads the low bit of `GetKeyState`, which for the lock keys *is* the toggle
state — the LED and the state are the same thing, not two things to keep in step.

There are two directions, and the press direction is **not** a read:

- **key → capture.** A press *switches* the capture, and `_on_capture_hotkey_triggered` does not
  read the key at all. This is deliberate, and it is the fix for the intermittency that made
  "sometimes a press does nothing" look like a flaky hotkey: Windows delivers `WM_HOTKEY` on the
  key **down** while a lock key's toggle bit flips on the key **up**, so a handler reading
  `GetKeyState` in that handler could only ever see the *previous* press's state — it worked on
  alternate presses and looked broken in between. Two follow-ons make the switch reliable:
  - **no mirror write on a key press.** Windows moved the LED itself, and reading it here gets
    the pre-press state, so mirroring would synthesise a second press and leave the light where
    it started — the switch off while the capture is on.
  - **the sync is held off mid-switch** (`_in_capture_switch`). The capture change re-emits
    synchronously, which lands in `_sync_captures_to_lock_keys` while the LED is still pre-press,
    so without the guard it would "correct" the capture straight back and undo the switch before
    the key-up arrived.
- **capture → key.** `_mirror_capture_lock_key` drives the key when the capture changes from
  anywhere else (tray, Preferences, startup sync), so the LED never lies about the state. It runs
  **before** the change, because `set_capture_enabled` re-emits synchronously and the sync would
  otherwise flip the capture back on a LED that had not moved yet. It also only writes when the
  key differs, which keeps the write a no-op in the common case.

`_sync_captures_to_lock_keys` reads each configured key at startup and after a Preferences save,
so a LED left on from a previous session means the captures it drives are on. It is silent by
design (`announce=False`): the tray row and the menu checkmark already state the result.

### Synthetic input triggers registered hotkeys

`press_lock_key` synthesises a key-down plus a key-up, and Windows does not care that the input
was synthetic — our own chord fires. So `_mirror_capture_lock_key` arms
`_ignore_key_presses_until` (250 ms) whenever it writes, and `_on_capture_hotkey_triggered` returns
early inside that window instead of switching the capture back. A window rather than a counter,
because no one presses the same key twice inside 250 ms while a real press after it must still
work.

### Safety rules

- **Only Scroll Lock and Num Lock are offered.** Anything else — every chord, `CapsLock`,
  `PrintScreen` — is refused rather than registered, because a capture cannot follow a key it
  cannot read the state of. A config written before this (or hand-edited) is *coerced* to the
  default by `config.coerce_capture_key` rather than refused: a stored key that never registers
  looks exactly like a hotkey that stopped working.
- **`ERROR_HOTKEY_ALREADY_REGISTERED` (1409) is reported to the user**, not swallowed: a key
  another application owns must say so instead of leaving a key that does nothing. The message
  names the conflict; `MainWindow._register_capture_hotkeys` surfaces it in the status bar, once
  per binding that failed. This is the one message that stays in the status bar indefinitely,
  because it describes a broken binding rather than an action and nothing has displaced it by the
  time the user looks.
- **A shared key is not reported in the status bar**, only in the log. It is a *setting*, not an
  event: nothing overwrites the status line at startup, so the note would sit there for the whole
  session — and the tray row already names the key on each capture while both Preferences rows say
  the same thing under their drop-down. The status line is for what the user is doing right now.
- **A switch is announced with a toast** (`notifications.notify_capture_switched`) when the state
  actually moves, and only then. This is the one capture event with no other sign at the moment it
  happens: a capture key press changes a setting with the window closed, where a status line and a
  tray checkmark are both unreadable, and the LED shows the key but not *which capture* moved. The
  toast names both.
  - the **startup/Preferences key sync is silent** (`announce=False`). A LED left on means the
    capture is on, which is a setting rather than an event, and the tray row already states it by
    the time the user looks — a toast on every launch trains the user to ignore toasts.
  - a press that agrees with the capture is not a change and says nothing.
  - a key shared by both captures notifies once per capture, because one LED can be driving two
    settings and the user needs to know what changed.

### Two bindings, two ids

`HotkeyManager` (`hotkey.py:365`) owns one `HotkeyRegistration` per binding, and each registers
its own process-private id (`HOTKEY_ID`, `CLIPBOARD_HOTKEY_ID`). That is not cosmetic: the id is
what `RegisterHotKey` echoes back in `WM_HOTKEY`, so two bindings sharing one id would make every
press look like the same one and one of them would silently move the wrong switch. Each
registration installs its own native filter and dispatches on its own `wParam`.

`register()` short-circuits when the requested key is already the one held, so a Preferences
save does not drop and re-claim the binding. A *different* key unregisters first, because
leaving the old one claimed would make it unusable system-wide forever.

### One key can drive both captures

Win32 refuses a second `RegisterHotKey` of an **identical** `fsModifiers`/`vk` pair for the
calling thread with `ERROR_HOTKEY_ALREADY_REGISTERED` — so the obvious setting, "Scroll Lock for
browser capture *and* Scroll Lock for clipboard capture", used to produce one working hotkey and
one silent failure that blamed another application for owning the key.

`HotkeyManager` now keeps a `(fsModifiers, vk) -> {binding names}` map. The first binding owns the
Win32 registration; any other binding asked to register the same key is added to that group and
dispatched on every press of the owner. Turning one off hands the registration to a survivor
rather than dropping it. `bindings_for(sequence)` reports the whole group, and
`is_binding_registered(key)` answers "is this binding live" for a sharer as well as for an owner —
which is why the tray label reads it and not `HotkeyRegistration.is_registered`.

A shared key makes both captures follow it together, which is the only reading of that setting
that is not a lie: one LED is the switch for both.

### Lifetime

`MainWindow.closeEvent` calls `_release_capture()` (`main_window.py:2119`), which calls
`HotkeyManager.unregister_all()`. **This is not optional**: Windows keeps a chord bound to the
process id that claimed it, so a hotkey left registered outlives the app and the next launch then
fails to claim it with nothing for the user to see.

### The two capture rows in the tray

`_TRAY_CAPTURE_LABELS` holds the base text and `_tray_capture_label()` appends the bound key, so
the row reads `🎯 Download Capture    Ctrl+Alt+D` and `📋 Clipboard Capture    NumLock`
(`_refresh_tray_capture_labels`, called from `_sync_capture_actions`). Three rules keep the suffix
from lying:

- an unconfigured hotkey adds **nothing**, so a user who never wanted one is not shown one;
- a chord that failed to register is still shown but marked `⚠`, because "which key *should* have
  worked" is the first step to fixing it;
- the label is rebuilt from the base text on every sync, never appended to, so it cannot grow.

### Known limit

The extension re-reads `GET /config` on a **30 s** timer (`background.js:121`), so
browser-*native* interception converges within that window after a toggle. Anything sent to the
app is declined immediately, because `BrowserServer._handle_add` reads the same config object.
The extension-side `chrome.commands` block — the cross-platform half — is not implemented: it
would be overwritten by that same sync on every tick unless the app-side state agreed, so it
adds little while the hotkey is the primary mechanism.

---

## 2. Clipboard monitoring (`my_idm/clipboard_monitor.py`)

`ClipboardMonitor` subscribes to `QClipboard.dataChanged`, debounces, and hands captureable text
to `DownloadManager.add_download()`.

### Debounce

A **single-shot** `QTimer` restarted on every change, so a burst of clipboard writes coalesces
into one read rather than queueing N of them. 500 ms by default. `capture_now()` is the timer's
slot *and* the seam tests drive, so no event loop and no real clipboard round-trip is needed.

### The all-or-nothing gate

A copy is accepted **only when every non-blank line is a downloadable URL**. A chat message that
happens to contain one link must not be captured, and neither must the 200-line log tail someone
just copied — otherwise the monitor fires on a keystroke and the user stops trusting it.

This is the policy `AddDownloadDialog._prefill_url` already used. Both now call one
`looks_like_download_url()`, so the pre-fill gate and the auto-capture gate **cannot drift**: the
same string must be offered in the dialog and captured by the monitor, or neither.

`blob:` and `data:` are rejected for the same reason
[`browser-integration.md`](browser-integration.md) rejects them — a browser-internal object
reference has no external transport, so queuing one only makes the user wait out the retry ladder.

### Self-write suppression

`MainWindow._on_copy_url` puts selected rows' URLs on the clipboard. Re-reading that would
re-add them, and **deduping inside `add_download` is not enough**: re-adding a *paused* download
calls `resume_download` on it, so Ctrl+C on a paused row would silently start it.

`ClipboardMonitor.suppress(text)` is therefore called by the writer, and the text is ignored on
the next `dataChanged` that carries it — consumed by that one change, so a subsequent genuine
copy of the same URL captures normally. The queue is capped (`_MAX_SUPPRESSED_TEXTS`) because
every entry is meant to be consumed by one change, and a writer that never produces a matching
change would otherwise grow it without bound.

`SettingsDialog`'s own clipboard writes (copying an ID, a path, a file extension) need no
suppression: none of them is a URL, so the gate rejects them.

### Resolve before capture

A copied URL is text, and text does not say what it points at. `https://example.com/` and
`https://example.com/ubuntu.iso` are the same shape until somebody asks the server — so a link
copied out of a browser address bar used to land in the table as a download that failed, and the
user had to find the row and delete it.

Each candidate is therefore resolved by `my_idm/http_probe.probe_url` **before** `add_download`:

1. one `HEAD` with redirects followed;
2. only if that produced no usable size, one single-byte ranged `GET` (`bytes=0-0`), which reads
   the total out of `Content-Range` for the many origins that answer `HEAD` with 405 or omit
   `Content-Length`.

The probe is what makes the **size** and **extension** settings mean anything: neither can be
evaluated from a URL that redirects to a named file, and a URL with no extension has no extension
to ignore. It also hands `add_download` the resolved filename from `Content-Disposition`, so a row
appears under its real name rather than nameless until the engine's own probe lands.

`decide_capture(url, probe, …)` then rejects, in order:

| Reason | Condition |
| :--- | :--- |
| `REASON_UNREACHABLE` | `ok=False` — transport failure, timeout, or any status ≥ 400 |
| `REASON_HTML_PAGE` | the server answered `text/html` |
| `REASON_IGNORED_EXT` | the copied URL **or** the resolved filename matches the ignore list |
| `REASON_TOO_SMALL` | `0 < size < min_file_size_kb` |

Two deliberate edges:

- **`size == 0` means *unknown*, not *empty*** (chunked, or a HEAD the origin refused). It never
  fails the size check — refusing every unsizeable response would reject every gzip-encoded
  download there is. `ok` is the separate question of whether the server answered at all.
- **The engine still probes authoritatively** and still receives `pending_min_bytes`, so a URL
  that shrank between the two probes is refused there too. The pre-capture probe is a filter, not
  a substitute for the download's own size check.

An unresolvable URL is **dropped, not queued**. That is the point: a row the user has to delete is
worse than no row. The cost is that a capture whose host is briefly unreachable is lost rather
than retried — Add Download is not gated at all, so it remains the escape hatch.

Probes run concurrently, capped at `MAX_CONCURRENT_PROBES` (4): a pasted list of 20 URLs must not
take 20 × the probe timeout, nor become a rate-limit target. The whole resolve-and-add step is one
coroutine run through the injected `run_async` — `DownloadManager.run_coro_threadsafe`, i.e. the
manager's background loop — because the clipboard read has to happen on the GUI thread but the
HTTP work must not block it.

`magnet:`, `ftp:` and a local `.torrent` are **not probed**: there is no server to ask and no size
to weigh, so a probe could only report failure.

### Limits and reporting

- Per-copy ceiling, configurable (`clipboard_monitor_max_urls`, default 20) with a hard
  `ABSOLUTE_MAX_URLS` of 200, so a pasted generated list cannot become thousands of rows at once.
- The length cap is **per line** (4096, inside `looks_like_download_url`), deliberately not on the
  whole payload — a cap on the payload would refuse a legitimate list of fifty links, which is
  exactly the case `max_urls` exists to handle.
- `urls_captured(list, int)` reports what was added and how many lines the limit dropped, so
  capture is never silent. The count is measured against the cap, not the returned list: a
  duplicate line was collapsed, not skipped, and reporting it as "over the limit" would be a lie.
- `urls_filtered(list)` reports each `[(url, reason)]` the probe rejected, **including when
  nothing was captured**. Silence here is the failure mode that made the feature untrustworthy in
  the first place: the user copies a link, nothing appears, and there is no way to tell a
  deliberate skip from a broken monitor. `MainWindow._on_clipboard_urls_filtered` puts the reason
  in the status bar.
- `add_download` returns `None` for three different outcomes (empty URL, security-blocked,
  already-completed duplicate), so only a real id counts as "added".

### Failure containment

`start()` is inert when `QGuiApplication.clipboard()` returns `None` (it can, legitimately).
`capture_now()` survives a clipboard that raises on read, and one bad URL does not abort the
rest of a batch. A probe that raises is treated as unreachable rather than aborting the batch, so
one bad host cannot cost the other URLs. `capture_now()` also wraps `run_async`: an exception
there would escape a `QTimer` slot, and Qt would drop the connection — leaving the monitor
silently never firing again. `stop()` never raises — it runs from `closeEvent`, where an exception
would abort teardown.

### It is never silent

A captured download appears on its own with nothing else to explain it, which is the failure
mode worth designing against: a user who does not realise that is what happened has no way to
connect the new row to the thing they copied. So a capture fires a **Windows toast** —
`notifications.notify_clipboard_download_captured`, titled **"Captured from Clipboard"** — naming
the source, alongside the transient status-bar line. The browser capture has always notified
(`notify_browser_download_caught`) for the same reason.

A batch of one is named (`Added: a.zip`); a batch of many is counted (`Added 7 downloads`),
because a wall of filenames is not actionable. Lines dropped by the per-copy limit are reported
in the toast as well as the status bar.

The notification is deliberately **not** routed through the "notify on completion" preference:
that one is about a download finishing, and reusing it would make a capture toast suppressible
by a setting the user has no reason to think is related.

---

## 3. `intercept_all` is now a hard app-side gate

`BrowserServer._handle_add` (`browser_server.py:230`) now declines when `intercept_all` is off:

```json
{ "status": "ignored", "reason": "capture_paused",
  "message": "Download capture is paused in My-IDM." }
```

`200 OK` with `ignored`, **not** an error status, and that shape is deliberate: the extension
reads a non-ok status as "My-IDM is broken" and falls back to a browser download, whereas
`ignored` is the shape it already understands for "handled, not queued" (see
`browser-integration.md` for the sibling `unsupported_url_scheme` and `file_size_below_minimum`
declines).

This makes `intercept_all` authoritative app-side, which it previously was not — it was only a
hint delivered to the extension. That is what lets the hotkey take effect immediately instead of
after the extension's 30 s sync. The Preferences label was changed to match
("Take downloads from the browser") because "automatically intercept" no longer described it.

The gate is **after** the `enabled` check (which keeps its own `403`) and **before** body
parsing, the scheme allow-list and the size probe — a paused capture must cost no work.

---

## 4. Configuration

| Field | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `capture_hotkey_enabled` | `bool` | `False` | Binds the download-capture key system-wide. |
| `capture_hotkey_sequence` | `str` | `"ScrollLock"` | The key. One of `LOCK_KEYS`; coerced to the default otherwise. |
| `clipboard_hotkey_enabled` | `bool` | `False` | Binds the clipboard-capture key system-wide. |
| `clipboard_hotkey_sequence` | `str` | `"ScrollLock"` | The key. Same two choices. |
| `clipboard_monitor_enabled` | `bool` | `False` | Watches the clipboard. |
| `clipboard_monitor_max_urls` | `int` | `20` | Ceiling on one copy event. |
| `clipboard_min_file_size_kb` | `int` | `1024` | Minimum capture size, applied to the **resolved** file. `0` disables. |
| `clipboard_ignored_extensions` | `list[str]` | `.txt .htm .html .jpg .jpeg .png .gif .webp` | Applied to both the copied URL and the resolved filename. |

Both keys default to `"ScrollLock"`, and both pointing at the **same** key is a supported setting
rather than a conflict: Win32 allows one registration per key per thread, so a shared key is
registered once and dispatched to every purpose bound to it — which is the same thing as saying
one key controls both captures. See *One key can drive both captures* above.

A sequence that is not one of the two keys is coerced to `ScrollLock` rather than rejected: a
stored key that never registers is indistinguishable from a hotkey that stopped working.
`clipboard_monitor_max_urls` is clamped to `>= 1` so a corrupt value cannot disable capture.

---

## 5. UI surfaces

Each row is built by `SettingsDialog._add_capture_hotkey_row()` — one method, because the two
tabs need the identical control and the rules have to be the same on each:

- **Preferences → 📋 Clipboard Capture**: the clipboard row (checkbox + key drop-down + a status
  label), sitting with the clipboard settings it controls.
- **Preferences → 🌐 Browser Integration**: the download-capture row, directly under
  **Take downloads from the browser** — the same switch, bound to a key.

The chooser is a **`QComboBox` with exactly two entries**, not a `QKeySequenceEdit`. Not a
simplified editor: only Scroll Lock and Num Lock can drive a capture, so those are the only
choices that mean anything — and an editor would imply a chord can be bound, which it cannot.

Nothing can be invalid, so the status label is advice rather than validation: `Scroll Lock on
turns this capture on, off turns it off — the key's own light shows the state`, and
`drives both` when both rows hold the same key. Telling the user their selection is invalid when
the only alternative was never offered is noise. The rows report on their own status label
(`_capture_hotkey_status_lbl_for`), so editing one never overwrites the other's advice.

- **Tray**: `🎯 Download Capture` and `📋 Clipboard Capture`, both checkable so a row shows live
  state rather than describing an action. These are the menu's only checkable entries; the rest
  mutate text in `_update_tray_menu_text` because their state is not a simple on/off.
  `_sync_capture_actions()` mirrors the real state onto them with `blockSignals`, called from the
  key, the tray and both config-changed signals, so the menu can never disagree with what
  capture is doing. Each row also shows its bound key
  (`🎯 Download Capture    Scroll Lock`) — the only place the user can be told how to silence a
  capture without opening Preferences — and `⚠` marks a key that could not be claimed.

Both take effect from a Preferences save without a restart, via `MainWindow`'s
`general_config_changed` and `browser_config_changed` connections.

---

## Related documents

- [Browser Integration](browser-integration.md) — the extension, the loopback REST server, and
  the sibling decline reasons.
- [Window Lifecycle & System Tray](window-system-tray.md) — the tray menu and teardown order.
- [Database & Persistence](database.md) — the config classes and their four-mirror serialization.
- [`tests/test_capture.py`](file:///d:/Projects/my-idm/tests/test_capture.py) — 158 tests covering
  the URL gate, debounce, suppression, lock-key parsing (lock keys only, both spellings, every
  chord refused), `GetKeyState`/`keybd_event` including the read-first that stops a write from
  toggling twice, **the press-switch behaviour with the LED deliberately stale** (which is the
  case the old read model got wrong), the echo guard for our own synthetic press, the sync hold
  during a switch, both directions of the key-to-capture relationship, registration, the
  two-binding dispatch, the shared-key group (one registration, both captures, hand-off on
  unregister), the `WM_HOTKEY` filter, the tray rows and their key labels, and config
  coercion. The `WM_HOTKEY` path is tested with a real `ctypes.wintypes.MSG` and a faked
  `user32`, so the ctypes boundary is genuinely exercised. The `TestHotkeyManager` class
  installs its fake for the whole case rather than per test: a registration that fails with 1409
  because the developer's own running My-IDM owns that key is indistinguishable from a real bug.
- [`tests/test_settings.py`](file:///d:/Projects/my-idm/tests/test_settings.py) — the two
  capture rows: seeding, save, the two-entry drop-down, greyed-out choosers and per-row advice.