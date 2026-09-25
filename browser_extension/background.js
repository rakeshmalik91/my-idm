// My-IDM Chrome Integration Extension - Service Worker (Manifest V3)

const DEFAULT_CONFIG = {
  enabled: true,
  serverPort: 19582,
  interceptDownloads: true,
  bypassExtensions: [".torrent", ".crx"]
};

// Initialize settings and context menus on install
chrome.runtime.onInstalled.addListener(async () => {
  chrome.storage.local.get(DEFAULT_CONFIG, (items) => {
    chrome.storage.local.set(items);
  });

  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: "myidm-download-context",
      title: "Download with My-IDM",
      contexts: ["link", "image", "video", "audio"]
    });
  });
});

async function getConfig() {
  return new Promise((resolve) => {
    chrome.storage.local.get(DEFAULT_CONFIG, (items) => {
      resolve(items);
    });
  });
}

async function getCookiesForUrl(url) {
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
  const timeoutId = setTimeout(() => controller.abort(), 3000);

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
chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  const targetUrl = info.linkUrl || info.srcUrl || info.pageUrl;
  if (!targetUrl) return;

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

// Intercept browser downloads
chrome.downloads.onDeterminingFilename.addListener((item, suggest) => {
  // Use async handling
  (async () => {
    const cfg = await getConfig();

    // If integration or interception is disabled, let Chrome handle it natively
    if (!cfg.enabled || !cfg.interceptDownloads) {
      suggest({ filename: item.filename });
      return;
    }

    const downloadUrl = item.finalUrl || item.url || "";

    // Ignore local loopback requests to avoid loops
    if (downloadUrl.includes("127.0.0.1:19582") || downloadUrl.includes("localhost:19582")) {
      suggest({ filename: item.filename });
      return;
    }

    // Check bypassed extensions
    const filename = (item.filename || "").toLowerCase();
    const isBypassed = (cfg.bypassExtensions || []).some(ext =>
      filename.endsWith(ext.toLowerCase())
    );
    if (isBypassed) {
      suggest({ filename: item.filename });
      return;
    }

    // Extract cookies for authenticated downloads
    const cookies = await getCookiesForUrl(downloadUrl);

    const payload = {
      url: downloadUrl,
      filename: item.filename || "",
      referrer: item.referrer || "",
      cookies: cookies,
      user_agent: navigator.userAgent
    };

    const success = await sendDownloadToMyIdm(payload, cfg.serverPort || 19582);
    if (success) {
      // My-IDM accepted download -> cancel and erase from Chrome downloads tray
      chrome.downloads.cancel(item.id, () => {
        chrome.downloads.erase({ id: item.id });
      });
      // Complete suggest callback
      suggest();
    } else {
      // Server offline or rejected -> let Chrome download natively as fallback
      suggest({ filename: item.filename });
    }
  })();

  return true; // Keep suggest callback valid asynchronously
});
