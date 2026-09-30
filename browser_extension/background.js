// My-IDM Browser Integration Extension - Background Script (Manifest V3)
// Compatible with Google Chrome, Microsoft Edge, Brave, Opera, and Mozilla Firefox (Gecko).

const DEFAULT_CONFIG = {
  enabled: true,
  serverPort: 19582,
  interceptDownloads: true,
  interceptTorrentFiles: true,
  interceptMagnetLinks: true,
  minFileSizeKb: 0,
  bypassExtensions: [".crx"]
};

// Server-controlled config keys (synced from My-IDM /config endpoint)
const SERVER_CONFIG_KEYS = [
  "enabled",
  "interceptDownloads",
  "interceptTorrentFiles",
  "interceptMagnetLinks",
  "minFileSizeKb",
  "bypassExtensions"
];

async function fetchServerConfig(port) {
  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 2000);
    const resp = await fetch(`http://127.0.0.1:${port}/config`, { signal: controller.signal });
    clearTimeout(timeout);
    if (resp.ok) {
      return await resp.json();
    }
  } catch (e) {
    console.debug("[My-IDM] Could not fetch server config:", e);
  }
  return null;
}

async function syncConfigWithServer() {
  const local = await new Promise(resolve => chrome.storage.local.get(DEFAULT_CONFIG, resolve));
  const port = local.serverPort || 19582;
  const serverConfig = await fetchServerConfig(port);
  
  if (serverConfig) {
    const merged = { ...local };
    if (serverConfig.enabled !== undefined) {
      merged.enabled = !!serverConfig.enabled;
    }
    if (serverConfig.port !== undefined) {
      merged.serverPort = serverConfig.port;
    }
    const interceptAll = serverConfig.intercept_all !== undefined ? serverConfig.intercept_all : serverConfig.interceptDownloads;
    if (interceptAll !== undefined) {
      merged.interceptDownloads = !!interceptAll;
    }
    const interceptTorrents = serverConfig.intercept_torrent_files !== undefined ? serverConfig.intercept_torrent_files : serverConfig.interceptTorrentFiles;
    if (interceptTorrents !== undefined) {
      merged.interceptTorrentFiles = !!interceptTorrents;
    }
    const interceptMagnets = serverConfig.intercept_magnet_links !== undefined ? serverConfig.intercept_magnet_links : serverConfig.interceptMagnetLinks;
    if (interceptMagnets !== undefined) {
      merged.interceptMagnetLinks = !!interceptMagnets;
    }
    const minSize = serverConfig.min_file_size_kb !== undefined ? serverConfig.min_file_size_kb : serverConfig.minFileSizeKb;
    if (minSize !== undefined) {
      merged.minFileSizeKb = parseInt(minSize, 10) || 0;
    }
    const bypassExts = serverConfig.bypassed_extensions !== undefined ? serverConfig.bypassed_extensions : serverConfig.bypassExtensions;
    if (bypassExts !== undefined) {
      merged.bypassExtensions = Array.isArray(bypassExts) ? bypassExts : [];
    }

    await new Promise(resolve => chrome.storage.local.set(merged, resolve));
    console.log("[My-IDM] Synced config with server:", merged);
    return merged;
  }
  return local;
}

// Initialize settings and context menus on install
chrome.runtime.onInstalled.addListener(async () => {
  chrome.storage.local.get(DEFAULT_CONFIG, (items) => {
    chrome.storage.local.set(items);
  });
  await syncConfigWithServer();

  if (chrome.contextMenus && chrome.contextMenus.removeAll) {
    chrome.contextMenus.removeAll(() => {
      try {
        chrome.contextMenus.create({
          id: "myidm-download-context",
          title: "Download with My-IDM",
          contexts: ["link", "image", "video", "audio"]
        });
      } catch (e) {
        console.debug("[My-IDM] Context menu creation error:", e);
      }
      try {
        chrome.contextMenus.create({
          id: "myidm-download-magnet",
          title: "Download magnet link with My-IDM",
          contexts: ["link"],
          targetUrlPatterns: ["magnet:*"]
        });
      } catch (e) {
        // Firefox and some browsers reject non-standard schemes in targetUrlPatterns
        console.debug("[My-IDM] Magnet context menu targetUrlPatterns not supported:", e);
      }
    });
  }
});

// Also sync on startup
if (chrome.runtime.onStartup) {
  chrome.runtime.onStartup.addListener(async () => {
    await syncConfigWithServer();
  });
}

// Periodic sync every 30 seconds
setInterval(async () => {
  await syncConfigWithServer();
}, 30000);

async function getConfig() {
  return new Promise((resolve) => {
    chrome.storage.local.get(DEFAULT_CONFIG, (items) => {
      resolve(items);
    });
  });
}

// URL schemes an external downloader can actually fetch. `blob:` and `data:` are
// browser-internal: a blob is an opaque handle to an in-memory object, a data URL is an
// inline payload, and neither has an HTTP representation that My-IDM could request.
const CAPTURABLE_SCHEMES = ["http:", "https:", "magnet:"];

function isCapturableUrl(url) {
  if (!url || typeof url !== "string") return false;
  const lower = url.trim().toLowerCase();
  return CAPTURABLE_SCHEMES.some((scheme) => lower.startsWith(scheme));
}

// Record a declined capture so the popup can explain it.
//
// A skip is otherwise completely invisible: Chrome downloads the file itself, so the only
// symptom is a file that never shows up in My-IDM, with no reason recorded anywhere the
// user is looking. The service-worker console has the detail but is effectively invisible
// to anyone who does not know to open it.
function recordSkip(reason, url) {
  console.log(`[My-IDM] Not capturing '${String(url).slice(0, 80)}': ${reason}`);
  chrome.storage.local.get(["skipCount"], (stored) => {
    const next = (stored.skipCount || 0) + 1;
    chrome.storage.local.set({
      lastSkip: { reason: reason, url: String(url).slice(0, 300), at: Date.now() },
      skipCount: next
    });
    setBadge(next);
  });
}

function setBadge(count) {
  try {
    if (!chrome.action || !chrome.action.setBadgeText) return;
    chrome.action.setBadgeText({ text: count > 0 ? String(count) : "" });
    chrome.action.setBadgeBackgroundColor({ color: "#e8a33d" });
  } catch (err) {
    // A missing badge API must never break interception.
  }
}

function clearSkips() {
  chrome.storage.local.remove(["lastSkip", "skipCount"]);
  setBadge(0);
}

async function getCookiesForUrl(url) {
  if (!chrome.cookies || !chrome.cookies.getAll) return "";
  try {
    const cookies = await chrome.cookies.getAll({ url: url });
    if (!cookies || cookies.length === 0) return "";
    return cookies.map(c => `${c.name}=${c.value}`).join("; ");
  } catch (err) {
    console.warn("[My-IDM] Failed to extract cookies for URL:", url, err);
    return "";
  }
}

async function sendDownloadToMyIdm(payload, port) {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 6000);

  try {
    const resp = await fetch(`http://127.0.0.1:${port}/add`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify(payload),
      signal: controller.signal
    });
    clearTimeout(timeoutId);

    if (!resp.ok) {
      console.warn("[My-IDM] Server responded with error status:", resp.status);
      return false;
    }
    const data = await resp.json();
    return data.status === "ok";
  } catch (err) {
    clearTimeout(timeoutId);
    console.warn("[My-IDM] Connection to local server failed:", err);
    return false;
  }
}

// Handle right-click context menu "Download with My-IDM"
if (chrome.contextMenus && chrome.contextMenus.onClicked) {
  chrome.contextMenus.onClicked.addListener(async (info, tab) => {
    const targetUrl = info.linkUrl || info.srcUrl || info.pageUrl;
    if (!targetUrl) return;

    if (info.menuItemId === "myidm-download-magnet" || targetUrl.startsWith("magnet:")) {
      // Magnet link handler
      const cfg = await getConfig();
      const payload = {
        url: targetUrl,
        referrer: info.pageUrl || (tab && tab.url) || "",
        cookies: "",
        user_agent: navigator.userAgent
      };

      const success = await sendDownloadToMyIdm(payload, cfg.serverPort || 19582);
      if (!success) {
        console.warn("[My-IDM] Failed to send magnet link to My-IDM via context menu.");
      }
      return;
    }

    // Regular download context menu
    const cfg = await getConfig();
    const cookies = await getCookiesForUrl(targetUrl);

    const payload = {
      url: targetUrl,
      referrer: info.pageUrl || (tab && tab.url) || "",
      cookies: cookies,
      user_agent: navigator.userAgent
    };

    const success = await sendDownloadToMyIdm(payload, cfg.serverPort || 19582);
    if (!success) {
      console.warn("[My-IDM] Failed to send download to My-IDM via context menu.");
    }
  });
}

// Intercept messages from content scripts (e.g. magnet link clicks)
if (chrome.runtime.onMessage) {
  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message && message.type === "MAGNET_LINK_CLICKED" && message.url) {
      (async () => {
        const cfg = await getConfig();
        if (!cfg.enabled || !cfg.interceptDownloads || !cfg.interceptMagnetLinks) return;

        const payload = {
          url: message.url,
          referrer: (sender && sender.tab && sender.tab.url) || "",
          cookies: "",
          user_agent: navigator.userAgent
        };

        const success = await sendDownloadToMyIdm(payload, cfg.serverPort || 19582);
        if (success) {
          console.log("[My-IDM] Successfully forwarded magnet link from content script:", message.url);
        }
      })();
    }
  });
}

// ---------------------------------------------------------------------------
// Download Interception Engine (Chromium vs Firefox)
// ---------------------------------------------------------------------------

if (chrome.downloads && chrome.downloads.onDeterminingFilename) {
  // --- Chromium Engine (Chrome, Edge, Brave, Opera, Vivaldi) ---
  chrome.downloads.onDeterminingFilename.addListener((item, suggest) => {
    (async () => {
      const cfg = await getConfig();

      // If integration or interception is disabled, let browser handle natively
      if (!cfg.enabled || !cfg.interceptDownloads) {
        suggest({ filename: item.filename });
        return;
      }

      const downloadUrl = item.finalUrl || item.url || "";

      // Reject schemes with no HTTP transport. `blob:` is what Chrome reports for
      // JS-generated downloads - and what GitHub hands out on some private-repo asset
      // pages - and it is a browser-internal object reference that no external
      // downloader can fetch. Handing one to My-IDM only produces a download that fails
      // the full retry ladder (5s, 10s, 20s, 40s, 60s) before erroring. The Firefox
      // engine below already had this guard; Chrome did not, so the same page was
      // captured on one browser and skipped on the other.
      if (!isCapturableUrl(downloadUrl)) {
        recordSkip(
          "This page handed the browser an internal URL (blob:/data:), which has no " +
          "address an external downloader can request. Chrome downloads it instead. " +
          "Right-click the link and copy its address - if it is a blob: URL, use Chrome's " +
          "own download; an https://github.com/... link can be captured.",
          downloadUrl
        );
        suggest({ filename: item.filename });
        return;
      }

      // Ignore local loopback requests to avoid loops
      if (downloadUrl.includes("127.0.0.1:19582") || downloadUrl.includes("localhost:19582")) {
        suggest({ filename: item.filename });
        return;
      }

      // Check bypassed extensions
      const filename = (item.filename || "").toLowerCase();
      const isTorrentFile = filename.endsWith(".torrent") || downloadUrl.toLowerCase().split("?")[0].endsWith(".torrent");
      const isBypassed = (cfg.bypassExtensions || []).some(ext =>
        filename.endsWith(ext.toLowerCase())
      );
      // Allow torrent files if interceptTorrentFiles is enabled
      if (isBypassed && !(isTorrentFile && cfg.interceptTorrentFiles)) {
        suggest({ filename: item.filename });
        return;
      }

      // Check minimum file size (in KB, 0 = no minimum)
      // Only skip if file size is definitively known (> 0) and strictly less than minSizeKb
      const minSizeKb = parseInt(cfg.minFileSizeKb, 10) || 0;
      const totalBytes = (item.totalBytes !== undefined && item.totalBytes > 0)
        ? item.totalBytes
        : (item.fileSize !== undefined && item.fileSize > 0 ? item.fileSize : 0);

      if (minSizeKb > 0 && totalBytes > 0) {
        const sizeKb = totalBytes / 1024;
        if (sizeKb < minSizeKb) {
          console.log(`[My-IDM] Skipping download (${sizeKb.toFixed(1)} KB < ${minSizeKb} KB threshold):`, item.filename);
          suggest({ filename: item.filename });
          return;
        }
      }

      // Extract cookies for authenticated downloads
      const cookies = await getCookiesForUrl(downloadUrl);

      const payload = {
        url: downloadUrl,
        filename: item.filename || "",
        total_bytes: totalBytes,
        file_size: totalBytes,
        referrer: item.referrer || "",
        cookies: cookies,
        user_agent: navigator.userAgent
      };

      const success = await sendDownloadToMyIdm(payload, cfg.serverPort || 19582);
      if (success) {
        // My-IDM accepted download -> cancel and erase from browser downloads tray
        chrome.downloads.cancel(item.id, () => {
          chrome.downloads.erase({ id: item.id });
        });
        suggest();
      } else {
        // Server offline, rejected, or below size threshold -> fallback to native browser download
        suggest({ filename: item.filename });
      }
    })();

    return true; // Keep suggest callback valid asynchronously
  });
} else if (chrome.downloads && chrome.downloads.onCreated) {
  // --- Mozilla Firefox WebExtension Engine ---
  // Firefox does not support chrome.downloads.onDeterminingFilename;
  // instead, we intercept onCreated, cancel the download, and pass to My-IDM.
  chrome.downloads.onCreated.addListener(async (item) => {
    try {
      const cfg = await getConfig();
      if (!cfg.enabled || !cfg.interceptDownloads) return;

      const downloadUrl = item.finalUrl || item.url || "";
      if (!downloadUrl || !isCapturableUrl(downloadUrl)) return;
      if (downloadUrl.includes("127.0.0.1:19582") || downloadUrl.includes("localhost:19582")) return;

      // Extract filename from item or download URL
      let filename = (item.filename || "").toLowerCase();
      if (!filename) {
        try {
          const parsed = new URL(downloadUrl);
          filename = decodeURIComponent(parsed.pathname.split("/").pop() || "").toLowerCase();
        } catch (e) {}
      }

      const isTorrentFile = filename.endsWith(".torrent") || downloadUrl.toLowerCase().split("?")[0].endsWith(".torrent");
      const isBypassed = (cfg.bypassExtensions || []).some(ext =>
        filename.endsWith(ext.toLowerCase())
      );
      if (isBypassed && !(isTorrentFile && cfg.interceptTorrentFiles)) return;

      // Check minimum file size
      const minSizeKb = parseInt(cfg.minFileSizeKb, 10) || 0;
      const totalBytes = item.totalBytes !== undefined && item.totalBytes > 0
        ? item.totalBytes
        : (item.fileSize !== undefined && item.fileSize > 0 ? item.fileSize : 0);
      if (minSizeKb > 0 && totalBytes > 0 && (totalBytes / 1024) < minSizeKb) return;

      // Extract cookies
      const cookies = await getCookiesForUrl(downloadUrl);

      const cleanFilename = item.filename ? item.filename.split(/[\\/]/).pop() : (filename || "");
      const payload = {
        url: downloadUrl,
        filename: cleanFilename,
        total_bytes: totalBytes,
        file_size: totalBytes,
        referrer: item.referrer || "",
        cookies: cookies,
        user_agent: navigator.userAgent
      };

      const success = await sendDownloadToMyIdm(payload, cfg.serverPort || 19582);
      if (success) {
        // Cancel and remove from Firefox download manager history
        try {
          chrome.downloads.cancel(item.id, () => {
            try {
              chrome.downloads.erase({ id: item.id });
            } catch (err) {}
          });
        } catch (err) {}
      }
    } catch (err) {
      console.debug("[My-IDM Firefox Engine] Download interception error:", err);
    }
  });
}

// Intercept magnet link navigations when webNavigation scheme filter is supported
if (chrome.webNavigation && chrome.webNavigation.onBeforeNavigate) {
  try {
    chrome.webNavigation.onBeforeNavigate.addListener(async (details) => {
      if (details.frameId !== 0 || !details.url || !details.url.startsWith("magnet:")) return;

      const cfg = await getConfig();
      if (!cfg.enabled || !cfg.interceptDownloads || !cfg.interceptMagnetLinks) return;

      const payload = {
        url: details.url,
        referrer: "",
        cookies: "",
        user_agent: navigator.userAgent
      };

      const success = await sendDownloadToMyIdm(payload, cfg.serverPort || 19582);
      if (success && chrome.tabs && chrome.tabs.remove) {
        try {
          chrome.tabs.remove(details.tabId);
        } catch (e) {}
      }
    }, { url: [{ schemes: ["magnet"] }] });
  } catch (err) {
    console.debug("[My-IDM] webNavigation magnet scheme filter not supported:", err);
  }
}
