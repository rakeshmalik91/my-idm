# Capture Subsystem (Global Hotkey & Clipboard Monitoring)

**Implemented 2026-10-01.** Covers [`my_idm/hotkey.py`](file:///d:/Projects/my-idm/my_idm/hotkey.py)
and [`my_idm/clipboard_monitor.py`](file:///d:/Projects/my-idm/my_idm/clipboard_monitor.py): the
two ways My-IDM notices a download the user did not hand it explicitly, plus the single
"capture is on/off" switch they share with [browser interception](browser-integration.md).

---

## Overview

Three capture sources feed the same queue:

| Source | Module | Opt-in? |
| --- | --- | --- |
| Browser extension (automatic + right-click) | [`browser_server.py`](file:///d:/Projects/my-idm/my_idm/browser_server.py) | `BrowserIntegrationConfig.enabled` (`config.py:773`) |
| System-wide hotkey | [`hotkey.py`](file:///d:/Projects/my-idm/my_idm/hotkey.py) | `GeneralConfig.capture_hotkey_enabled` |
| Clipboard | [`clipboard_monitor.py`](file:///d:/Projects/my-idm/my_idm/clipboard_monitor.py) | `GeneralConfig.clipboard_monitor_enabled` |

The hotkey and the tray's **🎯 Download Capture** row both call
`DownloadManager.set_capture_enabled()` (`manager.py:1252`), which flips
`BrowserIntegrationConfig.intercept_all`. The clipboard monitor is a separate switch.

**Both are off by default.** A global hotkey claims a chord system-wide and the clipboard
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

Raw `ctypes` behind `sys.platform == "win32"`, matching every other Windows API call in the app
(`single_instance.py:43`, `details_panel.py:472`). No `pywin32` — it is not in
`requirements.txt`, and a hotkey is not worth a dependency.

### Parsing works on the string, not on `QKeySequence`

`parse_hotkey(sequence)` deliberately **does not** use `QKeySequence`:

| Problem | Evidence |
| --- | --- |
| Qt drops a Meta/Win chord entirely | `QKeySequence("Win+D").isEmpty()` is `True`; `combo.key()` is `Qt.Key_unknown` (`0x01FFFFFF`) |
| F-keys and arrows live in a private range | `Qt.Key_F4` is `0x01000043`, not the Win32 `VK_F4` of `0x73` |
| Only `Meta` survives round-tripping | `QKeySequence("Windows+D").toString()` is `""` |

The stored string is what the user typed and what `QKeySequenceEdit` hands back, so it is parsed
directly: `_SEQUENCE_RE` splits a leading modifier run from the key, `_NAMED_VK` maps named keys
to Win32 codes, and printable characters go through **`VkKeyScanW`** so the user's own keyboard
layout decides the virtual key.

### The shift trap

`VkKeyScanW('D')` returns `0x0144` — "virtual key `0x44`, and it needs Shift" — because `D` is the
character `D`. Using the `QKeySequence` key code (`Qt.Key_D` == `0x44` == `'D'`) would silently
turn `Ctrl+Alt+D` into `Ctrl+Alt+Shift+D`. So ASCII letters are looked up in **lower case**
(`_printable_key_to_vk`); anything else keeps the form the user typed, because a symbol like `!`
genuinely does need the shift `VkKeyScanW` reports for it.

### Safety rules

- **A chord with no Ctrl, Alt or Win is refused**, not registered (`_REQUIRED_MODIFIERS`).
  `RegisterHotKey` accepts a bare letter, and a bare letter claimed system-wide would swallow
  that key in every other application on the desktop. A global hotkey that breaks the user's
  editor is worse than no hotkey.
- **Multi-step sequences are refused** (`"Ctrl+K, Ctrl+B"` has no single Win32 chord).
- **Unknown keys and modifiers are refused** rather than guessed at.
- **`ERROR_HOTKEY_ALREADY_REGISTERED` (1409) is reported to the user**, not swallowed: a chord
  another application owns must say so instead of leaving a key that does nothing. The message
  names the conflict; `_show_in_status_bar` surfaces it via `MainWindow._register_hotkey`.

### Lifetime

`register()` short-circuits when the requested chord is already the one held, so a Preferences
save does not drop and re-claim the binding. A *different* chord unregisters first, because
leaving the old one claimed would make it unusable system-wide forever.

`MainWindow.closeEvent` calls `_release_capture()` (`main_window.py:1382`), which unregisters.
**This is not optional**: Windows keeps a chord bound to the process id that claimed it, so a
hotkey left registered outlives the app and the next launch then fails to claim it with nothing
for the user to see.

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

### Limits and reporting

- Per-copy ceiling, configurable (`clipboard_monitor_max_urls`, default 20) with a hard
  `ABSOLUTE_MAX_URLS` of 200, so a pasted generated list cannot become thousands of rows at once.
- The length cap is **per line** (4096, inside `looks_like_download_url`), deliberately not on the
  whole payload — a cap on the payload would refuse a legitimate list of fifty links, which is
  exactly the case `max_urls` exists to handle.
- `urls_captured(list, int)` reports what was added and how many lines the limit dropped, so
  capture is never silent. The count is measured against the cap, not the returned list: a
  duplicate line was collapsed, not skipped, and reporting it as "over the limit" would be a lie.
- `add_download` returns `None` for three different outcomes (empty URL, security-blocked,
  already-completed duplicate), so only a real id counts as "added".

### Failure containment

`start()` is inert when `QGuiApplication.clipboard()` returns `None` (it can, legitimately).
`capture_now()` survives a clipboard that raises on read, and one bad URL does not abort the
rest of a batch. `stop()` never raises — it runs from `closeEvent`, where an exception would
abort teardown.

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

`BrowserServer._handle_add` (`browser_server.py:158`) now declines when `intercept_all` is off:

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
| `capture_hotkey_enabled` | `bool` | `False` | Binds the chord system-wide. |
| `capture_hotkey_sequence` | `str` | `"Ctrl+Alt+D"` | The chord. Validated by `parse_hotkey` on save and again at registration. |
| `clipboard_monitor_enabled` | `bool` | `False` | Watches the clipboard. |
| `clipboard_monitor_max_urls` | `int` | `20` | Ceiling on one copy event. |

`capture_hotkey_sequence` falls back to the default when empty (a stored empty string would
otherwise disable the hotkey silently), and `clipboard_monitor_max_urls` is clamped to `>= 1` so
a corrupt value cannot disable capture.

---

## 5. UI surfaces

- **Preferences → General & Downloads**, a **Capture** group: the two checkboxes, the per-copy
  spin box, and a `QKeySequenceEdit` (the first in this codebase). Validation is **inline** — a
  red status label under the field, not a modal — because `QKeySequenceEdit` commits on every
  `editingFinished` and a `QMessageBox` per edit would be hostile. It calls the same
  `parse_hotkey()` the runtime registration uses, so the dialog cannot accept a chord the hotkey
  layer will later refuse.
- **Tray**: `🎯 Download Capture` and `📋 Clipboard Capture`, both checkable so a row shows live
  state rather than describing an action. These are the menu's only checkable entries; the rest
  mutate text in `_update_tray_menu_text` because their state is not a simple on/off.
  `_sync_capture_actions()` mirrors the real state onto them with `blockSignals`, called from the
  hotkey, the tray and both config-changed signals, so the menu can never disagree with what
  capture is doing.

Both toggles take effect from a Preferences save without a restart, via `MainWindow`'s
`general_config_changed` and `browser_config_changed` connections.

---

## Related documents

- [Browser Integration](browser-integration.md) — the extension, the loopback REST server, and
  the sibling decline reasons.
- [Window Lifecycle & System Tray](window-system-tray.md) — the tray menu and teardown order.
- [Database & Persistence](database.md) — the config classes and their four-mirror serialization.
- [`tests/test_capture.py`](file:///d:/Projects/my-idm/tests/test_capture.py) — 84 tests covering
  the URL gate, debounce, suppression, chord parsing, registration, the `WM_HOTKEY` filter, the
  tray wiring, and config round-trips. The `WM_HOTKEY` path is tested with a real
  `ctypes.wintypes.MSG` and a faked `user32`, so the ctypes boundary is genuinely exercised.