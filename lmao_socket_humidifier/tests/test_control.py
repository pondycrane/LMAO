"""Control-law tests: dew-point hysteresis, safety limits, fail-safe behaviour."""

import pytest

from lmao_socket_humidifier.control import (
    ControlState,
    Setpoints,
    decide,
    dew_point_c,
    reconcile,
)

# The shipped band: 40-50 % RH across the room's 22.6-26.1 C swing, so the
# tests exercise production configuration.
SP = Setpoints(
    low_dew_c=7.5,
    high_dew_c=11.5,
    rh_ceiling_pct=60,
    stale_s=900,
    min_on_s=60,
    min_off_s=120,
    max_on_s=3 * 3600,
    cooldown_s=600,
    max_daily_on_s=10 * 3600,
    lease_s=300,
)

# A room at 24.2 C: RH 34 % -> 7.3 C dew (below the 7.5 C ON edge), RH 46 %
# -> 11.9 C (above the 11.5 C OFF edge).
T_MED = 24.2


def cycle(st, temp, hum, age, now, day=0, sp=SP):
    return decide(sp, st, temp, hum, age, now, day)


def test_dew_point_matches_the_magnus_reference():
    # 25 C / 50 % is the textbook 13.9 C dew point.
    assert dew_point_c(25.0, 50.0) == pytest.approx(13.86, abs=0.02)
    # 24.2 C / 34 % is below the ON edge (7.5 C); 46 % is above the OFF edge.
    assert dew_point_c(T_MED, 34) == pytest.approx(7.34, abs=0.05)
    assert dew_point_c(T_MED, 46) == pytest.approx(11.9, abs=0.05)
    # Saturated air: dew point == air temperature.
    assert dew_point_c(20.0, 100.0) == pytest.approx(20.0, abs=0.01)


def test_dew_point_is_temperature_invariant_at_fixed_moisture():
    """The whole point of the change: same absolute moisture, different T,
    same dew point -- so the relay does not follow the heater."""
    # 24.2 C / 35 % and 26.2 C / 31.4 % hold the same moisture.
    assert dew_point_c(26.2, 31.4) == pytest.approx(dew_point_c(24.2, 35.0), abs=0.15)


def test_dry_room_turns_on_after_min_off_time():
    st = ControlState()
    assert cycle(st, T_MED, 30, 5, 0).relay_on is False  # min_off not yet elapsed
    d = cycle(st, T_MED, 30, 5, 120)
    assert d.relay_on is True
    assert "<= 7.5C" in d.reason


def test_hysteresis_holds_between_setpoints():
    st = ControlState(relay_on=True, on_since_s=0.0)
    # 37 % at 24.2 C is 8.6 C dew: inside the band, stay ON.
    assert cycle(st, T_MED, 37, 5, 300).relay_on is True
    # 46 % is 11.9 C dew: OFF (the ceiling is 11.5).
    assert cycle(st, T_MED, 46, 5, 400).relay_on is False
    assert st.on_accum_s == pytest.approx(400.0)


def test_a_warm_room_does_not_trip_the_off_edge_by_temperature_alone():
    """RH 36 % at 30 C is 13.1 C dew -- far above the ceiling, so the relay
    stays OFF; the old RH law would have seen 36 % < 40 % and kept running."""
    st = ControlState(relay_on=False, off_since_s=0.0)
    d = cycle(st, 30.0, 36, 5, 600)
    assert d.relay_on is False
    assert "in band" not in d.reason
    assert ">= 11.5C" in d.reason


def test_a_cool_room_is_humidified_at_the_same_absolute_moisture():
    """RH 45 % at 21 C is 8.6 C dew: inside the band, so OFF -- even though
    45 % RH at 24.2 C would be 11.5 C dew, right at the ceiling."""
    st = ControlState(relay_on=False, off_since_s=0.0)
    assert cycle(st, 21.0, 45, 5, 600).relay_on is False


def test_off_stays_off_inside_the_band():
    st = ControlState(relay_on=False, off_since_s=0.0)
    assert cycle(st, T_MED, 37, 5, 600).relay_on is False


def test_off_at_the_top_edge_is_not_reported_as_in_band():
    st = ControlState(relay_on=False, off_since_s=0.0)
    d = cycle(st, T_MED, 40, 5, 600)
    assert d.relay_on is False
    assert "in band" not in d.reason
    assert ">= 11.5C" in d.reason


def test_min_on_time_delays_the_off_edge():
    st = ControlState(relay_on=True, on_since_s=1000.0)
    d = cycle(st, T_MED, 46, 5, 1030)
    assert d.relay_on is True
    assert "min on" in d.reason
    assert cycle(st, T_MED, 46, 5, 1060).relay_on is False


def test_stale_sample_forces_off():
    st = ControlState(relay_on=True, on_since_s=0.0)
    d = cycle(st, T_MED, 30, 901, 2000)
    assert d.relay_on is False
    assert d.fail_safe is True
    assert "stale" in d.reason


def test_missing_sample_forces_off():
    st = ControlState(relay_on=True, on_since_s=0.0)
    d = cycle(st, None, None, None, 2000)
    assert d.relay_on is False and d.fail_safe is True
    assert "no room data" in d.reason


def test_missing_temperature_forces_off():
    """No temperature -> no dew point -> no trustworthy reading."""
    st = ControlState(relay_on=True, on_since_s=0.0)
    d = cycle(st, None, 30, 5, 2000)
    assert d.relay_on is False and d.fail_safe is True


def test_unknown_age_is_treated_as_stale():
    st = ControlState(relay_on=False)
    d = cycle(st, T_MED, 30, None, 2000)
    assert d.relay_on is False and d.fail_safe is True


def test_implausible_reading_is_refused():
    st = ControlState(relay_on=True, on_since_s=0.0)
    for bad in (-1, 101, 250):
        d = cycle(st, T_MED, bad, 1, 2000)
        assert d.relay_on is False and d.fail_safe is True
    for bad_t in (-60.0, 200.0):
        d = cycle(st, bad_t, 40, 1, 2000)
        assert d.relay_on is False and d.fail_safe is True


def test_rh_ceiling_stops_a_cold_saturated_room():
    """A room cooled to 15 C at 62 % RH holds 7.7 C dew -- inside the band --
    but 62 % RH is condensation risk, so the humidifier must not run."""
    st = ControlState(relay_on=True, on_since_s=0.0)
    d = cycle(st, 15.0, 62, 5, 300)
    assert d.relay_on is False
    assert "room humid" in d.reason


def test_max_continuous_run_trips_a_cooldown():
    st = ControlState(relay_on=True, on_since_s=0.0)
    assert cycle(st, T_MED, 30, 5, 10799).relay_on is True
    d = cycle(st, T_MED, 30, 5, 10800)
    assert d.relay_on is False
    assert "max continuous" in d.reason
    # Even a bone-dry reading cannot restart it during the cooldown.
    assert cycle(st, T_MED, 10, 5, 10900).relay_on is False
    assert "cooldown" in cycle(st, T_MED, 10, 5, 10900).reason
    # After the cooldown the normal min_off gate applies (already elapsed).
    assert cycle(st, T_MED, 10, 5, 11400).relay_on is True


def test_daily_budget_stops_the_day():
    st = ControlState(relay_on=True, on_since_s=0.0, on_accum_s=10 * 3600 - 60, day_index=0)
    d = cycle(st, T_MED, 20, 5, 60)
    assert d.relay_on is False
    assert "daily budget" in d.reason


def test_day_rollover_resets_the_budget():
    st = ControlState(relay_on=False, off_since_s=0.0, on_accum_s=10 * 3600, day_index=1)
    d = cycle(st, T_MED, 20, 5, 200, day=2)
    assert st.on_accum_s == 0.0
    assert d.relay_on is True


def test_on_total_counts_the_live_run():
    st = ControlState(relay_on=True, on_since_s=100.0, on_accum_s=500.0)
    assert st.on_total_s(160.0) == pytest.approx(560.0)
    st.relay_on = False
    assert st.on_total_s(160.0) == pytest.approx(500.0)


def test_state_survives_a_round_trip():
    st = ControlState(
        relay_on=True,
        on_since_s=1000.0,
        off_since_s=900.0,
        on_accum_s=3600.0,
        day_index=739000,
        cooldown_until_s=5000.0,
    )
    restored = ControlState.from_dict(st.to_dict())
    assert restored == st


def test_state_from_dict_tolerates_a_partial_document():
    """An older/partial state file must not crash the controller."""
    assert ControlState.from_dict({}) == ControlState()
    assert ControlState.from_dict({"on_accum_s": 60}).on_accum_s == 60.0
    with pytest.raises(ValueError):
        ControlState.from_dict([1, 2, 3])


def test_setpoints_validate_rejects_inverted_band():
    with pytest.raises(ValueError, match="low_dew_c < high_dew_c"):
        Setpoints(low_dew_c=9.7, high_dew_c=7.8).validate()
    with pytest.raises(ValueError, match="lease_s"):
        Setpoints(lease_s=1).validate()
    with pytest.raises(ValueError, match="rh_ceiling_pct"):
        Setpoints(rh_ceiling_pct=0).validate()
    Setpoints().validate()


def test_reconcile_adopts_the_socket_and_does_not_bill_the_downtime():
    """A restart after the lease expired: state says ON, socket says OFF.

    The persisted ON window must not be added to the daily budget — the
    appliance was not running — and the next cycle must be free to start a
    fresh run."""
    st = ControlState(relay_on=True, on_since_s=0.0, on_accum_s=0.0, day_index=0)
    assert reconcile(st, False, 10_000.0) is True
    assert st.relay_on is False
    # No ON time was banked for the 10 000 s the process was down.
    assert st.on_total_s(10_000.0) == pytest.approx(0.0)


def test_reconcile_counts_a_run_already_in_progress_from_now():
    """The lease was renewed by another writer (or the pod restarted fast):
    the relay really is closed, so ON time starts now, not at the stale
    timestamp — otherwise a restart would charge minutes it never ran."""
    st = ControlState(relay_on=True, on_since_s=0.0, on_accum_s=0.0, day_index=0)
    assert reconcile(st, True, 10_000.0) is False
    assert st.on_since_s == pytest.approx(10_000.0)
    assert st.on_total_s(10_060.0) == pytest.approx(60.0)
