"""RS3M-vs-Sector removal — POSITIVE absence assertions.

Deleted coverage proves nothing: a removed rule and a rule that quietly came
back look identical to a test suite that simply stopped asking. These tests
assert the sector-relative logic CANNOT fire, on any input, at every layer it
used to live in — and that the SPY leg it sat beside is untouched.

See docs/decision-2026-08-21-remove-sector-rs.md for the decision and the
accepted safety trade-off.
"""
from __future__ import annotations

import inspect
import os
import subprocess
import tempfile

import pandas as pd
import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-sector-rm-"))

import config              # noqa: E402
import exit_reasons        # noqa: E402
import kill_switch         # noqa: E402
import rec_types           # noqa: E402
import rs_state            # noqa: E402
import scan_score          # noqa: E402
import scan_triggers       # noqa: E402
import screening           # noqa: E402
import stock_lights        # noqa: E402
from metrics import scorecard as sc     # noqa: E402
from metrics import thresholds as T     # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def _ramp(n, slope, start=100.0):
    return [start + slope * i for i in range(n)]


def _frame(values, vol=1e6):
    idx = pd.bdate_range("2024-01-01", periods=len(values))
    c = pd.Series(values, index=idx, dtype=float)
    return pd.DataFrame({"Open": c, "High": c + 1, "Low": c - 1, "Close": c,
                         "Volume": vol}, index=idx)


# ===========================================================================
# 1. Kill switch — the sector trigger cannot fire on ANY input
# ===========================================================================
def test_classify_takes_no_sector_argument():
    params = list(inspect.signature(kill_switch.classify).parameters)
    assert params == ["ticker", "rs_vs_spy"]
    with pytest.raises(TypeError):
        kill_switch.classify("AAPL", 1.0, -5.0)          # the old 3-arg call


def test_no_input_can_produce_a_sector_exit():
    """Sweep the whole sign grid. Nothing yields a sector verdict, a sector exit
    reason, or the immediate-exit wording."""
    for spy in (-9.0, -0.1, 0.0, 0.1, 4.9, 5.0, 25.0):
        v = kill_switch.classify("AAPL", spy)
        assert "rs3m_vs_sector" not in v
        assert "Sector" not in v["suggested_action"]
        assert "immediately" not in v["suggested_action"]
        assert kill_switch.exit_reason_code(v) != exit_reasons.ExitReason.KILL_SWITCH_SECTOR


def test_the_spy_leg_is_behaviourally_identical():
    """§1.3.1 — byte-for-byte parity on the retained leg: same threshold, same
    wording, same status at every boundary."""
    assert kill_switch.classify("A", -0.01)["status"] == "red"
    assert kill_switch.classify("A", -0.01)["alert"] is True
    assert "within 1-2 days" in kill_switch.classify("A", -0.01)["suggested_action"]
    assert "confirm on close" in kill_switch.classify("A", -0.01)["suggested_action"]
    # 0 is not negative -> not red. Below the thinning floor -> yellow, no alert.
    assert kill_switch.classify("A", 0.0)["status"] == "yellow"
    assert kill_switch.classify("A", 0.0)["alert"] is False
    assert kill_switch.classify("A", config.STOCK_RS_VS_SPY_MIN - 0.01)["status"] == "yellow"
    assert kill_switch.classify("A", config.STOCK_RS_VS_SPY_MIN)["status"] == "green"
    assert kill_switch.classify("A", None)["status"] == "green"
    assert config.STOCK_RS_VS_SPY_MIN == 5.0             # threshold untouched


def test_the_given_up_safety_surface_is_real_and_silent():
    """Scenario A from the decision record, pinned as an executable record of
    what was traded away: lagging its sector while beating SPY used to be an
    immediate exit and is now a clean hold. If this ever starts failing, someone
    has reinstated sector logic — read the decision record before "fixing" it."""
    v = kill_switch.classify("AAPL", 7.0)                # SPY strong, sector irrelevant
    assert v["status"] == "green" and v["alert"] is False
    assert kill_switch.exit_reason_code(v) is None


# ===========================================================================
# 2. Entry gate — no relative-strength veto remains
# ===========================================================================
def test_evaluate_vetoes_has_no_sector_leg_and_no_peer_frame():
    params = list(inspect.signature(stock_lights.evaluate_vetoes).parameters)
    assert params == ["df", "ivr_percentile", "is_etf"]      # no sector_df, no benchmark
    df = _frame(_ramp(230, 0.10))
    ids = {v["id"] for v in stock_lights.evaluate_vetoes(df, None, False)}
    assert ids == {"atr_expanding_high_ivr", "close_below_ma200"}


def test_a_severe_sector_laggard_is_not_vetoed():
    """The entry composition case from §1.4: every gate clean, sector RS deeply
    negative — the name is no longer vetoed for it."""
    stock = _frame(_ramp(230, 0.10))       # clean uptrend, 4/4 green
    sector = _frame(_ramp(230, 0.50))      # its sector ran 5x harder
    import indicators
    assert indicators.rs3m(stock, sector) < 0            # genuinely a laggard...
    res = stock_lights.compute(stock, ivr_percentile=None, is_etf=False)
    assert res["greens"] == 4
    assert res["vetoed"] is False                        # ...and no longer vetoed
    assert res["verdict"] == stock_lights.GREEN
    assert not any("sector" in r for r in res["veto_reasons"])


def test_no_sector_veto_id_can_reach_the_veto_registry():
    """The trigger KIND map this used to check went with the trigger machinery.
    The guarantee moved somewhere stronger: the veto REGISTRY is now the exhaustive
    list of everything that can block, so a sector-relative veto cannot exist
    without appearing in it."""
    import scan_verdict as sv
    for vid in sv.VETO_IDS:
        assert "sector" not in vid or vid == "account", vid
    assert not any("rs3m_vs_sector" in vid for vid in sv.VETO_IDS)

def test_suitability_has_no_sector_rule():
    src = inspect.getsource(sc.compute_verdict)
    assert "RS3M_VS_SECTOR_MIN" not in src
    assert not hasattr(T, "RS3M_VS_SECTOR_MIN")
    assert not hasattr(config, "STOCK_RS_VS_SECTOR_MIN")


def test_score_has_no_rs_component():
    assert not hasattr(scan_score, "W_RS_STATE")
    assert "rs_state_value" not in inspect.signature(scan_score.compute_score).parameters
    out = scan_score.compute_score(inst_flow=None, base_stage=None)
    assert "rs_state" not in out["parts"]
    assert 0.0 <= out["score"] <= 10.0        # still normalized after the weight drop


def test_the_shadow_rs_module_has_no_write_path_to_a_verdict():
    assert not hasattr(rs_state, "turning_watch_reason")
    assert not hasattr(rs_state, "WATCH_ANNOTATION")


def test_ranking_key_is_spy_for_stocks_and_etfs_alike(monkeypatch):
    import data_handler
    df = _frame(_ramp(230, 0.10))
    monkeypatch.setattr(data_handler, "get_daily", lambda s, force=False: df)
    for ticker in ("NVDA", "XLK"):
        row = screening._stock_row(ticker, df, "XLK", regime_green=True, sector_strong=True)
        assert row["rank_key"] == row["rs1m_vs_spy"]
        assert "rs1m_vs_sector" not in row and "rs3m_vs_sector" not in row


# ===========================================================================
# 4. Retired-but-readable persistence
# ===========================================================================
def test_the_retired_constants_still_exist_for_historical_reads():
    """Deleting them would break the History tab and the CSV export for every
    past sector exit. They must READ forever and never be written again."""
    assert exit_reasons.ExitReason.KILL_SWITCH_SECTOR == "KILL_SWITCH_SECTOR"
    assert rec_types.TriggerRule.KILL_RS_SECTOR == "KILL_RS_SECTOR"
    assert exit_reasons.is_valid("KILL_SWITCH_SECTOR") is True
    assert exit_reasons.ExitReason.KILL_SWITCH_SECTOR in exit_reasons.RETIRED
    assert rec_types.is_trigger("KILL_RS_SECTOR") is True


def test_no_new_close_can_be_stamped_with_the_retired_reason():
    assert exit_reasons.ExitReason.KILL_SWITCH_SECTOR not in exit_reasons.CLOSE_TIME
    assert exit_reasons.ExitReason.KILL_SWITCH_SPY in exit_reasons.CLOSE_TIME


def test_no_new_recommendation_can_be_emitted_with_the_retired_trigger():
    import recommendation_engine
    assert rec_types.TriggerRule.KILL_RS_SECTOR not in recommendation_engine._EXIT_PRIORITY
    assert rec_types.TriggerRule.KILL_RS_SPY_CONFIRMED in recommendation_engine._EXIT_PRIORITY


def test_the_sector_alert_type_is_gone():
    import alerts
    assert "KILL_SWITCH_SECTOR" not in alerts.ALERT_TYPES
    assert "KILL_SWITCH_SPY" in alerts.ALERT_TYPES


# ===========================================================================
# 5. Historical-event tolerance — old snapshots recompute without error
# ===========================================================================
def test_recompute_tolerates_snapshots_carrying_old_sector_fields():
    """§1.4 — a v3 entry snapshot still carries four vs-sector fields. Recompute
    reaches it only through entry_context.summary, a pure .get() read, so those
    fields are IGNORED, not tripped over, and the derived digest is correct."""
    import entry_context
    legacy = {
        "snapshot_schema_version": 3,
        "scorecard": {"verdict": "GO", "metrics": {"rs3m_vs_spy": 8.0,
                                                   "rs3m_vs_sector": -2.0}},
        "regime": {"status": "green"},
        "iv": {"iv_rank": 40.0},
        "stock": {"rs3m_vs_spy": 8.0, "rs3m_vs_sector": -2.0,
                  "rs1m_vs_sector": -1.0, "rs3m_vs_sector_method": "direct",
                  "rs3m_vs_sector_benchmark": "XLK"},
    }
    out = entry_context.summary(legacy)
    assert out["rs3m_vs_spy"] == 8.0          # the retained field still reads
    assert out["verdict"] == "GO" and out["regime"] == "green"
    assert "rs3m_vs_sector" not in out        # the removed one is simply not carried
    # A v4 snapshot (no sector fields at all) digests identically on what remains.
    modern = {**legacy, "snapshot_schema_version": 4,
              "stock": {"rs3m_vs_spy": 8.0}}
    assert entry_context.summary(modern)["rs3m_vs_spy"] == 8.0


def test_snapshot_schema_version_was_bumped():
    assert config.SNAPSHOT_SCHEMA_VERSION == 4


# ===========================================================================
# 6. Grep-level cleanliness (§1.4)
# ===========================================================================
_ALLOWED = {
    # the store of record for the decision itself
    "test_sector_rs_removed.py",
    # historical-tolerance / retirement sites, each commented as such
    "exit_reasons.py", "rec_types.py",
}


_IDENTS = {"rs3m_vs_sector", "rs1m_vs_sector",
           "STOCK_RS_VS_SECTOR_MIN", "RS3M_VS_SECTOR_MIN"}


def _live_identifiers(path):
    """Sector-RS identifiers that appear as CODE in one module.

    Parsed with ast rather than grepped, so the comments and docstrings that
    explain the removal — which are the whole point of a documented decision —
    are excluded structurally instead of by a fragile text heuristic. Docstrings
    are the one string kind skipped; every other literal counts, because a dict
    key or a field name IS live logic."""
    import ast
    tree = ast.parse(open(path, encoding="utf-8").read())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in _IDENTS:
            found.add(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in _IDENTS:
            found.add(node.attr)
        elif isinstance(node, ast.arg) and node.arg in _IDENTS:
            found.add(node.arg)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in docstrings and node.value in _IDENTS:
            found.add(node.value)
    return found


def test_no_stray_sector_rs_identifiers_in_production_code():
    """Every surviving mention must be prose explaining the removal, a
    retirement site, or a test asserting absence — never live logic."""
    offenders = {}
    for name in sorted(os.listdir(HERE)):
        if not name.endswith(".py") or name.startswith("test_") or name in _ALLOWED:
            continue
        live = _live_identifiers(os.path.join(HERE, name))
        if live:
            offenders[name] = sorted(live)
    for name in sorted(os.listdir(os.path.join(HERE, "metrics"))):
        if not name.endswith(".py"):
            continue
        live = _live_identifiers(os.path.join(HERE, "metrics", name))
        if live:
            offenders["metrics/" + name] = sorted(live)
    assert offenders == {}, f"live sector-RS logic survived: {offenders}"
