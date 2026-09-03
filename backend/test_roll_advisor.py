"""roll_advisor.roll_readiness — the advisory-only "clear to roll early" signal
surfaced in the Roll modal. Pure function: no state, no I/O."""
import config
import roll_advisor as ra


def test_unmeasured_inputs_give_none_not_false():
    # Neither leg priced -> unmeasured, never a false "not ready".
    out = ra.roll_readiness(None, None, 5)
    assert out["ready"] is None
    assert out["reasons"] == []


def test_below_both_thresholds_is_not_ready():
    out = ra.roll_readiness(40.0, 8.0, 5)
    assert out["ready"] is False
    assert out["reasons"] == []


def test_decay_threshold_alone_triggers_ready():
    out = ra.roll_readiness(config.ROLL_READY_DECAY_PCT, 8.0, 5)
    assert out["ready"] is True
    assert out["reasons"] == ["DECAY_CAPTURED"]


def test_decay_just_under_threshold_does_not_trigger():
    out = ra.roll_readiness(config.ROLL_READY_DECAY_PCT - 0.1, 8.0, 5)
    assert out["ready"] is False
    assert out["reasons"] == []


def test_itm_buffer_threshold_alone_triggers_ready():
    out = ra.roll_readiness(40.0, config.ROLL_READY_ITM_FLOOR_PCT - 0.1, 5)
    assert out["ready"] is True
    assert out["reasons"] == ["ITM_BUFFER_THIN"]


def test_itm_buffer_at_floor_does_not_trigger():
    # Strictly LESS than the floor, not <=.
    out = ra.roll_readiness(40.0, config.ROLL_READY_ITM_FLOOR_PCT, 5)
    assert out["ready"] is False


def test_both_triggers_report_both_reasons():
    out = ra.roll_readiness(95.0, 1.0, 5)
    assert out["ready"] is True
    assert out["reasons"] == ["DECAY_CAPTURED", "ITM_BUFFER_THIN"]


def test_none_itm_buffer_means_not_itm_and_only_decay_can_fire():
    # Caller passes None when the short isn't ITM (an OTM short has no "buffer"
    # in this sense) — must never be treated as "thin buffer" by accident.
    out = ra.roll_readiness(95.0, None, 5)
    assert out["ready"] is True
    assert out["reasons"] == ["DECAY_CAPTURED"]


def test_never_authoritative_shape():
    # No key here could be mistaken for veto/block machinery.
    out = ra.roll_readiness(95.0, 1.0, 5)
    assert out["advisory"] is True
    assert "veto" not in out and "blocking" not in out
