import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App.jsx";
import { ToastProvider } from "./components/Toast.jsx";
import { registerServiceWorker } from "./push.js";
import "./index.css";

createRoot(document.getElementById("root")).render(
  <ToastProvider>
    <App />
  </ToastProvider>
);

// Register the service worker so the app is installable and can receive push.
registerServiceWorker();
