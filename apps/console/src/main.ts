// Entry point — the app self-boots on import.
import "./app";

// Register the service worker so the console installs as a standalone PWA and
// opens offline. Ignored in dev / unsupported browsers.
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {
      /* SW is a progressive enhancement — never block the app on it */
    });
  });
}
