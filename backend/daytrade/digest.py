"""Daily performance digest — one push covering every enabled account.

Pushes each day-trading-enabled account's trial status once/day
(daytrade/scheduler.py's _maybe_daily_digest) so it reaches the operator
without checking the UI — an explicit "I want a daily update" ask, separate
from the trial-completion verdict itself (which this same message carries
once an account's trial is done). One combined message, not one push per
account: an operator running day-trade on two books wants one daily read,
not a notification flood.

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


def _account_label(account_id: str) -> str:
    try:
        import accounts
        acct = accounts.get(account_id)
        return (acct or {}).get("label") or account_id
    except Exception:  # noqa: BLE001 — a label lookup must never block the digest
        return account_id


def _account_fragment(account_id: str, status: dict) -> str:
    label = _account_label(account_id)
    progress = (f"trial complete — {status['verdict'].upper()}" if status["status"] == "complete"
                else f"{status['completed_trades']}/{status['target_trades']} trades")
    return f"{label}: {progress} ({status['net_r']:+.2f}R, ${status['net_pnl']:,.2f})"


def _format(accounts_status: dict[str, dict]) -> tuple[str, str]:
    if len(accounts_status) == 1:
        (account_id, status), = accounts_status.items()
        label = _account_label(account_id)
        subject = (f"Day Trade ({label}): trial complete — {status['verdict'].upper()}"
                   if status["status"] == "complete"
                   else f"Day Trade ({label}): {status['completed_trades']}/{status['target_trades']} trades")
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
                         "entries will be taken for this account. Going live is your "
                         "call — nothing here flips MODE automatically.")
        return subject, "\n".join(lines)

    complete = sum(1 for s in accounts_status.values() if s["status"] == "complete")
    subject = (f"Day Trade: all {len(accounts_status)} account(s) complete" if complete == len(accounts_status)
               else f"Day Trade: {len(accounts_status)} account(s) running ({complete} complete)")
    body = "\n".join(_account_fragment(aid, s) for aid, s in accounts_status.items())
    return subject, body


def send_daily_digest() -> list[dict]:
    """Push every enabled account's trial status to every configured
    channel, as one combined message. Returns [] (no channels touched) when
    no account has day-trading turned on. Best-effort per channel (one dead
    SMTP server must not block ntfy/webpush) and overall (the scheduler
    tick must survive a total failure here) — see daytrade/scheduler.py's
    _maybe_daily_digest, which already wraps this in its own try/except."""
    from daytrade import settings

    enabled = settings.enabled_account_ids()
    if not enabled:
        return []

    import notifier
    accounts_status = {aid: trial.trial_status(aid) for aid in enabled}
    subject, body = _format(accounts_status)

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
