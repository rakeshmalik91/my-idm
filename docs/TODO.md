# TODO list
---------------------
Pick feature, enhancements, fixes from here.
commit after each features or set of fixes/enhancements
---------------------

### CRITICAL BUG
- [x] had 3 torrents, 1 completed, 2 downloading, everytime i restart the app, one of the downloading torrent gets replaced by the completed one 
- [x] download going on even in paused state sometimes
- [x] to check why animepahe downloads are going to queued state and not retrying, manual resume works fine

### Fixes / Enhancements
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
- [x] manage leftover segment files. seeing leftover files often, not sure when they are not cleaned up. Check app close, entry deletion etc.
- [x] If unchecked file is checked after download complete, update download status & percentage.
- [x] Add Tor button in add download popup
- [x] Make the settings page vertically scrollable, Antivirus page iputs are squeezed on default height
- [x] Top taskbar menu items not indented correctly
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

----------------------
### Torrent
- [x] UNVERIFIED - Torrent in "fetching metadata" state for more than 1 (configurable) day should go to Suspended state. Suspended state shouldnt be considered active, shouldnt have an order. timer calculation should be regardless of app restart. Manual resume or successful progress should reset the timer.
- [x] for completed torrents, in details panel, files are showing Pending status
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
- [ ] to check possibilities: chrome, mozilla

----------------------
### Tools
- [ ] Youtube scraper
- [ ] inbuilt Animepahe scraper
- [ ] Mangareader scraper
