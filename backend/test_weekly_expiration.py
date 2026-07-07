"""Tests for the weekly short-call expiration picker in option_chain.

CFM sells one weekly call, but IWM/SPY now list a daily expiration every trading
day. The picker must land the short on the coming Friday — or the Thursday
before it when that Friday is a market holiday — never on a Mon–Thu daily.
"""
import os
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cfm-weekly-exp-test-"))

import option_chain  # noqa: E402


def _c(exp, dte):
    return {"expiration": exp, "dte": dte, "strike": 100.0}


def test_boundary_true_for_plain_friday():
    assert option_chain._is_weekly_boundary("2026-07-10") is True   # Friday


def test_boundary_false_for_midweek_dailies():
    assert option_chain._is_weekly_boundary("2026-07-08") is False  # Wednesday
    assert option_chain._is_weekly_boundary("2026-07-09") is False  # Thursday (no holiday next day)


def test_boundary_false_for_holiday_friday_true_for_prior_thursday():
    # July 3 2026 is a Friday but a holiday (July 4 observed); the series expires
    # Thursday July 2 instead.
    assert option_chain._is_weekly_boundary("2026-07-03") is False
    assert option_chain._is_weekly_boundary("2026-07-02") is True
    # Good-Friday week: April 3 2026 is a holiday Friday -> Thursday April 2.
    assert option_chain._is_weekly_boundary("2026-04-03") is False
    assert option_chain._is_weekly_boundary("2026-04-02") is True


def test_picks_friday_over_nearer_dailies():
    # Tue/Wed/Thu/Fri dailies present -> pick Friday, skipping the nearer dailies.
    contracts = [_c("2026-07-07", 0), _c("2026-07-08", 1),
                 _c("2026-07-09", 2), _c("2026-07-10", 3), _c("2026-07-13", 6)]
    assert option_chain._weekly_expiration(contracts) == "2026-07-10"


def test_picks_thursday_when_friday_is_holiday():
    # Week of July 4 2026 (Fri July 3 is a holiday): Thu July 2 is the boundary.
    contracts = [_c("2026-06-30", 1), _c("2026-07-01", 2),
                 _c("2026-07-02", 3), _c("2026-07-06", 7)]
    assert option_chain._weekly_expiration(contracts) == "2026-07-02"


def test_excludes_zero_dte_boundary():
    # A Friday that is today (dte 0) is excluded; roll to the next Friday.
    contracts = [_c("2026-07-10", 0), _c("2026-07-17", 7)]
    assert option_chain._weekly_expiration(contracts) == "2026-07-17"


def test_falls_back_to_nearest_when_no_friday_boundary():
    # Monthly-only style chain with no upcoming Friday boundary listed -> nearest.
    contracts = [_c("2026-07-08", 1), _c("2026-07-09", 2)]
    assert option_chain._weekly_expiration(contracts) == "2026-07-08"


def test_none_when_no_dated_contracts():
    assert option_chain._weekly_expiration([_c("2026-07-07", 0)]) is None
    assert option_chain._weekly_expiration([]) is None
