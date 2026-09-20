# Agent Guidelines & Repository Architecture

This file provides context and navigation for AI agents working in the **My-IDM** repository.

---

## 🏛️ Architecture Documentation

The canonical architecture documentation is organized under [`docs/architecture/`](file:///d:/Projects/my-idm/docs/architecture):

| Document | File | Description |
| :--- | :--- | :--- |
| **System Architecture** | [`docs/architecture/main.md`](file:///d:/Projects/my-idm/docs/architecture/main.md) | High-level system architecture, engine threading model (asyncio + libtorrent + Qt), SQLite database schema, and component workflows. |
| **Tor Network Privacy** | [`docs/architecture/tor.md`](file:///d:/Projects/my-idm/docs/architecture/tor.md) | Tor SOCKS5 proxy routing, `TorServiceManager` background lifecycle, executable auto-discovery, startup gating, and exit termination. |
| **VPN & Kill Switch** | [`docs/architecture/vpn.md`](file:///d:/Projects/my-idm/docs/architecture/vpn.md) | Network adapter interface binding, live adapter monitoring, instant kill switch loop, and HTTP/SOCKS5 proxy support. |
| **Antivirus & Security** | [`docs/architecture/antivirus.md`](file:///d:/Projects/my-idm/docs/architecture/antivirus.md) | Pre-download dangerous format warnings, double-extension inspection, post-download Windows Defender / custom scanning, and quarantine. |
| **BitTorrent Engine** | [`docs/architecture/torrent.md`](file:///d:/Projects/my-idm/docs/architecture/torrent.md) | `libtorrent` session management, magnet URI / `.torrent` file handling, file priorities, swarm/peer/tracker tracking, and fastresume. |
| **Backlog Processing** | [`docs/architecture/backlog.md`](file:///d:/Projects/my-idm/docs/architecture/backlog.md) | Multi-location auto-discovery, custom download location delimiters & directives, auto-clearing queue lifecycle, and IPC ingestion. |

---

## 📚 General Documentation & References

- **[User Guide](file:///d:/Projects/my-idm/docs/user-guide.md)** — Comprehensive user manual covering installation, GUI navigation, settings, and workflows.
- **[API Reference](file:///d:/Projects/my-idm/docs/api-reference.md)** — Detailed API reference for engines, database schema, manager signals, and delegates.
- **[TODO & Roadmap](file:///d:/Projects/my-idm/docs/TODO.md)** — Active development backlog, feature checklist, and tracked bug fixes.
- **[Project README](file:///d:/Projects/my-idm/README.md)** — Project overview, feature summary, screenshots, and setup instructions.

---

## 🛠️ Key Conventions for Agents

1. **Commit Convention**: Follow standard conventional commit prefixes (`feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `chore:`).
2. **Testing**: Always run `python -m pytest` and ensure 100% of unit tests pass before committing.
3. **Threading Architecture**:
   - Qt GUI runs on the main thread.
   - `HTTPEngine` uses an asyncio event loop running on a dedicated background thread (`idm-async`).
   - `TorrentEngine` uses `libtorrent` session managed via periodic Qt timer polls (`_poll_torrents`).
   - Background scans (antivirus) run on short-lived daemon threads (`scan-<id>`).
4. **Preserve User Settings & Environment (CRITICAL)**:
   - **NEVER** clear, wipe, or overwrite the user's live settings (`QSettings("MyIDM", "My-IDM").clear()`) during testing, debugging, or local development.
   - On Windows, un-isolated `QSettings("MyIDM", "My-IDM")` modifies the live Windows Registry (`HKEY_CURRENT_USER\Software\MyIDM\My-IDM`), which causes the user's default download folder, UI state, and configured preferences to be wiped repeatedly.
   - All tests must use isolated settings (enforced automatically in [`tests/conftest.py`](file:///d:/Projects/my-idm/tests/conftest.py) via a temporary `IniFormat` directory) or explicit temporary files (`QSettings(temp_file, QSettings.Format.IniFormat)`).
   - Never call `.clear()` on production registry settings.
5. **No Automatic Git Push (CRITICAL)**:
   - **NEVER** push automatically (`git push`).
   - Only commit changes locally (`git commit`).
   - Pushing to the remote repository must only be done if explicitly instructed by the user.

