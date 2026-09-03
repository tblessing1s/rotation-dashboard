"""Canonical SPCX 2026-09-03 fixture — the live case the roll-dialog audit
(2026-09) was built from. Spot $151.34, ATR 6.51, regime YELLOW, current short
133C/8DTE mark $18.55 (bid $18.20/ask $18.90), full 8-DTE strike ladder 133-148
as displayed in the dialog. The 145-strike-across-weeks ladder is anchored on
the two REAL per-strike marks from the conversation (8 DTE mark $8.32, 15 DTE
mark $10.02); the remaining weeks (22/29/36/43 DTE) are constructed on a
smooth, decelerating extrinsic-growth curve fitted through those two real
points — illustrative, not independently verified live numbers, but shaped to
reproduce the real screenshot's qualitative finding (43 DTE is cheapest by net
debit) so the fixture exercises the exact bug this audit fixes.

No canonical (parquet-style) fixture previously exercised the roll dialog at
all — see the Phase 0 audit §0.7 item 24. This is the first one.
"""
import os
import tempfile

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-spcx-fixture-"))

import config            # noqa: E402
import data_handler       # noqa: E402
import executor          # noqa: E402
import logging_handler as log  # noqa: E402
import option_chain as oc  # noqa: E402
import roll_advisor as ra  # noqa: E402
import screening          # noqa: E402
import strike_policy      # noqa: E402

SPOT = 151.34
ATR = 6.51
BUYBACK_MARK = 18.55  # 133C mark at SPOT
INTRINSIC_133 = SPOT - 133.0  # 18.34

# The real 8-DTE ladder (2026-09-11), as displayed.
LADDER_8DTE = {
    133.0: (18.20, 18.90, 18.55),
    142.0: (10.20, 10.85, 10.52),
    143.0: (9.60, 10.00, 9.80),
    144.0: (8.85, 9.20, 9.02),
    145.0: (8.15, 8.50, 8.32),
    146.0: (7.50, 7.80, 7.65),
    147.0: (6.85, 7.15, 7.00),
    148.0: (6.20, 6.50, 6.35),
}

# 145-strike-across-weeks: (dte, mark). 8/15 DTE are the two real, independently
# observed marks; 22/29/36/43 DTE follow extrinsic(dte) = 1.98*sqrt(dte/8)*1.357
# (a curve fitted through the two real points) — see module docstring.
INTRINSIC_145 = SPOT - 145.0  # 6.34
def _extrinsic_145(dte):
    return 1.98 * (dte / 8) ** 0.5 * 1.357
WEEKS_145 = {
    "2026-09-11": (8, LADDER_8DTE[145.0][2]),                     # real: 8.32
    "2026-09-18": (15, 10.02),                                    # real: 10.02
    "2026-09-25": (22, round(_extrinsic_145(22) + INTRINSIC_145, 4)),
    "2026-10-02": (29, round(_extrinsic_145(29) + INTRINSIC_145, 4)),
    "2026-10-09": (36, round(_extrinsic_145(36) + INTRINSIC_145, 4)),
    "2026-10-16": (43, round(_extrinsic_145(43) + INTRINSIC_145, 4)),
}


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "_demo_mode", False)
    return tmp_path


def _chain_payload(spot):
    """The full mocked Schwab chain: 8-DTE ladder + 145-strike-across-weeks."""
    exp_map = {}
    exp_map["2026-09-11:8"] = {
        str(strike): [{"symbol": f"C{strike}", "strikePrice": strike, "daysToExpiration": 8,
                       "bid": b, "ask": a, "mark": m, "volatility": 30.0}]
        for strike, (b, a, m) in LADDER_8DTE.items()
    }
    for exp, (dte, mark) in WEEKS_145.items():
        if exp == "2026-09-11":
            continue  # already in the 8-DTE ladder above
        exp_map[f"{exp}:{dte}"] = {"145.0": [
            {"symbol": f"C145_{dte}", "strikePrice": 145.0, "daysToExpiration": dte,
             "bid": round(mark - 0.10, 2), "ask": round(mark + 0.10, 2), "mark": mark,
             "volatility": 30.0}]}
    return {"status": "SUCCESS", "underlyingPrice": spot, "callExpDateMap": exp_map}


def _mock_common(monkeypatch, spot, entry_extrinsic=1.53):
    monkeypatch.setattr(screening, "regime", lambda: {"status": "yellow"})
    df = __import__("pandas").DataFrame(
        {"Open": [spot] * 60, "High": [spot + ATR / 2] * 60, "Low": [spot - ATR / 2] * 60,
         "Close": [spot] * 60, "Volume": [1e6] * 60},
        index=__import__("pandas").bdate_range("2024-01-01", periods=60))
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: df)
    monkeypatch.setattr(data_handler, "latest_quote", lambda s: {"price": spot, "source": "t"})
    monkeypatch.setattr(log, "find_position", lambda s, t: {
        "position_type": "SHARES",
        "short_calls": [{"strike": 133.0, "contracts": 1, "dte": 8, "expiration": "2026-09-11",
                         "entry_premium_total": entry_extrinsic * 100,
                         "entry_extrinsic_per_share": entry_extrinsic}]})
    monkeypatch.setattr(oc, "_fetch_chain", lambda t, refresh=False: _chain_payload(spot))


def test_regime_target_is_near_138_not_145(store, monkeypatch):
    # YELLOW/2.0xATR: atr_strike = 151.34 - 13.02 = 138.32; itm_strike =
    # 151.34*0.97 = 146.80 -> deeper (138.32) wins, rounds to 138.5. NOT 145 —
    # 145 was the OLD STRIKE_TABLE (1.0xATR/conservative) target this audit
    # flags as undershooting the documented YELLOW rule.
    _mock_common(monkeypatch, SPOT)
    out = oc.roll_options("SPCX")
    assert out["regime"] == "yellow"
    assert out["atr_mult"] == 2.0
    rt = out["regime_target"]
    assert rt["rule_strike"] == pytest.approx(138.5)
    assert abs(rt["rule_strike"] - (SPOT - 2 * ATR)) <= 0.5  # within one rounding grid step
    assert out["suggested_strike"] == rt["rule_strike"]
    assert out["suggested_strike"] != 145.0


def test_week_ranking_picks_8dte_not_43dte_best_rate(store, monkeypatch):
    """The bug this audit fixes: net-debit ranking crowns the 43 DTE row
    'cheapest' (it always will be, for any fixed strike); juice/wk ranking
    correctly prefers 8 DTE (~1.15%/wk, matching the real conversation), tied
    within the parity band against 15 DTE and broken toward the shorter DTE."""
    _mock_common(monkeypatch, SPOT)
    out = oc.roll_options("SPCX")
    exp_145 = None
    rows = []
    for exp in out["expirations"]:
        s = next((s for s in exp["strikes"] if s["strike"] == 145.0), None)
        if s:
            rows.append({"expiration": exp["expiration"], "dte": exp["dte"],
                        "juice_per_week_pct": s["juice_per_week_pct"],
                        "net_credit": s["mark"] * 100 - BUYBACK_MARK * 100})
    assert len(rows) >= 3, "need at least the 8/15/43 DTE rows for this fixture"

    ranked = ra.rank_weeks_by_juice(rows)
    assert ranked["best"]["expiration"] == "2026-09-11"
    assert ranked["best"]["juice_per_week_pct"] == pytest.approx(1.15, abs=0.03)

    # The trap this fixes: net-debit alone crowns 43 DTE "cheapest" every time.
    cheapest_by_net_debit = max(rows, key=lambda r: r["net_credit"])
    assert cheapest_by_net_debit["expiration"] == "2026-10-16"
    assert cheapest_by_net_debit["expiration"] != ranked["best"]["expiration"]


def test_roll_up_guard_fires_above_133_not_at_133(store, monkeypatch):
    _mock_common(monkeypatch, SPOT)
    out = oc.roll_options("SPCX")
    row_145 = next(s for exp in out["expirations"] if exp["expiration"] == "2026-09-11"
                  for s in exp["strikes"] if s["strike"] == 145.0)
    guard = ra.roll_up_guard(
        current_strike=133.0, chosen_strike=145.0,
        earnings_in_week=False, ex_div_known=False, ex_div_in_week=None,
        chosen_juice_per_week_pct=row_145["juice_per_week_pct"],
        juice_floor_pct=out["weekly_juice_floor_pct"],
        operating_cash=out["operating_cash"], reserve_required=out["reserve_required"],
        net_credit=row_145["mark"] * 100 - BUYBACK_MARK * 100)
    assert guard is not None  # fires — rolling 133 -> 145 is a roll UP

    same_strike_guard = ra.roll_up_guard(
        current_strike=133.0, chosen_strike=133.0,
        earnings_in_week=False, ex_div_known=False, ex_div_in_week=None,
        chosen_juice_per_week_pct=0.1, juice_floor_pct=0.75,
        operating_cash=20000, reserve_required=13000, net_credit=0)
    assert same_strike_guard is None  # same strike is not a roll-up


def test_realized_extrinsic_on_close_and_ledger_isolation(store, monkeypatch):
    """Buyback at the real mark ($18.55) against the real spot ($151.34):
    extrinsic_paid_back = 18.55 - 18.34 = 0.21 — matching the live dialog's own
    displayed $0.21. realized_extrinsic = initial_extrinsic_sold (1.53, the
    "$153 sold / 82% captured" origin figure) - 0.21. The roll's net debit
    (-1023, matching the real screenshot) must not reach the accrual ledger."""
    entry_extrinsic = 1.53
    _mock_common(monkeypatch, SPOT, entry_extrinsic=entry_extrinsic)
    # Seed the position for a real executor roll (find_position is monkeypatched
    # for the dialog read above; _commit_roll reads/writes real state).
    state = log.load_state()
    state.setdefault("positions", []).append({
        "ticker": "SPCX", "status": "open", "position_type": "SHARES",
        "shares": {"count": 100, "cost_basis_per_share": 140.32},
        "short_calls": [{
            "strike": 133.0, "contracts": 1, "open_date": "2026-08-25",
            "expiration": "2026-09-11", "dte": 8,
            "entry_extrinsic_per_share": entry_extrinsic,
            "entry_premium_total": entry_extrinsic * 100 + INTRINSIC_133 * 100,
            "current_bid": BUYBACK_MARK, "current_cost": BUYBACK_MARK * 100,
        }],
    })
    log.save_state(state)

    new_mark = LADDER_8DTE[145.0][2]  # 8.32
    payload = {
        "from_strike": 133.0, "to_strike": 145.0,
        "close_price_per_share": BUYBACK_MARK,
        "premium_per_share": new_mark,
        "to_expiration": "2026-09-11", "to_dte": 8,
        "roll_strike_choice": {
            "regime": "yellow", "regime_target_strike": 138.5,
            "floor_strike": None, "chosen_strike": 145.0,
            "juice_per_week_at_chosen": ra.juice_per_week(new_mark, 145.0, SPOT, 8),
            "cushion_atr_at_chosen": ra.cushion_atr(145.0, SPOT, ATR),
        },
    }
    executor._commit_roll(payload, "SPCX", 1, SPOT, "logged", "test")

    result = log.load_state()
    close_exec = [e for e in result["executions"] if e.get("action") == "close_short"][-1]
    assert close_exec["extrinsic_paid_back"] == pytest.approx(0.21, abs=0.01)
    assert close_exec["extrinsic_sold"] == pytest.approx(entry_extrinsic)
    expected_realized = round((entry_extrinsic - close_exec["extrinsic_paid_back"]) * 100, 2)
    assert close_exec["net_juice_total"] == pytest.approx(expected_realized)

    net_debit = round((new_mark - BUYBACK_MARK) * 100, 2)
    assert net_debit == pytest.approx(-1023, abs=1)
    ytd = result["theta_ledger"]["totals"]["ytd"]
    assert ytd == pytest.approx(expected_realized)
    assert ytd != pytest.approx(net_debit)  # the roll's cash never reaches the ledger


def test_deadband_holds_default_strike_across_the_150_91_variant(store, monkeypatch):
    """Second variant, spot $150.91 (the later reading in the same live case):
    the DISPLAYED DEFAULT strike must not flip just because spot drifted a few
    cents — apply_deadband holds the prior default unless the continuous
    target moved more than 0.25xATR past it."""
    _mock_common(monkeypatch, SPOT)
    first = oc.roll_options("SPCX")
    prior_default = first["suggested_strike"]

    _mock_common(monkeypatch, 150.91)
    second = oc.roll_options("SPCX", prior_target=prior_default)
    assert second["target_deadband"]["held"] is True
    assert second["suggested_strike"] == prior_default

    # And the 133C extrinsic recomputes to ~$0.64 from the SAME mark ($18.55)
    # against the new spot — matching the live case's own observed mismatch.
    new_intrinsic = 150.91 - 133.0
    recomputed_extrinsic = round(BUYBACK_MARK - new_intrinsic, 2)
    assert recomputed_extrinsic == pytest.approx(0.64, abs=0.01)
