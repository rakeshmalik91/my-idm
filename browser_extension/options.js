// My-IDM Chrome Integration Extension - Options Script

document.addEventListener("DOMContentLoaded", () => {
  const serverPortInput = document.getElementById("serverPort");
  const interceptDownloadsCheckbox = document.getElementById("interceptDownloads");
  const interceptTorrentFilesCheckbox = document.getElementById("interceptTorrentFiles");
  const interceptMagnetLinksCheckbox = document.getElementById("interceptMagnetLinks");
  const minFileSizeKbInput = document.getElementById("minFileSizeKb");
  const bypassExtsInput = document.getElementById("bypassExts");
  const testBtn = document.getElementById("testBtn");
  const statusDot = document.getElementById("statusDot");
  const statusText = document.getElementById("statusText");
  const saveBtn = document.getElementById("saveBtn");
  const toast = document.getElementById("toast");

  const DEFAULT_CONFIG = {
    serverPort: 19582,
    interceptDownloads: true,
    interceptTorrentFiles: true,
    interceptMagnetLinks: true,
    minFileSizeKb: 0,
    bypassExtensions: [".crx"]
  };

  // Load preferences from local storage first
  chrome.storage.local.get(DEFAULT_CONFIG, (items) => {
    serverPortInput.value = items.serverPort || 19582;
    interceptDownloadsCheckbox.checked = !!items.interceptDownloads;
    interceptTorrentFilesCheckbox.checked = !!items.interceptTorrentFiles;
    interceptMagnetLinksCheckbox.checked = !!items.interceptMagnetLinks;
    minFileSizeKbInput.value = items.minFileSizeKb || 0;
    bypassExtsInput.value = (items.bypassExtensions || [".crx"]).join(", ");
    testConnection();
  });

  // Also fetch fresh config from My-IDM server to show current server-side settings
  async function loadServerConfig() {
    const port = parseInt(serverPortInput.value, 10) || 19582;
    try {
      const resp = await fetch(`http://127.0.0.1:${port}/config`);
      if (resp.ok) {
        const serverConfig = await resp.json();
        // Update UI with server config for server-controlled settings
        if (serverConfig.enabled !== undefined) interceptDownloadsCheckbox.checked = !!serverConfig.enabled;
        if (serverConfig.intercept_all !== undefined) interceptDownloadsCheckbox.checked = !!serverConfig.intercept_all;
        if (serverConfig.intercept_torrent_files !== undefined) interceptTorrentFilesCheckbox.checked = !!serverConfig.intercept_torrent_files;
        if (serverConfig.intercept_magnet_links !== undefined) interceptMagnetLinksCheckbox.checked = !!serverConfig.intercept_magnet_links;
        const minSize = serverConfig.min_file_size_kb !== undefined ? serverConfig.min_file_size_kb : serverConfig.minFileSizeKb;
        if (minSize !== undefined) minFileSizeKbInput.value = minSize;
        if (serverConfig.bypassed_extensions !== undefined) {
          bypassExtsInput.value = (serverConfig.bypassed_extensions || []).join(", ");
        } else if (serverConfig.bypassExtensions !== undefined) {
          bypassExtsInput.value = (serverConfig.bypassExtensions || []).join(", ");
        }
        // Also update local storage to match
        chrome.storage.local.set({
          interceptDownloads: serverConfig.intercept_all ?? serverConfig.interceptDownloads ?? serverConfig.enabled ?? interceptDownloadsCheckbox.checked,
          interceptTorrentFiles: serverConfig.intercept_torrent_files ?? serverConfig.interceptTorrentFiles ?? interceptTorrentFilesCheckbox.checked,
          interceptMagnetLinks: serverConfig.intercept_magnet_links ?? serverConfig.interceptMagnetLinks ?? interceptMagnetLinksCheckbox.checked,
          minFileSizeKb: parseInt(minSize ?? minFileSizeKbInput.value, 10) || 0,
          bypassExtensions: serverConfig.bypassed_extensions ?? serverConfig.bypassExtensions ?? bypassExtsInput.value.split(",").map(s => s.trim()).filter(s => s).map(s => s.startsWith(".") ? s : `.${s}`)
        });
      }
    } catch (e) {
      console.debug("[My-IDM Options] Could not load server config:", e);
    }
  }

  // Load server config on page open
  loadServerConfig();

  async function testConnection() {
    const port = parseInt(serverPortInput.value, 10) || 19582;
    statusDot.className = "dot";
    statusText.textContent = "Testing...";

    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 2000);
      const resp = await fetch(`http://127.0.0.1:${port}/health`, {
        signal: controller.signal
      });
      clearTimeout(timeout);

      if (resp.ok) {
        const data = await resp.json();
        statusDot.className = "dot connected";
        statusText.textContent = `Connected (My-IDM v${data.version || "1.0"})`;
        return true;
      } else {
        throw new Error(`HTTP ${resp.status}`);
      }
    } catch (e) {
      statusDot.className = "dot disconnected";
      statusText.textContent = "Cannot connect to My-IDM loopback server";
      return false;
    }
  }

  function showToast(msg, isSuccess) {
    toast.textContent = msg;
    toast.className = `toast show ${isSuccess ? "success" : "error"}`;
    setTimeout(() => {
      toast.className = "toast";
    }, 3000);
  }

  testBtn.addEventListener("click", () => {
    testConnection();
  });

  saveBtn.addEventListener("click", () => {
    const port = parseInt(serverPortInput.value, 10);
    if (!port || port < 1024 || port > 65535) {
      showToast("Port must be between 1024 and 65535", false);
      return;
    }

    const minSize = parseInt(minFileSizeKbInput.value, 10);
    if (isNaN(minSize) || minSize < 0 || minSize > 1000000) {
      showToast("Minimum file size must be between 0 and 1,000,000 KB", false);
      return;
    }

    const exts = bypassExtsInput.value
      .split(",")
      .map(s => s.trim())
      .filter(s => s.length > 0)
      .map(s => s.startsWith(".") ? s : `.${s}`);

    chrome.storage.local.set({
      serverPort: port,
      interceptDownloads: interceptDownloadsCheckbox.checked,
      interceptTorrentFiles: interceptTorrentFilesCheckbox.checked,
      interceptMagnetLinks: interceptMagnetLinksCheckbox.checked,
      minFileSizeKb: minSize,
      bypassExtensions: exts
    }, () => {
      showToast("Settings saved successfully!", true);
      testConnection();
    });
  });
});
