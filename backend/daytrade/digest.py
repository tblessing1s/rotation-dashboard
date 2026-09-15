"""Daily performance digest.

Pushes the paper-trading trial's status once/day (daytrade/scheduler.py's
_maybe_daily_digest) so it reaches the operator without checking the UI —
an explicit "I want a daily update" ask, separate from the trial-completion
verdict itself (which this same message carries once the trial is done).

Reuses the SAME delivery channels CFM alerts use (notifier.CHANNELS —
email/ntfy/webpush, whichever is configured) rather than building a second
notification path, but does NOT go through notifier.dispatch() /
format_subject() / format_body(): those hardcode a "CFM {severity}" tag and
a ticker-scoped body built for POSITION alerts. This is the day-trade
sleeve, not CFM — tagging its digest "CFM" would be a real mix-up (the two
are deliberately kept separate everywhere else in this package), not just
cosmetic, so this module owns its own subject/body text and calls each
channel's send() directly.
"""
from __future__ import annotations

import logging

from daytrade import trial

logger = logging.getLogger("cfm.daytrade")


def _format(status: dict) -> tuple[str, str]:
    if status["status"] == "complete":
        subject = f"Day Trade: paper trial complete — {status['verdict'].upper()}"
    else:
        subject = f"Day Trade: {status['completed_trades']}/{status['target_trades']} trades"

    lines = [
        f"Trial progress: {status['completed_trades']}/{status['target_trades']} trades closed",
        f"Net R: {status['net_r']:+.2f}R",
        f"Net P&L: ${status['net_pnl']:,.2f}",
    ]
    if status["win_rate"] is not None:
        lines.append(f"Win rate: {status['win_rate']}%")
    if status["status"] == "complete":
        lines.append("")
        lines.append(f"Trial complete — {status['verdict'].upper()}. No new paper "
                     "entries will be taken. Going live is your call — nothing here "
                     "flips MODE automatically.")
    return subject, "\n".join(lines)


def send_daily_digest() -> list[dict]:
    """Push today's trial status to every configured channel. Best-effort
    per channel (one dead SMTP server must not block ntfy/webpush) and
    overall (the scheduler tick must survive a total failure here) — see
    daytrade/scheduler.py's _maybe_daily_digest, which already wraps this in
    its own try/except."""
    import notifier
    status = trial.trial_status()
    subject, body = _format(status)

    report = []
    for ch in notifier.CHANNELS:
        try:
            # configured() can do real work (WebPushNotifier's lazily
            # generates/loads VAPID keys, importing `cryptography`) — a
            # broken native dependency there raises straight out of a
            # library panic (observed: pyo3_runtime.PanicException, which
            # doesn't even subclass Exception), so this check gets the same
            # broad guard as send() below, not just an `if`.
            if not ch.configured():
                continue
        except BaseException as e:  # noqa: BLE001 — see above; must not block other channels
            logger.error("daytrade digest: %s.configured() failed: %s", ch.name, e)
            report.append({"channel": ch.name, "ok": False, "error": str(e)})
            continue
        try:
            ch.send(subject, body, [])
            report.append({"channel": ch.name, "ok": True})
        except BaseException as e:  # noqa: BLE001 — one channel's failure must not block others
            logger.error("daytrade digest delivery via %s failed: %s", ch.name, e)
            report.append({"channel": ch.name, "ok": False, "error": str(e)})
    if not report:
        logger.info("daytrade digest (no channel configured): %s\n%s", subject, body)
    return report
