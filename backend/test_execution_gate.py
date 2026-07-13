"""Tests for the market-settle execution gate (fully offline, injected clock).

Covers the Design-doc test matrix: per-action verdicts across the session
(pre-open, settle window, entry window, midday, close blackout, post-close),
early-close blackout shift, the gap-emergency unlock and its refusals, the
market-order block, PENDING-style executable_at stamping, and spread quality.

All timestamps are ET on 2026-07-13 (an ordinary Monday) unless a test names a
different date. Defaults in play: settle 30 min (blackout of entries/rolls until
10:00), entry earliest 60 min (entries until 10:30), close blackout 15 min
(from 15:45), gap ATR mult 2.0, emergency print min 5 min, spread mult 2.0.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import config
import execution_gate as eg
import session
from execution_gate import GateAction as GA
from execution_gate import GapContext, WindowReason

ET = ZoneInfo("America/New_York")

ALL_ORDER_ACTIONS = [GA.ENTRY, GA.ROLL_SHORT, GA.ROLL_LEAP, GA.DEFENSE, GA.EXIT_KILL]


def sess(h, mi, y=2026, mo=7, d=13):
    return session.session_state(datetime(y, mo, d, h, mi, tzinfo=ET))


def verdict(action, s, gap=None):
    return eg.execution_window(action, s.now_et, s, gap)


# ---- action classification ---------------------------------------------------

def test_classify_action_mapping():
    assert eg.classify_action("open_position_atomic") == GA.ENTRY
    assert eg.classify_action("buy_leap") == GA.ENTRY
    assert eg.classify_action("roll_leap") == GA.ROLL_LEAP
    assert eg.classify_action("close_position_atomic") == GA.EXIT_KILL
    assert eg.classify_action("close_leap") == GA.EXIT_KILL
    assert eg.classify_action("adjustment") is None


def test_classify_defense_vs_routine_roll():
    assert eg.classify_action("roll_short", {"roll_reason": "defend"}) == GA.DEFENSE
    assert eg.classify_action("roll_short", {"roll_reason": "scheduled"}) == GA.ROLL_SHORT
    assert eg.classify_action("roll_short", {"roll_reason": "75%-rule"}) == GA.ROLL_SHORT
    # Missing/unknown reason falls back to the stricter ROLL_SHORT (never emergency).
    assert eg.classify_action("roll_short", {}) == GA.ROLL_SHORT
    assert eg.classify_action("roll_short") == GA.ROLL_SHORT


# ---- full per-action verdict matrix ------------------------------------------

def test_settle_window_blocks_all_order_types():
    for hh, mm in [(9, 31), (9, 45)]:
        s = sess(hh, mm)
        for a in ALL_ORDER_ACTIONS:
            v = verdict(a, s)
            assert not v.allowed, (a, hh, mm)
            assert v.reason == WindowReason.SETTLE_WINDOW
        # entries defer to open+60 (10:30); rolls/defense/exit to open+30 (10:00).
        assert verdict(GA.ENTRY, s).executable_at == datetime(2026, 7, 13, 10, 30, tzinfo=ET)
        assert verdict(GA.ROLL_SHORT, s).executable_at == datetime(2026, 7, 13, 10, 0, tzinfo=ET)
        assert verdict(GA.EXIT_KILL, s).executable_at == datetime(2026, 7, 13, 10, 0, tzinfo=ET)
        assert verdict(GA.CANCEL, s).allowed


def test_at_settle_boundary_1000_rolls_clear_entry_still_blocked():
    s = sess(10, 0)   # exactly open+30
    for a in [GA.ROLL_SHORT, GA.ROLL_LEAP, GA.DEFENSE, GA.EXIT_KILL]:
        v = verdict(a, s)
        assert v.allowed and v.reason == WindowReason.OPEN, a
    ev = verdict(GA.ENTRY, s)
    assert not ev.allowed and ev.reason == WindowReason.ENTRY_WINDOW
    assert ev.executable_at == datetime(2026, 7, 13, 10, 30, tzinfo=ET)


def test_entry_window_1029_then_open_1030():
    s = sess(10, 29)
    assert not verdict(GA.ENTRY, s).allowed
    assert verdict(GA.ENTRY, s).reason == WindowReason.ENTRY_WINDOW
    for a in [GA.ROLL_SHORT, GA.DEFENSE, GA.EXIT_KILL]:
        assert verdict(a, s).allowed
    s2 = sess(10, 30)   # open+60 -> entry clears
    assert verdict(GA.ENTRY, s2).allowed and verdict(GA.ENTRY, s2).reason == WindowReason.OPEN


def test_midday_everything_allowed():
    s = sess(12, 30)
    for a in ALL_ORDER_ACTIONS + [GA.CANCEL]:
        v = verdict(a, s)
        assert v.allowed and v.executable_at is None, a


def test_close_blackout_boundary_344_open_346_blocked():
    ok = sess(15, 44)   # 16 min to close -> not blackout
    for a in ALL_ORDER_ACTIONS:
        assert verdict(a, ok).allowed, a
    blk = sess(15, 46)  # 14 min to close -> blackout
    for a in ALL_ORDER_ACTIONS:
        v = verdict(a, blk)
        assert not v.allowed and v.reason == WindowReason.CLOSE_BLACKOUT, a
    # Deferred to NEXT session after its settle window (07-14).
    assert verdict(GA.ROLL_SHORT, blk).executable_at == datetime(2026, 7, 14, 10, 0, tzinfo=ET)
    assert verdict(GA.ENTRY, blk).executable_at == datetime(2026, 7, 14, 10, 30, tzinfo=ET)
    assert verdict(GA.CANCEL, blk).allowed


def test_pre_open_and_post_close_market_closed():
    pre = sess(8, 0)
    for a in ALL_ORDER_ACTIONS:
        v = verdict(a, pre)
        assert not v.allowed and v.reason == WindowReason.MARKET_CLOSED, a
    # Pre-open on a trading day defers to TODAY's open + minimum.
    assert verdict(GA.ROLL_SHORT, pre).executable_at == datetime(2026, 7, 13, 10, 0, tzinfo=ET)
    assert verdict(GA.ENTRY, pre).executable_at == datetime(2026, 7, 13, 10, 30, tzinfo=ET)

    post = sess(16, 30)
    for a in ALL_ORDER_ACTIONS:
        assert verdict(a, post).reason == WindowReason.MARKET_CLOSED
    # Post-close defers to the NEXT session (07-14).
    assert verdict(GA.ROLL_SHORT, post).executable_at == datetime(2026, 7, 14, 10, 0, tzinfo=ET)


# ---- CANCEL is never gated (item 2) ------------------------------------------

def test_cancel_allowed_in_every_session_state():
    states = [
        sess(8, 0),                     # pre-open trading day
        sess(9, 31),                    # settle
        sess(12, 0),                    # midday
        sess(15, 46),                   # close blackout
        sess(16, 30),                   # post-close
        sess(12, 0, d=11),              # Saturday 2026-07-11 (weekend)
        sess(11, 0, y=2026, mo=1, d=1), # New Year's Day holiday
    ]
    for s in states:
        v = verdict(GA.CANCEL, s)
        assert v.allowed and v.executable_at is None


# ---- early-close blackout shift (item 1) -------------------------------------

def test_early_close_shifts_blackout_to_1245():
    # 2026-11-27 is the half-day after Thanksgiving (close 13:00). Blackout from 12:45.
    ok = sess(12, 44, y=2026, mo=11, d=27)   # 16 min to 13:00 close
    assert ok.is_early_close
    for a in ALL_ORDER_ACTIONS:
        assert verdict(a, ok).allowed, a
    blk = sess(12, 46, y=2026, mo=11, d=27)  # 14 min to close -> blackout
    for a in ALL_ORDER_ACTIONS:
        assert verdict(a, blk).reason == WindowReason.CLOSE_BLACKOUT, a
    # On an ORDINARY day 12:46 is midday, not blackout — proves the shift.
    assert verdict(GA.ROLL_SHORT, sess(12, 46)).allowed


# ---- gap-emergency unlock (items 3 & 4) --------------------------------------

def _emergency_gap(**kw):
    base = dict(adverse_gap_atr=2.5, two_sided_print_minutes=6.0, is_limit_order=True)
    base.update(kw)
    return GapContext(**base)


def test_gap_emergency_unlocks_defense_and_exit_at_936():
    s = sess(9, 36)
    for a in (GA.DEFENSE, GA.EXIT_KILL):
        v = verdict(a, s, _emergency_gap())
        assert v.allowed and v.emergency_path
        assert v.reason == WindowReason.GAP_EMERGENCY_UNLOCK


def test_gap_emergency_market_order_refused():
    s = sess(9, 36)
    v = verdict(GA.DEFENSE, s, _emergency_gap(is_limit_order=False))
    assert not v.allowed and v.reason == WindowReason.SETTLE_WINDOW


def test_gap_emergency_never_for_entry_or_routine_roll():
    s = sess(9, 36)
    assert not verdict(GA.ENTRY, s, _emergency_gap()).allowed
    assert not verdict(GA.ROLL_SHORT, s, _emergency_gap()).allowed
    assert not verdict(GA.ROLL_LEAP, s, _emergency_gap()).allowed
    assert verdict(GA.ENTRY, s, _emergency_gap()).reason == WindowReason.SETTLE_WINDOW


def test_gap_emergency_requires_two_sided_prints():
    s = sess(9, 36)
    # prints below the minimum -> no unlock.
    assert not verdict(GA.DEFENSE, s, _emergency_gap(two_sided_print_minutes=3.0)).allowed
    # prints unknown (fail-closed) -> no unlock.
    assert not verdict(GA.DEFENSE, s, _emergency_gap(two_sided_print_minutes=None)).allowed


def test_opening_range_confirmation_path():
    s = sess(9, 36)
    # Gap below the ATR threshold but a confirmed break of the opening-range low
    # (continuation) unlocks.
    broke = _emergency_gap(adverse_gap_atr=1.0, broke_opening_range_low=True)
    assert verdict(GA.DEFENSE, s, broke).allowed
    # A gap that stays inside the opening range (no break, below threshold) does NOT.
    filling = _emergency_gap(adverse_gap_atr=1.0, broke_opening_range_low=False)
    assert not verdict(GA.DEFENSE, s, filling).allowed


def test_no_gap_context_defense_blocked_in_settle():
    # No emergency inputs at all -> DEFENSE blocked in the settle window.
    v = verdict(GA.DEFENSE, sess(9, 40))
    assert not v.allowed and v.reason == WindowReason.SETTLE_WINDOW


def test_emergency_inputs_after_settle_are_irrelevant():
    # Post-settle, DEFENSE/EXIT are allowed on the normal path; no emergency tag.
    v = verdict(GA.DEFENSE, sess(11, 0), _emergency_gap())
    assert v.allowed and not v.emergency_path and v.reason == WindowReason.OPEN


# ---- market-order block (items 5) --------------------------------------------

def test_market_order_blocked_only_inside_settle():
    assert eg.market_order_blocked_now(sess(9, 45)) is True
    assert eg.market_order_blocked_now(sess(10, 0)) is False   # settle boundary cleared
    assert eg.market_order_blocked_now(sess(12, 0)) is False
    assert eg.market_order_blocked_now(sess(8, 0)) is False     # closed, not "at open"
    assert eg.market_order_blocked_now(sess(15, 46)) is False   # blackout is not the open


# ---- spread quality (item 8) -------------------------------------------------

def test_spread_wide_requires_ack_post_settle():
    v = eg.spread_quality(current_spread=0.30, baseline_spread=0.10, contracts=2)
    assert v.wide and v.warning == eg.SpreadWarning.WIDE_SPREAD
    assert v.requires_ack is True
    # excess = (0.30 - 0.10) * 2 contracts * 100 = 40.0
    assert v.est_excess_slippage_usd == 40.0


def test_spread_wide_on_emergency_shown_but_no_ack():
    v = eg.spread_quality(0.30, 0.10, contracts=2, emergency_path=True)
    assert v.wide and v.requires_ack is False
    assert v.warning == eg.SpreadWarning.WIDE_SPREAD


def test_spread_within_bound_no_warning():
    v = eg.spread_quality(0.15, 0.10, contracts=1)
    assert not v.wide and v.warning is None and not v.requires_ack


def test_spread_no_baseline_not_fabricated():
    v = eg.spread_quality(0.30, None, contracts=2)
    assert not v.has_baseline and v.warning == eg.SpreadWarning.NO_BASELINE
    assert v.est_excess_slippage_usd is None and not v.requires_ack


# ---- config provenance sanity ------------------------------------------------

def test_hard_rules_are_set():
    assert config.NO_MARKET_ORDERS_AT_OPEN is True
    assert config.EMERGENCY_NEVER_FOR_ENTRY is True
    assert config.CANCEL_NEVER_GATED is True
