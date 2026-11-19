"""Control-law tests: hysteresis, safety limits, fail-safe behaviour."""

import pytest

from lmao_socket_humidifier.control import ControlState, Setpoints, decide

# The shipped band (data-derived: room p10..p75), so the tests exercise the
# production configuration rather than an arbitrary one.
SP = Setpoints(
    low_pct=35,
    high_pct=40,
    stale_s=900,
    min_on_s=60,
    min_off_s=120,
    max_on_s=2 * 3600,
    cooldown_s=600,
    max_daily_on_s=5 * 3600,
    lease_s=300,
)


def cycle(st, hum, age, now, day=0, sp=SP):
    return decide(sp, st, hum, age, now, day)


def test_dry_room_turns_on_after_min_off_time():
    st = ControlState()
    assert cycle(st, 30, 5, 0).relay_on is False  # min_off not yet elapsed
    d = cycle(st, 30, 5, 120)
    assert d.relay_on is True
    assert "<= 35%" in d.reason


def test_hysteresis_holds_between_setpoints():
    st = ControlState(relay_on=True, on_since_s=0.0)
    # 37 % is inside the band: stay ON.
    assert cycle(st, 37, 5, 300).relay_on is True
    # 40 %: OFF.
    assert cycle(st, 40, 5, 400).relay_on is False
    assert st.on_accum_s == pytest.approx(400.0)


def test_off_stays_off_inside_the_band():
    st = ControlState(relay_on=False, off_since_s=0.0)
    assert cycle(st, 37, 5, 600).relay_on is False


def test_off_at_the_top_edge_is_not_reported_as_in_band():
    """An already-OFF relay at >= high must not log the misleading
    "35% < 40% < 40%" comparison (40 is not inside the band)."""
    st = ControlState(relay_on=False, off_since_s=0.0)
    d = cycle(st, 40, 5, 600)
    assert d.relay_on is False
    assert "in band" not in d.reason
    assert ">= 40%" in d.reason


def test_min_on_time_delays_the_off_edge():
    st = ControlState(relay_on=True, on_since_s=1000.0)
    d = cycle(st, 40, 5, 1030)
    assert d.relay_on is True
    assert "min on" in d.reason
    assert cycle(st, 40, 5, 1060).relay_on is False


def test_stale_sample_forces_off():
    st = ControlState(relay_on=True, on_since_s=0.0)
    d = cycle(st, 30, 901, 2000)
    assert d.relay_on is False
    assert d.fail_safe is True
    assert "stale" in d.reason


def test_missing_sample_forces_off():
    st = ControlState(relay_on=True, on_since_s=0.0)
    d = cycle(st, None, None, 2000)
    assert d.relay_on is False and d.fail_safe is True
    assert "no humidity" in d.reason


def test_unknown_age_is_treated_as_stale():
    st = ControlState(relay_on=False)
    d = cycle(st, 30, None, 2000)
    assert d.relay_on is False and d.fail_safe is True


def test_implausible_reading_is_refused():
    st = ControlState(relay_on=True, on_since_s=0.0)
    for bad in (-1, 101, 250):
        d = cycle(st, bad, 1, 2000)
        assert d.relay_on is False and d.fail_safe is True


def test_max_continuous_run_trips_a_cooldown():
    st = ControlState(relay_on=True, on_since_s=0.0)
    assert cycle(st, 30, 5, 7199).relay_on is True
    d = cycle(st, 30, 5, 7200)
    assert d.relay_on is False
    assert "max continuous" in d.reason
    # Even a bone-dry reading cannot restart it during the cooldown.
    assert cycle(st, 10, 5, 7300).relay_on is False
    assert "cooldown" in cycle(st, 10, 5, 7300).reason
    # After the cooldown the normal min_off gate applies (already elapsed).
    assert cycle(st, 10, 5, 7800).relay_on is True


def test_daily_budget_stops_the_day():
    st = ControlState(relay_on=True, on_since_s=0.0, on_accum_s=5 * 3600 - 60, day_index=0)
    d = cycle(st, 20, 5, 60)
    assert d.relay_on is False
    assert "daily budget" in d.reason


def test_day_rollover_resets_the_budget():
    st = ControlState(relay_on=False, off_since_s=0.0, on_accum_s=5 * 3600, day_index=1)
    # A new day clears the accumulator, so a dry room may run again.
    d = cycle(st, 20, 5, 200, day=2)
    assert st.on_accum_s == 0.0
    assert d.relay_on is True


def test_on_total_counts_the_live_run():
    st = ControlState(relay_on=True, on_since_s=100.0, on_accum_s=500.0)
    assert st.on_total_s(160.0) == pytest.approx(560.0)
    st.relay_on = False
    assert st.on_total_s(160.0) == pytest.approx(500.0)


def test_setpoints_validate_rejects_inverted_band():
    with pytest.raises(ValueError, match="low < high"):
        Setpoints(low_pct=60, high_pct=50).validate()
    with pytest.raises(ValueError, match="lease_s"):
        Setpoints(lease_s=1).validate()
    Setpoints().validate()
