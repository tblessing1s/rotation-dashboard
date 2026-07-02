"""Delivery channels for CFM alerts, behind one small interface.

Adding a channel = implementing one class with ``name``, ``configured()`` and
``send()``, then listing it in CHANNELS. Email (SMTP) ships first; NtfyNotifier
shows the push path (any webhook-style service — Pushover, Slack, SMS gateways —
follows the same shape). ``dispatch`` fans one batch of alerts out to every
channel that is both configured (env) and enabled (operator settings), and
falls back to the process log when nothing is configured or dry-run is on, so
an evaluator run never silently drops alerts.
"""
from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

import requests

logger = logging.getLogger("cfm.alerts")

SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2}


def format_subject(alerts: list[dict]) -> str:
    worst = min(alerts, key=lambda a: SEVERITY_ORDER.get(a.get("severity"), 9))
    tickers = sorted({a["ticker"] for a in alerts if a.get("ticker")})
    scope = ", ".join(tickers[:4]) or "portfolio"
    return f"[CFM {worst.get('severity', 'ALERT')}] {len(alerts)} alert(s) — {scope}"


def format_body(alerts: list[dict]) -> str:
    ordered = sorted(alerts, key=lambda a: SEVERITY_ORDER.get(a.get("severity"), 9))
    lines = []
    for a in ordered:
        head = f"{a.get('severity', '?')} · {a.get('type', '?')}"
        if a.get("ticker"):
            head += f" · {a['ticker']}"
        lines.append(head)
        lines.append(f"  {a.get('message', '')}")
        if a.get("action"):
            lines.append(f"  ACTION: {a['action']}")
        lines.append("")
    lines.append("— CFM dashboard alert engine")
    return "\n".join(lines)


class Notifier:
    """One delivery channel. Implementations must not raise out of send()."""

    name = "base"

    def configured(self) -> bool:
        raise NotImplementedError

    def send(self, subject: str, body: str, alerts: list[dict]) -> None:
        raise NotImplementedError


class EmailNotifier(Notifier):
    """SMTP email. Env: SMTP_HOST, SMTP_PORT (587), SMTP_USER, SMTP_PASSWORD,
    ALERT_EMAIL_TO, ALERT_EMAIL_FROM (defaults to SMTP_USER)."""

    name = "email"

    def configured(self) -> bool:
        return bool(os.environ.get("SMTP_HOST") and os.environ.get("ALERT_EMAIL_TO"))

    def send(self, subject: str, body: str, alerts: list[dict]) -> None:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = os.environ.get("ALERT_EMAIL_FROM") or os.environ.get("SMTP_USER", "")
        msg["To"] = os.environ["ALERT_EMAIL_TO"]
        msg.set_content(body)
        host = os.environ["SMTP_HOST"]
        port = int(os.environ.get("SMTP_PORT", "587"))
        user = os.environ.get("SMTP_USER")
        password = os.environ.get("SMTP_PASSWORD")
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.starttls()
            if user and password:
                smtp.login(user, password)
            smtp.send_message(msg)


class NtfyNotifier(Notifier):
    """Push via ntfy.sh (or a self-hosted ntfy server). Env: ALERT_NTFY_TOPIC,
    optional ALERT_NTFY_SERVER (default https://ntfy.sh)."""

    name = "ntfy"

    def configured(self) -> bool:
        return bool(os.environ.get("ALERT_NTFY_TOPIC"))

    def send(self, subject: str, body: str, alerts: list[dict]) -> None:
        server = (os.environ.get("ALERT_NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
        topic = os.environ["ALERT_NTFY_TOPIC"]
        priority = "urgent" if any(a.get("severity") == "CRITICAL" for a in alerts) else \
                   "high" if any(a.get("severity") == "HIGH" for a in alerts) else "default"
        requests.post(f"{server}/{topic}", data=body.encode("utf-8"),
                      headers={"Title": subject, "Priority": priority, "Tags": "rotating_light"},
                      timeout=20)


class LogNotifier(Notifier):
    """Dry-run / fallback channel: writes to the process log, always available."""

    name = "log"

    def configured(self) -> bool:
        return True

    def send(self, subject: str, body: str, alerts: list[dict]) -> None:
        logger.warning("ALERTS (not sent): %s\n%s", subject, body)


CHANNELS: list[Notifier] = [EmailNotifier(), NtfyNotifier()]


def dispatch(alerts: list[dict], settings: dict | None = None,
             dry_run: bool = False) -> list[dict]:
    """Send one batch of new alerts to every configured+enabled channel.

    Returns a delivery report (one entry per channel attempted). Never raises —
    a broken SMTP server must not sink the evaluator run that produced the
    alerts (they are already persisted to state by then).
    """
    if not alerts:
        return []
    settings = settings or {}
    channel_enabled = settings.get("channels") or {}
    subject = format_subject(alerts)
    body = format_body(alerts)

    report = []
    if dry_run:
        LogNotifier().send(subject, body, alerts)
        return [{"channel": "log", "ok": True, "dry_run": True}]

    for ch in CHANNELS:
        if not ch.configured() or channel_enabled.get(ch.name) is False:
            continue
        try:
            ch.send(subject, body, alerts)
            report.append({"channel": ch.name, "ok": True})
        except Exception as e:  # noqa: BLE001 — delivery failure must not raise
            logger.error("alert delivery via %s failed: %s", ch.name, e)
            report.append({"channel": ch.name, "ok": False, "error": str(e)})
    if not report:  # nothing configured — keep the alerts visible somewhere
        LogNotifier().send(subject, body, alerts)
        report.append({"channel": "log", "ok": True})
    return report
