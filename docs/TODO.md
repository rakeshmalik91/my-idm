# TODO list
---------------------
Pick feature, enhancements, fixes from here.
commit after each features or set of fixes/enhancements
---------------------

### CRITICAL BUG
- [x] had 3 torrents, 1 completed, 2 downloading, everytime i restart the app, one of the downloading torrent gets replaced by the completed one 
- [x] download going on even in paused state sometimes
- [x] to check why animepahe downloads are going to queued state and not retrying, manual resume works fine
- [x] BUG: My-IDM freezes in Windows system tray when AnimePahe background scraper triggers Cloudflare bypass
  - [x] Do not pass `container_hwnd` (`ANIMEPAHE_EMBED_CONTAINER_HWND`) if My-IDM is minimized or hidden in system tray (run detached/headless in background)
  - [x] Switch `_animepahe_queue_lock` in `DownloadManager` from `threading.Lock()` to `threading.RLock()` to avoid re-entrancy deadlocks
  - [x] Dispatch `self.process_backlogs()` from `_monitor()` daemon thread safely to the Qt main event loop via `QTimer.singleShot(0, ...)` instead of synchronous DB/signal execution on background thread
  - [x] Prevent double-docking race in `DetailsPanel._browser_monitor_timer` when window is already reparented by CLI, and avoid expanding details panel while in tray
  - [x] In `animepahe-downloader` (`browser_embed.py`): restore window parent (`user32.SetParent(chrome_hwnd, 0)`) before closing Chrome driver

----------------------
### Torrent
- [x] Torrent in "fetching metadata" state for more than 1 (configurable) day should go to Suspended state. Suspended state shouldnt be considered active, shouldnt have an order. timer calculation should be regardless of app restart. Manual resume or successful progress should reset the timer.
- [x] for completed torrents, in details panel, files are showing Pending status
- [x] for downloading torrents, segments in details panel were showing Pending status; mark them Downloading, and Paused when interrupted
- [x] double clicking a file in details panel should open it
- [x] max parallel download limit not working, ones that are limited should be in queued state and not downloading. queued ones should automatically move to downloading when another one finishes/paused/stopped/deleted etc or limit is increased. verify ordering, last added download should be processed last. order 1 is for high priority, higher number for lower priority. downloads sometimes progressing even in paused state.
- [x] after successful download torrent should go to seeding state. make max seeding speed and download to seeedin g speed ration configurable in preferences
- [x] seperate torrent specific preferences to seperate tab in preferences window
- [x] store seeders, trackers, file hierarchy details with progress details in db & handle them in details panel
- [x] setting a downloaded file to "Do not download" or unchecking it should ask for confirmation and move it to trash. deleting a folder or multiple files shouldnt trigger multiple confirmations. unlock the file before delete if required.
- [x] make how long to seed configurable
- [x] resume seeding downloads in seeding status on startup. (make it configurable)
- [x] add start seeding button in context menu
- [x] move failed while seeding. but it moved partially, handle it properly. unlock the file before move/delete if required.
- [x] pause/stop at seeding state should move to completed. pause/stop at completed shouldnt do anything. update state diagrams as well
- [x] when a new torrent is added, after metadata fetch do a recheck first just in case the torrent file already exists. (update state digrams)
- [x] store total seeded bytes and show in details tab

----------------------
### Tor Support
- [ ] UNVERIFIED - add support for activating tor, add as a button in toolbar. 
  - [x] make what to run inside tor configurable in settings, usual download or torrent etc. 
  - [x] make whether to activate tor at startup or not configurable too. configurable
  - [x] when exiting with tor on, stop it and exit
  - [x] dont start with tor unless that settings is on
  - [x] fix progressbar glitch when tor is turned on/off during download
  - [x] while connecting or disconnecting tor, show a progressbar on both toolbar and footer tor buttons. when ON make the buttons green.
  - [x] before tor start/stop pause all ongoing downloads, and resume on tor stop/start. ensure switching works seemslessly and doesnt mess up downloads data.
  - [x] to verify what happens if another instance of tor or tor browser running in parallel
  - [x] route an individual download through tor from the row context menu, enabled only when tor is running
  - [x] cache tor availability and probe off the GUI thread (it was blocking ~1s on every menu open and row repaint)

----------------------
### Bandwidth control
- [x] Add support for bandwidth allocation of downloads and files in torrent downloads (Low - 25%, Medium - 50%, High - 75%, Max - 100%)
- [x] Add support total Up/Down Bandwidth limit in footer speed context menu (1kbps, 2kbps, 5kbps, 10kbps, 50kbps, 100kbps, 200kbps, 500kbps, 1mbps, 2mbps, 5mbps, 10mbps, 100 mbps, Unlimited, Custom)
  
----------------------
### Malware Scan
- [x] BUG: test antivirus button in preferences window failing: tuple object has no object is_clean
- [x] control what type of virus/malware to scan (for example ignore win/CrackTool, HackTool etc that are mostly harmless and commonly found on torrents). 
- [x] control when to scan, before download start, after completes etc
- [x] control what kind of malware to delete/quarrantine

----------------------
### Browser Integration

#### Chrome
- [x] chrome integration
- [x] allow .torrent files to be caught from browser and add it as a torrent, have seperate config to control it
- [x] add config to control min fixed size to catch
- [x] add windows notification when a file is caught from browser extension
- [x] config values should be shared between extension & my-idm ui
- [x] capture clicking on magnet URL from browser, have this as a config on both extension and my-idm ui

#### Mozilla
- [ ] UNVERIFIED - mozilla integration

#### Edge
- [ ] UNVERIFIED - edge integration

----------------------
### Tools

#### Animepahe scraper
  - [x] add a new tab for external tools in preferences, add any related configs
    - [x] repo location
    - [x] toggle to launch animepahe scraper at launch, in cli mode, should send downloads to myidm in backlog file
    - [x] periodic background scraper schedule (configurable toggle, default: every 6 hours)
    - [x] buttons to view console logs (redirect them in a file, open that file) & debug logs (should be there alreaady, open the file)
  - [x] add a button in tools menu to launch animepahe downloader gui
  - [x] show footer badge while background CLI scraper is active (status popup for logs, stop/start, GUI, settings)
  - [x] active console log bottom panel (stream logs in real-time when clicking console log from footer while animepahe cli is active)
    - [x] group the output into collapsible sessions, one per scraper run, titled by start timestamp; all collapsed except the newest
  - [x] download new anime from preferences window > animepahe, using url & optional episode range

#### Youtube scraper ([Architecture Doc](architecture/youtube-scraper.md))

**Phase 1: Foundation**
- [x] Add `ytdlp_*` fields to `ExternalToolsConfig` (config, serialization, unit tests)
- [x] Create `youtube_tool.py` — dataclasses, availability checks, URL detection regex
- [x] Implement `extract_metadata()` — call `yt_dlp.extract_info(download=False)`, parse formats
- [x] Implement `resolve_direct_url()` for Mode A (direct CDN URL extraction)

**Phase 2: Mode A — HTTPEngine Integration**
- [x] Add `add_youtube_download()` to `DownloadManager` (resolve URL → standard HTTP download)
- [x] Handle URL expiration — re-extract on resume via `_refresh_youtube_url()`

**Phase 3: Mode B — yt-dlp Native Download**
- [x] Implement `start_native_download()` — daemon thread + progress hooks + postprocessor hooks
- [x] Add Mode B tracking to `DownloadManager` (progress relay, completion, cancellation)
- [x] Control the output filename so the DB path matches the file yt-dlp writes
- [x] Locate the produced file by stem (ignores `.part` / `.fNNN` merge fragments)
- [x] Self-heal entries whose recorded path disagrees with the file on disk

**Phase 4: YouTube Dialog (UI)**
- [x] Create `youtube_dialog.py` — URL input, thumbnail preview, video info panel
- [x] Build format selection table with quality presets
- [x] Add options panel (embed thumbnail, subtitles, save path)
- [x] Implement playlist/channel batch support (video list with checkboxes)
- [x] Playlist listing uses one flat request (was re-extracting every video)
- [x] Cap listed entries via `ytdlp_playlist_limit` (default 10) + report truncation
- [x] Throttle analyses (0.75s min gap) and cache identical URLs for 5 min
- [x] Hide Select all / Clear for a single-item list
- [x] Closing the dialog mid-analysis no longer crashes (daemon thread, no QThread)

**Phase 5: Menu & Toolbar Integration**
- [x] Add `Tools → Download YouTube Video…` menu action (`Ctrl+Y`)
- [x] Auto-detect YouTube URLs in "Add Download" dialog → redirect to YouTube dialog
- [x] Surface YouTube origin in the table via a tooltip (engine + uploader + video id)

**Phase 6: Settings UI**
- [x] Add YouTube sub-tab to External Tools preferences
- [x] Wire settings persistence + live validation (yt-dlp/ffmpeg path status)
- [x] yt-dlp version display + "Update yt-dlp" button (pip or binary `-U`)
- [x] Playlist entry limit control (1-500) + persistence

**Phase 7: Polish**
- [x] Handle age-restricted, geo-restricted, live stream, private/members-only videos
- [x] yt-dlp auto-update mechanism (`youtube_tool.update_ytdlp()`)

**All 7 phases complete.**

#### Mangareader scraper
- [ ] TODO

---------------------
### Fixes / Enhancements
#### 2026-09-29
*Downloads table*
- [x] Add "Last Seeded" column (when a torrent last completed a seed)
- [x] Add "Source" column (Chrome/Firefox/Edge/AnimePahe/YouTube, blank = manually added)
- [x] Add "Seeding Started At" column, updated on every seeding restart and used for the max-duration calculation
- [x] Add a size-bucket filter on the Size column (<10MB, 10-100MB, 100MB-1GB, 1-5GB, 5-10GB, 10-50GB, >50GB)
- [x] Fix segment rows showing "Pending" while downloading; cancelled/retrying segments now report "Paused"
- [x] Fix columns not refreshing after a status change; keep row selection across segregated-view rebuilds
- [x] Append new columns at the end so saved column order/widths survive, and heal stale saved layouts

*Tor*
- [x] Add "Route through Tor" to the row context menu, enabled only while Tor is reachable
- [x] Route a single HTTP download through a dedicated Tor SOCKS5 session (general proxy suppressed for it)
- [x] Cache Tor availability and probe it on a background thread (was freezing the GUI ~1s per menu open / row repaint)

*Toolbar / window*
- [x] Add a quick-search box in the toolbar gap; filters by name, original name, URL, or domain
- [x] Move Preferences to the far right of the toolbar
- [x] Add "Add Download…" and "About My-IDM" to the system tray menu
- [x] Fix editable combo boxes (Add/YouTube dialogs) rendering without a visible outline
- [x] Group the AnimePahe console log into collapsible per-session blocks (newest open, rest collapsed)
- [x] Fix playlist list overlapping the Select all / Clear buttons, and grow the list height

*Dialogs / about*
- [x] Relabel the Add Download group to "URL / Magnet Link / YouTube URL"
- [x] Keep the YouTube hand-off banner visible after closing the YouTube dialog
- [x] Update the About dialog feature summary and add AI co-author credits
- [x] Fix deleting a download mid-transfer leaving locked `.part`/`.fNNN` files behind

#### Older 
- [x] add download popup isnt picking default folder
- [x] add copy url/magnet button in context menu
- [x] for torrent show down/up speed both
- [x] show total down/up speed on footer
- [x] Save debug logs under logs folder
- [x] BUG: scan with Antivirus marks incomplete download as completed. expencted force recheck will fix that, but it also got stuck at Checking. tested for a torrent with no seed/peer.
- [x] default sort by Added DESC
- [x] in footer along with X Downloads, Show X Active
- [x] details panel:
  - [x] segments tab doesnt update unless app is restarted
  - [x] hides peers tab for HTTP
  - [x] for torrents, in files tab, add checkbox as first column for "dont download"
  - [x] save contents of all the tabs in db. downloaded a torrent, restarted the app, all tab data is gone except first tab
- [x] Toolbar: remove buttons: Open file, Open folder, Details Panel
- [x] resume queued downloads on startup
- [x] add column 1 for queue order. add move up/down in queue buttons in context menu
- [x] double click to open file
- [x] add "Fetching Metadata" state for torrents
- [x] when torrent doesnt have much seed peer and no progress for certain time, set it to stalled state for some time and try to connect to new peer
- [x] if file is not found, mark it as File not Found. reset status only when Recheck is done
- [x] Add force start button in context menu
- [x] in details panel, show files with folder hierarchy
- [x] hide details panel button has not icon/text
- [x] Dont have test files like batch 1/2/3 or glitches etc.
- [x] Use some Icon in Name column instead of a seperate Type column
- [x] add support for column ordering by dragging
- [x] Unify file path seperator in UI, db, code everywhere
- [x] make the selected row light green, instead of so vibrant color
- [x] dont show download order for Completed, Cancelled, Stopper, File not Found, Error etc. Visible numbers should be continuous.
- [x] Add context menu item to just delete file, without removing the entry. Ask for confirmation. Should pause and set progress to 0.
- [x] Recheck on completed file gets stuck at Checking
- [x] Window moving up on every launch compared to last launch
- [x] manage leftover segment files. seeing leftover files often, not sure when they are not cleaned up. Check app close, entry deletion etc.
- [x] If unchecked file is checked after download complete, update download status & percentage.
- [x] Add Tor button in add download popup
- [x] Make the settings page vertically scrollable, Antivirus page iputs are squeezed on default height
- [x] Top taskbar menu items not indented correctly
- [x] add a size-bucket filter on the Size column
- [x] add a toolbar quick-search box; filter by name, original name, URL, or domain
- [x] move the Preferences button to the right end of the toolbar
- [x] add "Add Download…" and "About My-IDM" to the system tray menu
- [x] If a torrent file url is given treat it as torrent
- [x] Make app single instance, focusing existing window on subsequent launches
- [x] Fix backlog processing on startup; pick backlog files from both project home and user home
- [x] Add support for custom download location in backlog files (inline delimiters and section directives)
- [x] Add settings UI for configuring list of places where backlog files will be picked
- [x] Automatically clear entries after processing successfully from the backlog file
- [x] Poll for backlog periodically with configurable interval (1 min default)
- [x] Add support to select multiple and copy URL/Magnet. Should copy all Url/Magnet one per line.
- [x] Add Download should also accept multiple URLs, one per line. make the textbox multiline
- [x] Add support to change the root file or folder name of a Torrent/HTTP download (at any point of time, during download or after completion)
- [x] If a torrent or HTTP download is already completed, dont try to fetch metadata when clicked on it. Leave it as it is unless recheck/force-start etc.
- [x] show source website domain in a separate column with a distinct colour
- [x] seed/peer data isnt showing up in the seed/peer column or details panel anymore
- [x] Add a stop button that puts a download to Stopped state, that dis never retried unless manually resumed, and not considered as active, doesnt have an order number
- [x] Add filters over Status & Name (for Type) columns. A filter icon on header that opens a opoup for multiselect. 
- [x] Show an ASC/DESC icon on header for columns sorted by
- [x] left click on footer speed should also open its bandwidth context menu
- [x] deleting files should move to trash, verify it is the behaviour
- [x] Add startup splash screen with loading progress, logo, and stage indicators
- [x] make retry exponential and configurable
- [x] Torrent in fetching_metadata for > configurable day should go to Suspended
- [x] window is always placed a bit below compared to last launch
- [x] column filter button is blocking column resize trigger area. filter button is on right, and dropdown is on left
- [x] there are 2 column sort buttons overlapping
- [x] make the white part of app logo transparent
- [x] why are the images in 2 places, assets and my_idm/resources
- [x] make the horizontal scroll smooth, instead of snapping on colum start/end
- [x] move the retry configuration in preferences to a seperate group
- [x] in move & add download popup: suggest last 5 unique folders where files were moved
- [x] move source domain to a seperate column instead of Name
- [x] in Save Path column, when resized, change shortning logic to prioritize leaf nodes. eg "D:/.../Folder", "D:/.../Fol...", "D:/..."
- [x] add a reset view button under Manu > View, to reset columns/filters/sorting/width etc
- [x] in delete download popup, preselct the delete file checkbox
- [x] remove the emoji for Stopped status
- [x] in tools menu > add a feature to export selected downloads as csv. 2 columns - name, url/magnet
- [x] add a segregated view toggle in view menu with 2 segregation modes: Status-based (Active, Seeding, Inactive) and Date-based (Today, Yesterday, Last 7 Days, Last 30 Days, Older based on latest of added, completed, last tried timestamps). Store active view mode and individual section collapse states in DB across app launches.
