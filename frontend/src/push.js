// Web Push + service-worker plumbing for the PWA.
//
// The service worker (public/sw.js) makes the app installable and turns push
// events into notifications. These helpers register it and manage this device's
// push subscription against the browser's PushManager. All server persistence
// goes through api.js (push/subscribe, push/unsubscribe).

let swRegistration = null;

export function pushSupported() {
  return (
    typeof navigator !== "undefined" &&
    "serviceWorker" in navigator &&
    "PushManager" in window &&
    "Notification" in window
  );
}

export async function registerServiceWorker() {
  if (typeof navigator === "undefined" || !("serviceWorker" in navigator)) return null;
  try {
    swRegistration = await navigator.serviceWorker.register("/sw.js");
    return swRegistration;
  } catch (e) {
    // Non-fatal: the app still works as a normal website, just not installable.
    console.warn("service worker registration failed", e);
    return null;
  }
}

async function ready() {
  if (swRegistration) return swRegistration;
  if ("serviceWorker" in navigator) {
    swRegistration = await navigator.serviceWorker.ready;
  }
  return swRegistration;
}

// VAPID public key is base64url; PushManager wants a Uint8Array.
function urlB64ToUint8Array(base64String) {
  const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = window.atob(base64);
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

export function notificationPermission() {
  return typeof Notification !== "undefined" ? Notification.permission : "unsupported";
}

export async function currentSubscription() {
  const reg = await ready();
  if (!reg) return null;
  return reg.pushManager.getSubscription();
}

// Ask permission, subscribe with the server's VAPID key, and return the raw
// PushSubscription JSON (endpoint + p256dh/auth keys) for the server to store.
export async function enablePush(vapidPublicKey) {
  if (!pushSupported()) throw new Error("This browser does not support push notifications.");
  if (!vapidPublicKey) throw new Error("Server has no VAPID key configured.");
  const reg = await ready();
  if (!reg) throw new Error("Service worker is not ready yet — reload and retry.");

  const permission = await Notification.requestPermission();
  if (permission !== "granted") {
    throw new Error(`Notification permission ${permission}. Enable it in the browser/site settings.`);
  }

  let sub = await reg.pushManager.getSubscription();
  if (!sub) {
    sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlB64ToUint8Array(vapidPublicKey),
    });
  }
  return sub.toJSON();
}

// Unsubscribe this device from the browser push service. Returns the endpoint
// that was removed (so the server can drop it) or null if none existed.
export async function disablePush() {
  const reg = await ready();
  if (!reg) return null;
  const sub = await reg.pushManager.getSubscription();
  if (!sub) return null;
  const endpoint = sub.endpoint;
  await sub.unsubscribe();
  return endpoint;
}
