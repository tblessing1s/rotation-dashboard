/* CFM service worker — installability + Web Push delivery.
 *
 * Kept deliberately small: no offline asset caching (the dashboard is live data
 * behind an auth cookie, so a stale cache would mislead). A pass-through fetch
 * handler is present so the app satisfies the PWA install criteria; the real
 * job is turning `push` events into lock-screen notifications and focusing the
 * app when one is tapped.
 */
const APP_SHELL = "/";

self.addEventListener("install", () => {
  // Activate this worker immediately on first install / update.
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

// Network pass-through. Present only to meet installability; no caching.
self.addEventListener("fetch", () => {});

self.addEventListener("push", (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (e) {
    payload = { title: "CFM alert", body: event.data ? event.data.text() : "" };
  }
  const title = payload.title || "CFM alert";
  const severity = payload.severity || "";
  const options = {
    body: payload.body || "",
    icon: "/icon-192.png",
    badge: "/icon-192.png",
    tag: payload.tag || "cfm-alerts",
    renotify: true,
    // CRITICAL alerts require an explicit dismiss so they can't be missed.
    requireInteraction: severity === "CRITICAL",
    data: { url: payload.url || "/" },
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((clients) => {
      for (const client of clients) {
        if ("focus" in client) {
          client.navigate(target).catch(() => {});
          return client.focus();
        }
      }
      return self.clients.openWindow(target);
    })
  );
});
