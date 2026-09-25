// My-IDM Chrome Integration Extension - Options Script

document.addEventListener("DOMContentLoaded", () => {
  const serverPortInput = document.getElementById("serverPort");
  const interceptDownloadsCheckbox = document.getElementById("interceptDownloads");
  const bypassExtsInput = document.getElementById("bypassExts");
  const testBtn = document.getElementById("testBtn");
  const statusDot = document.getElementById("statusDot");
  const statusText = document.getElementById("statusText");
  const saveBtn = document.getElementById("saveBtn");
  const toast = document.getElementById("toast");

  // Load preferences
  chrome.storage.local.get({
    serverPort: 19582,
    interceptDownloads: true,
    bypassExtensions: [".torrent", ".crx"]
  }, (items) => {
    serverPortInput.value = items.serverPort || 19582;
    interceptDownloadsCheckbox.checked = !!items.interceptDownloads;
    bypassExtsInput.value = (items.bypassExtensions || [".torrent", ".crx"]).join(", ");
    testConnection();
  });

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

    const exts = bypassExtsInput.value
      .split(",")
      .map(s => s.trim())
      .filter(s => s.length > 0)
      .map(s => s.startsWith(".") ? s : `.${s}`);

    chrome.storage.local.set({
      serverPort: port,
      interceptDownloads: interceptDownloadsCheckbox.checked,
      bypassExtensions: exts
    }, () => {
      showToast("Settings saved successfully!", true);
      testConnection();
    });
  });
});
