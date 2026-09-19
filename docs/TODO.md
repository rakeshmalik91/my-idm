# TODO list
---------------------
Pick feature, enhancements, fixes from here.
commit after each features or set of fixes/enhancements
---------------------

### CRITICAL BUG
- [x] had 3 torrents, 1 completed, 2 downloading, everytime i restart the app, one of the downloading torrent gets replaced by the completed one 
- [x] download going on even in paused state sometimes

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

----------------------
### Tor Support
- [x] add support for activating tor, add as a button in toolbar. 
  - [x] make what to run inside tor configurable in settings, usual download or torrent etc. 
  - [x] make whether to activate tor at startup or not configurable too. configurable
  - [x] when exiting with tor on, stop it and exit
  - [x] dont start with tor unless that settings is on
  - [x] fix progressbar glitch when tor is turned on/off during download
  - [x] while connecting or disconnecting tor, show a progressbar on both toolbar and footer tor buttons. when ON make the buttons green.
  - [x] before tor start/stop pause all ongoing downloads, and resume on tor stop/start. ensure switching works seemslessly and doesnt mess up downloads data.

----------------------
### Bandwidth control
- [x] Add support for bandwidth allocation of downloads and files in torrent downloads (Low - 25%, Medium - 50%, High - 75%, Max - 100%)
- [x] Add support total Up/Down Bandwidth limit in footer speed context menu (1kbps, 2kbps, 5kbps, 10kbps, 50kbps, 100kbps, 200kbps, 500kbps, 1mbps, 2mbps, 5mbps, 10mbps, 100 mbps, Unlimited, Custom)
  
----------------------

