"""Monthly payout tracker tests — per-month net-juice derivation, the
finalizable signal (last short of the month closed vs still open, calendar
month-end fallback), the finalize / mark-paid bookkeeping with amount
snapshotting, the PAYOUT_READY alert, and the migration seed."""
import os
import tempfile

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-test-"))

import alerts  # noqa: E402
import config  # noqa: E402
import logging_handler as log  # noqa: E402
import migrations  # noqa: E402
import payouts  # noqa: E402


@pytest.fixture()
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    return tmp_path


def _close(ticker, date, net):
    return {"action": "close_short", "ticker": ticker, "date": date,
            "net_juice_total": net}


def _pos_with_short(ticker, expiration):
    """An open position carrying one open short leg expiring on ``expiration``."""
    return {"ticker": ticker, "status": "open",
            "short_calls": [{"strike": 100, "contracts": 5, "open_date": "2026-07-01",
                             "expiration": expiration, "dte": 5}]}


def _seed(monkeypatch, execs, positions=None, cur_month="2026-07", burn=None):
    """Write a state file with the given executions/positions and pin 'now'.
    ``burn`` is an optional {month: leap_burn} map stubbed in for the burn marks."""
    monkeypatch.setattr(payouts, "_cur_month", lambda: cur_month)
    monkeypatch.setattr(payouts, "monthly_leap_burn", lambda: dict(burn or {}))
    state = log.load_state()
    state["executions"] = execs
    state["positions"] = positions or []
    log.save_state(state)
    return state


# --- derivation ------------------------------------------------------------
def test_monthly_net_juice_buckets_close_shorts(isolated_state, monkeypatch):
    state = _seed(monkeypatch, [
        _close("ON", "2026-05-08T15:00:00Z", 420.0),
        _close("ON", "2026-05-15T15:00:00Z", 380.5),
        _close("AA", "2026-06-05T15:00:00Z", 510.0),
        _close("AA", "2026-07-02T15:00:00Z", 110.0),
        {"action": "buy_leap", "ticker": "ON", "date": "2026-05-01T15:00:00Z"},
    ])
    by_month = payouts.monthly_net_juice(state)
    assert by_month["2026-05"] == {"net_juice": 800.5, "closes": 2}
    assert by_month["2026-06"] == {"net_juice": 510.0, "closes": 1}
    assert by_month["2026-07"] == {"net_juice": 110.0, "closes": 1}
    assert set(by_month) == {"2026-05", "2026-06", "2026-07"}


def test_close_with_bad_date_falls_back_to_expiration(isolated_state, monkeypatch):
    # A rolled/expiry close whose `date` isn't a parseable YYYY-MM-DD used to be
    # DROPPED from the payout (its juice vanished) while the History per-week table
    # relabelled it to 'today'. Both now bucket by the expiration month instead.
    state = _seed(monkeypatch, [
        {"action": "close_short", "ticker": "XLK", "date": "NoneT20:00:00Z",
         "expiration": "2026-07-10", "net_juice_total": 253.0},
    ])
    by_month = payouts.monthly_net_juice(state)
    assert by_month == {"2026-07": {"net_juice": 253.0, "closes": 1}}


def test_history_and_payouts_reconcile_for_rolled_closes(isolated_state, monkeypatch):
    # The reported bug: History showed net juice that Payouts never counted. With
    # shared date->expiration bucketing the two views must sum to the same figure.
    import logging_handler as log
    execs = [
        {"action": "close_short", "ticker": "XLK", "date": "2026-07-08",
         "expiration": "2026-07-10", "net_juice_total": 253.0,
         "extrinsic_sold": 0.57, "extrinsic_paid_back": 0.317, "contracts": 1},
        {"action": "close_short", "ticker": "XLK", "date": "bad-date",
         "expiration": "2026-07-17", "net_juice_total": 281.0,
         "extrinsic_sold": 0.281, "extrinsic_paid_back": 0.0, "contracts": 1},
    ]
    state = _seed(monkeypatch, execs)
    payout_july = payouts.monthly_net_juice(state)["2026-07"]["net_juice"]

    log.recompute_derived(state)
    weeks = state["theta_ledger"]["weeks"]
    history_july = sum(w["net_juice"] for w in weeks
                       if w["week"] != log.UNDATED and w["week"][:4] == "2026")
    assert payout_july == history_july == 534.0
    # And nothing silently landed in an 'undated' bucket.
    assert all(w["week"] != log.UNDATED for w in weeks)


def test_reversed_and_excluded_closes_dont_leak_into_payouts(isolated_state, monkeypatch):
    # The reported bug: Payouts totalled net juice that the History per-week table
    # never showed. The theta ledger drops reversed/excluded executions from the
    # derived replay; Payouts iterated the raw log and counted them, so a reversed
    # adoption or a manually-excluded fill inflated (or, with big buybacks, tanked)
    # the payout while History stayed correct. Both views must now key off the same
    # filtered executions.
    import logging_handler as log
    execs = [
        _close("XLK", "2026-07-08T15:00:00Z", 253.0),                    # counts
        {**_close("XLK", "2026-07-09T15:00:00Z", -517.0),                # reversed adoption
         "reversed_by": "rev-1"},
        {**_close("XLK", "2026-07-10T15:00:00Z", 999.0), "excluded": True},  # operator-excluded
        {"action": "adoption_reversal", "ticker": "XLK",                 # the reversal marker itself
         "date": "2026-07-09T16:00:00Z", "reverses_execution_id": "abc"},
    ]
    state = _seed(monkeypatch, execs)
    by_month = payouts.monthly_net_juice(state)
    assert by_month == {"2026-07": {"net_juice": 253.0, "closes": 1}}

    # And it reconciles with the theta ledger the History table renders.
    log.recompute_derived(state)
    history_july = sum(w["net_juice"] for w in state["theta_ledger"]["weeks"]
                       if w["week"] != log.UNDATED)
    assert payouts.view(state)["current"]["net_juice"] == history_july == 253.0


def test_view_current_estimate_previous_final(isolated_state, monkeypatch):
    state = _seed(monkeypatch, [
        _close("AA", "2026-06-05T15:00:00Z", 510.0),
        _close("AA", "2026-07-02T15:00:00Z", 110.0),
    ])
    v = payouts.view(state)
    assert v["current"]["month"] == "2026-07"
    assert v["current"]["estimated"] is True
    assert v["previous"]["month"] == "2026-06"
    assert v["previous"]["estimated"] is False
    assert v["previous"]["net_juice"] == 510.0
    assert v["totals"]["ytd"] == 620.0


# --- finalizable signal (the "last short of the month" rule) ---------------
def test_current_month_not_finalizable_while_short_open_this_month(isolated_state, monkeypatch):
    # An open short still expires in July -> July isn't done earning.
    state = _seed(monkeypatch, [_close("AA", "2026-07-02T15:00:00Z", 110.0)],
                  positions=[_pos_with_short("AA", "2026-07-31")])
    v = payouts.view(state)
    assert v["current"]["finalizable"] is False
    assert v["current"]["status"] == "in_progress"
    assert payouts.pending_finalization(state) is None


def test_current_month_finalizable_when_last_short_closed(isolated_state, monkeypatch):
    # The remaining open short rolled into an August expiry -> July's short
    # income is done; July becomes finalizable mid-month.
    state = _seed(monkeypatch, [_close("AA", "2026-07-02T15:00:00Z", 110.0)],
                  positions=[_pos_with_short("AA", "2026-08-07")])
    v = payouts.view(state)
    assert v["current"]["finalizable"] is True
    assert v["current"]["status"] == "finalizable"
    pending = payouts.pending_finalization(state)
    assert pending["month"] == "2026-07"
    assert pending["reason"] == "last_short_closed"


def test_current_month_finalizable_when_flat_on_shorts(isolated_state, monkeypatch):
    state = _seed(monkeypatch, [_close("AA", "2026-07-02T15:00:00Z", 110.0)],
                  positions=[{"ticker": "AA", "status": "open", "short_calls": []}])
    assert payouts.view(state)["current"]["finalizable"] is True


def test_previous_month_finalizable_by_calendar(isolated_state, monkeypatch):
    # June has income and the month has ended -> finalizable regardless of shorts.
    state = _seed(monkeypatch, [_close("AA", "2026-06-05T15:00:00Z", 510.0)],
                  positions=[_pos_with_short("AA", "2026-07-31")])
    v = payouts.view(state)
    assert v["previous"]["finalizable"] is True
    pending = payouts.pending_finalization(state)
    assert pending["month"] == "2026-06"
    assert pending["reason"] == "month_ended"


def test_short_expiry_falls_back_to_open_date_plus_dte(isolated_state, monkeypatch):
    # No stored expiration -> derived from open_date + dte (still July).
    pos = {"ticker": "AA", "status": "open",
           "short_calls": [{"strike": 100, "contracts": 5,
                            "open_date": "2026-07-20", "dte": 5, "expiration": None}]}
    state = _seed(monkeypatch, [_close("AA", "2026-07-02T15:00:00Z", 110.0)],
                  positions=[pos])
    assert payouts.has_open_short_expiring_in(state, "2026-07") is True
    assert payouts.view(state)["current"]["finalizable"] is False


# --- LEAP burn folded into the payout (leftover) ---------------------------
def test_payout_is_juice_minus_leap_burn(isolated_state, monkeypatch):
    state = _seed(monkeypatch, [
        _close("AA", "2026-06-05T15:00:00Z", 510.0),
        _close("AA", "2026-07-02T15:00:00Z", 300.0),
    ], burn={"2026-06": 120.0, "2026-07": 80.0})
    v = payouts.view(state)
    assert v["previous"]["net_juice"] == 510.0
    assert v["previous"]["leap_burn"] == 120.0
    assert v["previous"]["burn_tracked"] is True
    assert v["previous"]["net_payout"] == 390.0        # 510 − 120 leftover
    assert v["previous"]["payout_amount"] == 390.0
    assert v["current"]["net_payout"] == 220.0         # 300 − 80
    # YTD leftover = juice 810 − burn 200 = 610
    assert v["totals"]["ytd_juice"] == 810.0
    assert v["totals"]["ytd_burn"] == 200.0
    assert v["totals"]["ytd"] == 610.0


def test_payout_untracked_burn_degrades_to_juice(isolated_state, monkeypatch):
    state = _seed(monkeypatch, [_close("AA", "2026-06-05T15:00:00Z", 510.0)], burn={})
    prev = payouts.view(state)["previous"]
    assert prev["burn_tracked"] is False
    assert prev["leap_burn"] == 0.0
    assert prev["net_payout"] == 510.0                 # falls back to juice-only


def test_finalize_snapshots_leftover_and_breakdown(isolated_state, monkeypatch):
    _seed(monkeypatch, [_close("AA", "2026-06-05T15:00:00Z", 510.0)],
          burn={"2026-06": 120.0})
    v = payouts.finalize("2026-06")
    prev = v["previous"]
    assert prev["finalized_amount"] == 390.0           # leftover snapshotted
    assert prev["finalized_juice"] == 510.0
    assert prev["finalized_burn"] == 120.0
    assert prev["payout_amount"] == 390.0


def test_payout_ready_alert_reports_leftover(isolated_state, monkeypatch):
    state = _seed(monkeypatch, [_close("AA", "2026-06-05T15:00:00Z", 510.0)],
                  burn={"2026-06": 120.0})
    a = alerts.check_payout_ready(state)[0]
    assert a["data"]["net_payout"] == 390.0
    assert a["data"]["leap_burn"] == 120.0
    assert "390.00 leftover" in a["message"]
    assert "LEAP burn" in a["message"]


def test_monthly_realized_burn_from_marks(monkeypatch):
    import burn_marks
    marks = {
        "AA": [
            {"date": "2026-06-05", "extrinsic_now": 1000.0},
            {"date": "2026-06-12", "extrinsic_now": 900.0},   # −100 burn (Jun)
            {"date": "2026-07-03", "extrinsic_now": 850.0},   # −50 burn (Jul)
            {"date": "2026-07-10", "extrinsic_now": 880.0},   # +30 grew -> clamped 0
        ],
    }
    monkeypatch.setattr(burn_marks, "_load", lambda: marks)
    by_month = burn_marks.monthly_realized_burn()
    assert by_month["2026-06"] == 100.0
    assert by_month["2026-07"] == 50.0                 # the +30 week clamped to 0


# --- ITM→OTM intrinsic repayment -------------------------------------------
def _sell(ticker, strike, stock, contracts=1):
    return {"action": "sell_short", "ticker": ticker, "strike": strike,
            "contracts": contracts, "stock_price": stock}


def _close_full(ticker, strike, stock, date, net, contracts=1):
    return {"action": "close_short", "ticker": ticker, "strike": strike,
            "contracts": contracts, "stock_price": stock, "date": date,
            "net_juice_total": net}


def _roll_pair(ticker, strike, entry_stock, exit_stock, date, ext_sold, buyback,
               contracts=1, expiration=None):
    """A sell_short + close_short pair with full close economics, so net juice and
    intrinsic both derive from stored facts (mirrors the real broker rows)."""
    return [
        {"action": "sell_short", "ticker": ticker, "strike": strike,
         "contracts": contracts, "stock_price": entry_stock,
         "entry_extrinsic_per_share": ext_sold, "expiration": expiration},
        {"action": "close_short", "ticker": ticker, "strike": strike,
         "contracts": contracts, "stock_price": exit_stock, "date": date,
         "expiration": expiration, "close_price_per_share": buyback,
         "extrinsic_sold": ext_sold},
    ]


def test_intrinsic_melt_counts_itm_entry_closed_otm(isolated_state, monkeypatch):
    # Sold ITM (stock 103 > strike 100) and closed OTM at 98: the LEAP's UNHEDGED
    # loss is how far past the strike it closed = (100 − 98) × 1 × 100 = $200.
    state = _seed(monkeypatch, [
        _sell("AA", 100, 103, 1),
        _close_full("AA", 100, 98, "2026-07-08T15:00:00Z", 50.0, 1),
    ])
    assert payouts.monthly_intrinsic_melt(state) == {"2026-07": 200.0}


def test_intrinsic_melt_ignores_still_itm_and_otm_entry(isolated_state, monkeypatch):
    state = _seed(monkeypatch, [
        # Sold ITM but closed STILL ITM (105 > 100) — nothing given back.
        _sell("AA", 100, 103, 1),
        _close_full("AA", 100, 105, "2026-07-08T15:00:00Z", 50.0, 1),
        # Sold OTM (90 < 100) then closed OTM — never had a hedge to lose.
        _sell("BB", 100, 90, 1),
        _close_full("BB", 100, 88, "2026-07-09T15:00:00Z", 40.0, 1),
    ])
    assert payouts.monthly_intrinsic_melt(state) == {}


def test_intrinsic_repayment_carries_forward(isolated_state, monkeypatch):
    # June: $200 melted (100→98 past the strike), $100 juice -> repays $100, $100
    # debt carried, $0 leftover. July: $250 juice, no new melt -> repays the $100
    # carried, $150 leftover.
    state = _seed(monkeypatch, [
        _sell("AA", 100, 103, 1),                                   # ITM entry
        _close_full("AA", 100, 98, "2026-06-05T15:00:00Z", 100.0),  # OTM close -> $200 melt
        _sell("AA", 100, 90, 1),                                    # OTM entry
        _close_full("AA", 100, 88, "2026-07-02T15:00:00Z", 250.0),  # no melt
    ])
    v = payouts.view(state)
    june, july = v["previous"], v["current"]
    assert june["intrinsic_lost"] == 200.0
    assert june["intrinsic_repaid"] == 100.0
    assert june["intrinsic_debt"] == 100.0
    assert june["net_payout"] == 0.0            # all juice went to repayment
    assert july["intrinsic_repaid"] == 100.0    # carried debt repaid
    assert july["intrinsic_debt"] == 0.0
    assert july["net_payout"] == 150.0          # 250 − 100 leftover
    assert v["totals"]["ytd_intrinsic_repaid"] == 200.0
    assert v["totals"]["ytd"] == 150.0          # 350 juice − 200 repaid
    assert v["totals"]["intrinsic_debt"] == 0.0
    assert v["totals"]["all_time"] == 150.0


def test_intrinsic_debt_outstanding_when_juice_short(isolated_state, monkeypatch):
    # $200 melted but only $100 juice all-time -> $100 still owed, leftover floored.
    state = _seed(monkeypatch, [
        _sell("AA", 100, 103, 1),
        _close_full("AA", 100, 98, "2026-07-02T15:00:00Z", 100.0),
    ])
    v = payouts.view(state)
    assert v["current"]["net_payout"] == 0.0
    assert v["current"]["intrinsic_debt"] == 100.0
    assert v["totals"]["intrinsic_debt"] == 100.0


def test_intrinsic_debt_carries_through_empty_current_month(isolated_state, monkeypatch):
    # June left $100 owed; July (current) has no activity — the debt must still
    # surface on the current card and in totals, not read as $0.
    state = _seed(monkeypatch, [
        _sell("AA", 100, 103, 1),
        _close_full("AA", 100, 98, "2026-06-05T15:00:00Z", 100.0),  # $200 melt, $100 juice
    ])
    v = payouts.view(state)
    assert v["previous"]["intrinsic_debt"] == 100.0
    assert v["current"]["intrinsic_debt"] == 100.0      # carried into an empty July
    assert v["totals"]["intrinsic_debt"] == 100.0


def test_intrinsic_repayment_flag_off_degrades_to_juice(isolated_state, monkeypatch):
    monkeypatch.setattr(config, "PAYOUT_INTRINSIC_REPAYMENT", False)
    state = _seed(monkeypatch, [
        _sell("AA", 100, 103, 1),
        _close_full("AA", 100, 98, "2026-07-02T15:00:00Z", 100.0),
    ])
    v = payouts.view(state)
    assert v["current"]["intrinsic_repaid"] == 0.0
    assert v["current"]["net_payout"] == 100.0   # plain juice, no reservation


def test_intrinsic_melt_fifo_partial_close(isolated_state, monkeypatch):
    # Two ITM lots (2 + 1 contracts) closing OTM at 97; a 2-contract close consumes
    # the first lot FIFO -> (100 − 97) × 2 × 100 = $600, the 1-contract lot open.
    state = _seed(monkeypatch, [
        _sell("AA", 100, 103, 2),
        _sell("AA", 100, 103, 1),
        _close_full("AA", 100, 97, "2026-07-08T15:00:00Z", 80.0, 2),
    ])
    assert payouts.monthly_intrinsic_melt(state) == {"2026-07": 600.0}


def test_shared_melt_matches_theta_ledger_and_payouts(isolated_state, monkeypatch):
    # The per-week closes (theta ledger) and the monthly payout melt must derive
    # from the SAME per-close pairing so they can never disagree.
    execs = [
        _sell("AA", 100, 103, 1),                                   # ITM entry
        _close_full("AA", 100, 98, "2026-07-08T15:00:00Z", 50.0),   # OTM -> $200 melt
    ]
    state = _seed(monkeypatch, execs)
    by_idx = log.intrinsic_melt_by_close(execs)
    assert by_idx == {1: 200.0}                       # the close is index 1
    assert payouts.monthly_intrinsic_melt(state) == {"2026-07": 200.0}
    # And recompute_derived surfaces it on the week row, netting the per-week juice.
    log.recompute_derived(state)
    wk = next(w for w in state["theta_ledger"]["weeks"] if w["ticker"] == "AA")
    assert wk["net_juice"] == 50.0                     # raw extrinsic capture unchanged
    assert wk["intrinsic_covered"] == 200.0
    assert wk["net_juice_after_intrinsic"] == -150.0   # covers intrinsic first


def test_real_xlk_rolls_reconcile_to_expected_payout(isolated_state, monkeypatch):
    # The operator's actual three XLK July rolls (from the transactions table):
    #   roll_001 close 181 @ 2.49, entry 184.06 -> exit 179.27 (ITM->OTM)
    #   roll_002 close 176 @ 10.88, entry 179.27 -> exit 186.20 (ITM->ITM)
    #   roll_003 close 183 @ 2.66, entry 186.19 -> exit 182.53 (ITM->OTM)
    execs = (
        _roll_pair("XLK", 181, 184.06, 179.27, "2026-07-07T15:00:00Z", 1.91, 2.49) +
        _roll_pair("XLK", 176, 179.27, 186.20, "2026-07-09T15:00:00Z", 3.79, 10.88) +
        _roll_pair("XLK", 183, 186.19, 182.53, "2026-07-13T15:00:00Z", 2.81, 2.66)
    )
    state = _seed(monkeypatch, execs)
    # Net juice re-derived from the close facts: −58 + 311 + 15 = 268 (NOT 534 —
    # close 183's $2.66 buyback is subtracted, not booked as $0 paid back).
    assert payouts.monthly_net_juice(state)["2026-07"]["net_juice"] == 268.0
    # Intrinsic covered = unhedged move past the strike on the two OTM closes:
    #   181 − 179.27 = 1.73 -> $173 ; 183 − 182.53 = 0.47 -> $47 ; total $220.
    assert payouts.monthly_intrinsic_melt(state) == {"2026-07": 220.0}
    v = payouts.view(state)
    assert v["current"]["net_juice"] == 268.0
    assert v["current"]["intrinsic_lost"] == 220.0
    assert v["current"]["intrinsic_repaid"] == 220.0
    assert v["current"]["net_payout"] == 48.0          # 268 − 220 leftover
    assert v["current"]["intrinsic_debt"] == 0.0


def test_finalize_snapshots_intrinsic_repaid(isolated_state, monkeypatch):
    _seed(monkeypatch, [
        _sell("AA", 100, 103, 1),
        _close_full("AA", 100, 98, "2026-06-05T15:00:00Z", 250.0),
    ])
    v = payouts.finalize("2026-06")
    prev = v["previous"]
    # $200 melt, $250 juice -> repays $200, $50 leftover finalized.
    assert prev["finalized_intrinsic_repaid"] == 200.0
    assert prev["finalized_amount"] == 50.0
    assert prev["intrinsic_debt"] == 0.0


# --- finalize / mark paid --------------------------------------------------
def test_finalize_snapshots_amount(isolated_state, monkeypatch):
    _seed(monkeypatch, [_close("AA", "2026-06-05T15:00:00Z", 510.0)])
    v = payouts.finalize("2026-06")
    prev = v["previous"]
    assert prev["finalized"] is True
    assert prev["finalized_amount"] == 510.0
    assert prev["status"] == "finalized"
    assert prev["finalized_at"]
    assert v["totals"]["awaiting"] == 510.0


def test_cannot_finalize_month_still_earning(isolated_state, monkeypatch):
    state = _seed(monkeypatch, [_close("AA", "2026-07-02T15:00:00Z", 110.0)],
                  positions=[_pos_with_short("AA", "2026-07-31")])  # noqa: F841
    with pytest.raises(ValueError):
        payouts.finalize("2026-07")


def test_finalize_current_month_once_last_short_closed(isolated_state, monkeypatch):
    _seed(monkeypatch, [_close("AA", "2026-07-02T15:00:00Z", 110.0)],
          positions=[_pos_with_short("AA", "2026-08-07")])
    v = payouts.finalize("2026-07")
    assert v["current"]["finalized"] is True
    assert v["current"]["finalized_amount"] == 110.0


def test_finalized_amount_frozen_against_later_recompute(isolated_state, monkeypatch):
    _seed(monkeypatch, [_close("AA", "2026-06-05T15:00:00Z", 510.0)])
    payouts.finalize("2026-06")
    state = log.load_state()
    state["executions"].append(_close("AA", "2026-06-20T15:00:00Z", 90.0))
    log.save_state(state)
    v = payouts.view()
    assert v["previous"]["finalized_amount"] == 510.0   # frozen
    assert v["previous"]["net_juice"] == 600.0          # live figure moved
    assert v["previous"]["payout_amount"] == 510.0      # headlines the locked one


def test_mark_paid_finalizes_implicitly(isolated_state, monkeypatch):
    _seed(monkeypatch, [_close("AA", "2026-06-05T15:00:00Z", 510.0)])
    v = payouts.mark_paid("2026-06")
    prev = v["previous"]
    assert prev["finalized"] is True and prev["paid"] is True
    assert prev["paid_amount"] == 510.0
    assert prev["status"] == "paid"
    assert v["totals"]["paid_out"] == 510.0
    assert v["totals"]["awaiting"] == 0


def test_mark_paid_amount_override(isolated_state, monkeypatch):
    _seed(monkeypatch, [_close("AA", "2026-06-05T15:00:00Z", 510.0)])
    payouts.mark_paid("2026-06", amount=500, note="rounded down")
    rec = (log.load_state().get("payouts") or {}).get("records", {})["2026-06"]
    assert rec["paid_amount"] == 500.0
    assert rec["finalized_amount"] == 510.0
    assert rec["note"] == "rounded down"


def test_cannot_pay_month_still_earning(isolated_state, monkeypatch):
    _seed(monkeypatch, [_close("AA", "2026-07-02T15:00:00Z", 110.0)],
          positions=[_pos_with_short("AA", "2026-07-31")])
    with pytest.raises(ValueError):
        payouts.mark_paid("2026-07")


def test_unfinalize_clears_paid(isolated_state, monkeypatch):
    _seed(monkeypatch, [_close("AA", "2026-06-05T15:00:00Z", 510.0)])
    payouts.mark_paid("2026-06")
    v = payouts.unfinalize("2026-06")
    assert v["previous"]["finalized"] is False
    assert v["previous"]["paid"] is False
    assert v["totals"]["paid_out"] == 0


def test_unmark_paid_keeps_finalized(isolated_state, monkeypatch):
    _seed(monkeypatch, [_close("AA", "2026-06-05T15:00:00Z", 510.0)])
    payouts.mark_paid("2026-06")
    v = payouts.unmark_paid("2026-06")
    assert v["previous"]["paid"] is False
    assert v["previous"]["finalized"] is True
    assert v["previous"]["status"] == "finalized"


# --- pending / alert -------------------------------------------------------
def test_pending_none_when_finalized_or_no_income(isolated_state, monkeypatch):
    _seed(monkeypatch, [_close("AA", "2026-06-05T15:00:00Z", 510.0)])
    payouts.finalize("2026-06")
    assert payouts.pending_finalization(log.load_state()) is None
    state2 = _seed(monkeypatch, [_close("AA", "2026-06-05T15:00:00Z", -50.0)])
    assert payouts.pending_finalization(state2) is None


def test_payout_ready_alert_fires_and_resolves(isolated_state, monkeypatch):
    state = _seed(monkeypatch, [_close("AA", "2026-06-05T15:00:00Z", 510.0)])
    fired = alerts.check_payout_ready(state)
    assert len(fired) == 1
    a = fired[0]
    assert a["type"] == "PAYOUT_READY"
    assert a["fingerprint"] == "PAYOUT_READY|2026-06"
    assert a["action_url"] == "/?tab=Payouts"
    assert "June 2026" in a["message"]
    assert a["data"]["reason"] == "month_ended"
    payouts.finalize("2026-06")
    assert alerts.check_payout_ready(log.load_state()) == []


def test_payout_ready_fires_for_current_month_last_short_closed(isolated_state, monkeypatch):
    state = _seed(monkeypatch, [_close("AA", "2026-07-02T15:00:00Z", 110.0)],
                  positions=[_pos_with_short("AA", "2026-08-07")])
    fired = alerts.check_payout_ready(state)
    assert len(fired) == 1
    assert fired[0]["fingerprint"] == "PAYOUT_READY|2026-07"
    assert fired[0]["data"]["reason"] == "last_short_closed"


# --- migration -------------------------------------------------------------
def test_migration_seeds_payouts_key():
    old = {"schema_version": 14, "executions": [], "positions": []}
    migrated, changed = migrations.migrate(old)
    assert changed is True
    assert migrated["schema_version"] == migrations.CURRENT_VERSION
    assert migrated["payouts"] == {"records": {}}
