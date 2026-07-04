import React from "react";
import { api } from "../api.js";
import {
  currentSubscription,
  disablePush,
  enablePush,
  notificationPermission,
  pushSupported,
} from "../push.js";
import { useToast } from "./Toast.jsx";

// Per-device Web Push control, shown inside the Alerts settings panel. Each
// phone/browser enables push once; the subscription lives in state.json and the
// alert engine's "webpush" channel delivers to it. Installing the app to the
// home screen (PWA) first makes push far more reliable on Android.
export default function PushSetup() {
  const toast = useToast();
  const [server, setServer] = React.useState(null); // {configured, subscriptions}
  const [subscribedHere, setSubscribedHere] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [loaded, setLoaded] = React.useState(false);

  const supported = pushSupported();
  const permission = notificationPermission();

  const refresh = React.useCallback(async () => {
    try {
      const key = await api.pushVapidKey();
      setServer(key);
    } catch {
      setServer({ configured: false, subscriptions: 0 });
    }
    if (supported) {
      const sub = await currentSubscription();
      setSubscribedHere(!!sub);
    }
    setLoaded(true);
  }, [supported]);

  React.useEffect(() => {
    refresh();
  }, [refresh]);

  async function enable() {
    setBusy(true);
    try {
      const { key } = await api.pushVapidKey();
      const sub = await enablePush(key);
      await api.pushSubscribe(sub);
      setSubscribedHere(true);
      toast.show("Push enabled on this device.", { type: "success" });
      await refresh();
    } catch (e) {
      toast.show(e.message || "Could not enable push.", { type: "error", duration: 7000 });
    } finally {
      setBusy(false);
    }
  }

  async function disable() {
    setBusy(true);
    try {
      const endpoint = await disablePush();
      if (endpoint) await api.pushUnsubscribe(endpoint);
      setSubscribedHere(false);
      toast.show("Push disabled on this device.", { type: "info" });
      await refresh();
    } catch (e) {
      toast.show(e.message || "Could not disable push.", { type: "error" });
    } finally {
      setBusy(false);
    }
  }

  async function test() {
    setBusy(true);
    try {
      const res = await api.pushTest();
      toast.show(`Test push sent to ${res.devices} device(s).`, { type: "success" });
    } catch (e) {
      toast.show(e.message || "Test push failed.", { type: "error", duration: 7000 });
    } finally {
      setBusy(false);
    }
  }

  if (!loaded) return null;

  const btn = "rounded-full border px-2.5 py-1 text-xs font-semibold disabled:opacity-50";
  const primary = `${btn} border-emerald-600/50 bg-emerald-500/10 text-emerald-300 hover:bg-emerald-500/20`;
  const neutral = `${btn} border-slate-700 bg-slate-800/60 text-slate-300 hover:bg-slate-800`;

  return (
    <div className="mt-3 rounded-lg border border-slate-800 bg-slate-950/40 p-3">
      <div className="mb-1 flex items-center gap-2">
        <span className="text-xs font-semibold uppercase tracking-wide text-slate-400">
          Push notifications (this device)
        </span>
        {subscribedHere && (
          <span className="rounded-full bg-emerald-500/15 px-2 py-0.5 text-[10px] font-semibold text-emerald-300">
            ON
          </span>
        )}
      </div>

      {!supported && (
        <p className="text-xs text-amber-300">
          This browser can’t do Web Push. On Android use Chrome; install the app to the home
          screen first (menu → “Add to Home screen” / “Install app”).
        </p>
      )}

      {supported && server && !server.configured && (
        <p className="text-xs text-amber-300">
          The server has no VAPID keys set, so native push is off. Run
          <code className="mx-1 rounded bg-slate-800 px-1">python scripts/gen_vapid_keys.py</code>
          and set the printed secrets, then reload. (The ntfy channel below works without this.)
        </p>
      )}

      {supported && server?.configured && (
        <>
          <p className="text-xs text-slate-400">
            Deliver kill-switch / roll / assignment alerts straight to this phone’s lock screen —
            even when the app is closed.
            {permission === "denied" && (
              <span className="text-amber-300"> Notifications are blocked in site settings; re-allow them to enable.</span>
            )}
          </p>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            {!subscribedHere ? (
              <button onClick={enable} disabled={busy || permission === "denied"} className={primary}>
                {busy ? "Enabling…" : "Enable on this device"}
              </button>
            ) : (
              <>
                <button onClick={test} disabled={busy} className={primary}>
                  {busy ? "…" : "Send test"}
                </button>
                <button onClick={disable} disabled={busy} className={neutral}>
                  Disable here
                </button>
              </>
            )}
            <span className="text-[11px] text-slate-600">
              {server.subscriptions} device(s) registered
            </span>
          </div>
        </>
      )}
    </div>
  );
}
