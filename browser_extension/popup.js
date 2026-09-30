// My-IDM Chrome Integration Extension - Popup Script

document.addEventListener("DOMContentLoaded", async () => {
  const statusDot = document.getElementById("statusDot");
  const statusText = document.getElementById("statusText");
  const refreshBtn = document.getElementById("refreshBtn");
  const interceptToggle = document.getElementById("interceptToggle");
  const enableToggle = document.getElementById("enableToggle");
  const openOptionsBtn = document.getElementById("openOptionsBtn");
  const portDisplay = document.getElementById("portDisplay");
  const skipNotice = document.getElementById("skipNotice");
  const skipReason = document.getElementById("skipReason");
  const skipUrl = document.getElementById("skipUrl");
  const skipCount = document.getElementById("skipCount");

  let currentPort = 19582;

  // Load saved preferences
  chrome.storage.local.get({
    enabled: true,
    serverPort: 19582,
    interceptDownloads: true
  }, (cfg) => {
    enableToggle.checked = !!cfg.enabled;
    interceptToggle.checked = !!cfg.interceptDownloads;
    currentPort = cfg.serverPort || 19582;
    portDisplay.textContent = `127.0.0.1:${currentPort}`;
    checkHealth();
    showLastSkip();
  });

  // Surface the most recent skip in the popup. A download the extension declines is
  // invisible otherwise: Chrome just downloads it itself, so the only symptom is a file
  // that never appears in My-IDM and no explanation anywhere the user is looking. This
  // is the second time that has cost real debugging time, so the reason is surfaced where
  // the user already is instead of only in the service-worker console.
  async function showLastSkip() {
    const stored = await chrome.storage.local.get(["lastSkip", "skipCount"]);
    const skip = stored.lastSkip;
    if (!skip || !skip.reason) {
      skipNotice.hidden = true;
      return;
    }
    const count = stored.skipCount || 0;
    skipReason.textContent = skip.reason;
    skipUrl.textContent = skip.url || "";
    skipUrl.title = skip.url || "";
    skipCount.textContent = count > 1 ? `(${count} this session)` : "";
    skipNotice.hidden = false;
  }

  async function checkHealth() {
    statusDot.className = "dot";
    statusText.textContent = "Connecting...";
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 1800);
      const resp = await fetch(`http://127.0.0.1:${currentPort}/health`, {
        signal: controller.signal
      });
      clearTimeout(timeout);

      if (resp.ok) {
        const data = await resp.json();
        statusDot.className = "dot connected";
        const count = data.active_downloads || 0;
        statusText.textContent = `Connected (${count} active)`;
      } else {
        throw new Error(`HTTP ${resp.status}`);
      }
    } catch (e) {
      statusDot.className = "dot disconnected";
      statusText.textContent = "My-IDM Offline";
    }
  }

  refreshBtn.addEventListener("click", () => {
    checkHealth();
  });

  enableToggle.addEventListener("change", () => {
    chrome.storage.local.set({ enabled: enableToggle.checked });
  });

  interceptToggle.addEventListener("change", () => {
    chrome.storage.local.set({ interceptDownloads: interceptToggle.checked });
  });

  openOptionsBtn.addEventListener("click", () => {
    if (chrome.runtime.openOptionsPage) {
      chrome.runtime.openOptionsPage();
    } else {
      window.open(chrome.runtime.getURL("options.html"));
    }
  });
});
