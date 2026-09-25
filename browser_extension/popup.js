// My-IDM Chrome Integration Extension - Popup Script

document.addEventListener("DOMContentLoaded", async () => {
  const statusDot = document.getElementById("statusDot");
  const statusText = document.getElementById("statusText");
  const refreshBtn = document.getElementById("refreshBtn");
  const interceptToggle = document.getElementById("interceptToggle");
  const enableToggle = document.getElementById("enableToggle");
  const openOptionsBtn = document.getElementById("openOptionsBtn");
  const portDisplay = document.getElementById("portDisplay");

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
  });

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
