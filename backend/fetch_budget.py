"""How long a provider fetch may keep trying — decided by WHO is waiting.

The retry knobs used to be one set (`config.SCHWAB_MAX_RETRIES` and friends), and
``schwab_api._request``'s docstring said so out loud: *"so on-demand and
background fetches behave alike"*. That is the wrong goal. The two callers want
opposite things:

  * A **background** sweep (the warm scan, the alert scheduler, `data_transport`)
    has nobody waiting. Spending 87 seconds to eventually get real data beats
    giving up and writing a stale frame. Patience is free.
  * An **interactive** request has a human on the other end who leaves. The
    frontend aborts at 60s (`frontend/src/api.js` ``TIMEOUT_MS``); the patient
    budget for ONE symbol is 4 attempts x 20s timeout + 1+2+4s of backoff = **87
    seconds**. It cannot even fit inside the window it is being spent in. The
    request does not fail fast, it fails *late*, after the human is gone.

That is what turned a dead provider into a hung dashboard: three requests for the
same ticker queued behind one 87-second fetch on ``data_handler``'s per-symbol
lock, and the browser gave up before any of them finished.

So the budget is now a property of the CALLER, carried on a context variable:

    with fetch_budget.interactive():
        ...                       # every fetch below is bounded

**PATIENT is the default.** Nothing changes for code that does not opt in — the
scheduler, the warm sweep, every CLI helper and the whole test suite keep the
exact knobs they had. The interactive budget is opted into in one place
(``app._fetch_budget_gate``, on every HTTP request) and opted back OUT in one
place (``executor.execute``, below).

Two layers, because one is not enough:

  1. **Per attempt** — fewer attempts, a shorter HTTP timeout, briefer backoff.
     Bounds a single symbol (87s -> ~21s worst case).
  2. **A whole-request DEADLINE** — an absolute monotonic instant, past which no
     new provider call is attempted at all. Layer 1 alone does not bound a
     request: ``screening.entry_gate`` fans out to SPY, eleven sector ETFs, the
     peer benchmark and the name itself, so a per-symbol bound still multiplies
     by ~13. The deadline is what makes the REQUEST bounded rather than the call.

Past the deadline a fetch does not raise — it degrades to the cached frame, which
is what ``data_handler.get_daily`` already does on provider failure. A late answer
built on this morning's bars beats a spinner that never resolves; the app already
surfaces staleness (``/api/data-health``, ``STALE_BLOCKS_GO``) so a degraded read
is visible rather than silent.

ORDER PLACEMENT IS DELIBERATELY EXEMPT. ``executor.execute`` re-enters the patient
budget for its whole body, so the reads that price and verify a live order keep
the full retry budget even though a human is waiting on them. Money moving is the
one place where correctness outranks latency, and the operator will wait.
"""
from __future__ import annotations

import contextlib
import contextvars
import time
from dataclasses import dataclass

import config

PATIENT = "patient"
INTERACTIVE = "interactive"


@dataclass(frozen=True)
class Budget:
    """A resolved retry budget. Built per read from ``config`` rather than frozen
    at import, so a test (or an operator env override) that changes a knob is
    honoured by the very next fetch."""

    name: str
    attempts: int
    base_seconds: float
    max_seconds: float
    timeout: float | None
    deadline: float | None = None    # time.monotonic() instant, or None = never

    @property
    def interactive(self) -> bool:
        return self.name == INTERACTIVE

    def expired(self) -> bool:
        """True once the whole-request deadline has passed. A budget with no
        deadline never expires — that is the patient budget's whole point."""
        return self.deadline is not None and time.monotonic() >= self.deadline

    def remaining(self) -> float | None:
        """Seconds left on the deadline (never negative), or None if unbounded."""
        if self.deadline is None:
            return None
        return max(0.0, self.deadline - time.monotonic())

    def cap_timeout(self, caller_timeout):
        """Narrow a call site's own HTTP timeout to fit this budget.

        Only ever NARROWS: a call site asking for 20s under the patient budget
        still gets 20s. It is capped by both the per-attempt timeout and whatever
        is left on the deadline, so the last attempt before a deadline cannot
        overshoot it.
        """
        candidates = [t for t in (caller_timeout, self.timeout, self.remaining())
                      if t is not None]
        return min(candidates) if candidates else None

    def sleep_for(self, wait: float) -> float:
        """Clamp a backoff sleep so it cannot run past the deadline. Sleeping
        through the deadline would burn the request's remaining time on waiting
        rather than on the one more attempt it might have afforded."""
        left = self.remaining()
        return wait if left is None else max(0.0, min(wait, left))


def patient_budget() -> Budget:
    """The existing knobs, unchanged. Every background caller gets this."""
    return Budget(
        name=PATIENT,
        attempts=max(1, int(config.SCHWAB_MAX_RETRIES)),
        base_seconds=float(config.SCHWAB_BACKOFF_BASE_SECONDS),
        max_seconds=float(config.SCHWAB_BACKOFF_MAX_SECONDS),
        timeout=None,          # the call site's own timeout stands
        deadline=None,         # patience is the point
    )


def interactive_budget(deadline_seconds: float | None = None) -> Budget:
    """Bounded enough to answer before the browser gives up."""
    if deadline_seconds is None:
        deadline_seconds = float(config.INTERACTIVE_DEADLINE_SECONDS)
    return Budget(
        name=INTERACTIVE,
        attempts=max(1, int(config.INTERACTIVE_MAX_RETRIES)),
        base_seconds=float(config.INTERACTIVE_BACKOFF_BASE_SECONDS),
        max_seconds=float(config.INTERACTIVE_BACKOFF_MAX_SECONDS),
        timeout=float(config.INTERACTIVE_TIMEOUT_SECONDS),
        deadline=time.monotonic() + deadline_seconds,
    )


_current: contextvars.ContextVar[Budget | None] = contextvars.ContextVar(
    "fetch_budget", default=None)


def current() -> Budget:
    """The budget in force. PATIENT when nothing opted in — so a caller that is
    never annotated behaves exactly as it did before this module existed."""
    return _current.get() or patient_budget()


def set_current(budget: Budget):
    """Install a budget, returning the token to reset with. For a framework hook
    that cannot hold a ``with`` block open across its scope (Flask's
    before_request / teardown_request pair); prefer the context managers."""
    return _current.set(budget)


def reset(token) -> None:
    _current.reset(token)


@contextlib.contextmanager
def interactive(deadline_seconds: float | None = None):
    """Bound every fetch in this block, and start the request deadline now."""
    token = _current.set(interactive_budget(deadline_seconds))
    try:
        yield
    finally:
        _current.reset(token)


@contextlib.contextmanager
def patient():
    """Restore the full retry budget inside a block that is otherwise
    interactive. Used by ``executor.execute`` — see the module docstring."""
    token = _current.set(patient_budget())
    try:
        yield
    finally:
        _current.reset(token)


def propagate(fn):
    """Wrap ``fn`` so it runs under the CALLER's budget when a pool thread runs it.

    A ``ThreadPoolExecutor`` worker starts with an empty context, so without this
    ``data_handler.get_many`` would silently drop an interactive request back to
    the patient budget — the exact hang this module exists to prevent, reappearing
    in the batch path only.

    This captures the BUDGET, not a ``contextvars.Context``. A copied Context is
    single-entry: ``executor.map`` hands the same wrapped callable to every worker,
    so ``ctx.run`` from eight threads at once raises "cannot enter context: ... is
    already entered". Re-setting one variable per call is reentrant and needs no
    coordination between workers.
    """
    budget = _current.get()

    def _run(*a, **kw):
        token = _current.set(budget)
        try:
            return fn(*a, **kw)
        finally:
            _current.reset(token)

    return _run
