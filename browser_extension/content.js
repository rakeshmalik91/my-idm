// My-IDM Browser Extension - Content Script
// Captures magnet link clicks directly in the page DOM across all browsers (Firefox, Chrome, Edge)

document.addEventListener("click", (event) => {
  try {
    const anchor = event.target.closest("a");
    if (!anchor || !anchor.href) return;

    const href = anchor.href;
    if (href.startsWith("magnet:")) {
      // Notify background script to forward to My-IDM
      chrome.runtime.sendMessage({
        type: "MAGNET_LINK_CLICKED",
        url: href
      });
      // Prevent browser default prompt or empty tab
      event.preventDefault();
      event.stopPropagation();
    }
  } catch (err) {
    console.debug("[My-IDM] Content script magnet handler error:", err);
  }
}, true);
