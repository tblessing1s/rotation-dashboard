import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App.jsx";
import { ToastProvider } from "./components/Toast.jsx";
import { ErrorBoundary } from "./components/ui.jsx";
import { registerServiceWorker } from "./push.js";
import "./index.css";

// Last-resort boundary. App wraps each tab's content in its own boundary (that is
// the one that normally catches a bad card and keeps the nav usable); this one only
// sees a throw from the shell itself — Navbar, the auth gate, SchwabStatus — where
// there is no smaller unit left to contain. Without it those throws render nothing
// at all, with no message and no way back except a manual reload.
createRoot(document.getElementById("root")).render(
  <ToastProvider>
    <ErrorBoundary label="The dashboard">
      <App />
    </ErrorBoundary>
  </ToastProvider>
);

// Register the service worker so the app is installable and can receive push.
registerServiceWorker();
