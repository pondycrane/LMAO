"""Dew-point control law for a mains humidifier — pure, no I/O.

**Why dew point, not relative humidity.**  RH is a ratio: at fixed absolute
moisture it moves ~2-3 points for every degree the room's temperature swings.
The room's own history shows it (14 days, 2026-11-05..11-19, n=8615 paired
minutes): T 21.5-33.6 C (mean 24.3), RH p10 35.0 / p50 37.1 / p75 38.6, dew
point p10 6.8 / p25 7.5 / p50 8.6 / p75 9.6 / p90 10.8 C, corr(T, RH) -0.53.
A 5-point RH band therefore duty-cycles the humidifier on the *heater's*
schedule rather than on dryness: the relay closes whenever the room warms,
and the appliance is asked to fight a transient it cannot win.  Dew point is
the absolute moisture the room holds; it is what condensation and mould
depend on, and it does not move when the thermostat does.

**The band.**  ``low``/``high`` come from the indoor comfort target, not from
the room's own history.  Hold 40-50 % RH across the room's whole temperature
swing (21.5-33.6 C observed; 22.6-26.1 C in the last day), so the dew point
must be high enough for 40 % RH even at the *warmest* temperature: 26.1 C at
40 % RH is 11.4 C dew.  ``high`` = 11.5 C therefore holds 40.2 % at 26.1 C,
45.0 % at 24.2 C and 49.5 % at 22.6 C, and stays under the 12 C winter
window-condensation line.  ``low`` = 7.5 C (4 C hysteresis) is the floor that
starts the appliance: 34.6 % RH at 24.2 C.

Do **not** take the band from the room's own dew-point deciles (p10 6.8 /
p75 9.6 C).  That history is the room *without* humidification, so adopting
it enshrines the chronic dryness as the target — a self-fulfilling setpoint
that never turns the appliance on.  Nor convert an RH threshold at one
temperature: RH and dew point are anti-correlated here (r = -0.53) — a warm
room reads a low RH while holding *more* moisture — so a single-point
conversion lands on a different regime than intended.

``rh_ceiling_pct`` is the physical backstop: a room cold enough that the
ceiling dew point is 60 % RH (below ~19.5 C at 11.5 C dew) must not be
humidified further — that is condensation on windows and walls.

Every rule is a *safety* rule first:

* **Stale or missing room data** -> OFF.  The controller reads the room through
  the LMAO server; if the reporting node stops, the last value must not be
  trusted forever (a humidifier that runs on a frozen reading never stops).
  A missing temperature is also missing data: without it there is no dew point.
* **Continuous-run cap + cooldown** -> OFF after ``max_on_s``, and refuse to
  restart for ``cooldown_s``.  A stuck sensor that keeps reading dry must not
  run the appliance all day.  The cap is a runaway guard, not the normal stop:
  the dew ceiling normally ends a run.  Crossing the 4 C band takes ~2.4 h
  at the measured dial (+1.7 C dew/h) and ~7.3 h at the low dial (+0.55 C/h),
  so 3 h covers the measured dial and splits the low-dial crossing into runs
  with a 10 min cooldown.
* **Daily budget** -> OFF once the day's accumulated ON time is spent.  It is
  a runaway guard sized from the physics, not a comfort cap: holding the band
  needs a duty cycle of ``L/(L+G)`` where ``G`` is the measured net rise
  (+1.7 C dew/h) and ``L`` the room's own moisture loss (0.5 C/h typical,
  0.7 C/h at the p10 of its hourly drift), i.e. ~5.5-7 h/day, plus a ~2.4 h
  ramp from the dry floor to the ceiling on the first run of a dry day:
  ~8-9.5 h.  ``10 h`` covers that and still bounds a pathological day; the
  2.5 L tank covers 10 h at the unit's 190 mL/h maximum (1.9 L).
* **Minimum on/off times** -> the relay is not cycled faster than it can
  tolerate.

The caller renews the socket's lease on every ON cycle (``lease_s``), so the
socket itself is the deadman if this process dies.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Magnus coefficients (WMO formulation) for water over the domestic range.
_MAGNUS_A = 17.62
_MAGNUS_B = 243.12


def dew_point_c(temp_c: float, humidity_pct: float) -> float:
    """Dew point in degrees Celsius from air temperature and RH."""
    gamma = math.log(humidity_pct / 100.0) + _MAGNUS_A * temp_c / (_MAGNUS_B + temp_c)
    return _MAGNUS_B * gamma / (_MAGNUS_A - gamma)


@dataclass(frozen=True)
class Setpoints:
    """Control band + safety limits (temperatures in C, times in seconds)."""

    low_dew_c: float = 7.5  # turn ON at/below this dew point (34.6 % RH at 24.2 C)
    high_dew_c: float = 11.5  # turn OFF at/above this dew point (40-50 % RH across the swing)
    rh_ceiling_pct: int = 60  # hard OFF: condensation risk in a cold room
    stale_s: int = 900  # no fresh sample for this long -> OFF
    min_on_s: int = 60  # shortest ON run (relay protection)
    min_off_s: int = 120  # shortest OFF run (relay protection)
    max_on_s: int = 3 * 3600  # longest continuous ON run (runaway guard)
    cooldown_s: int = 600  # forced OFF after a max_on trip
    max_daily_on_s: int = 10 * 3600
    lease_s: int = 300  # lease granted to the socket on each ON cycle

    def validate(self) -> None:
        if not (self.low_dew_c < self.high_dew_c):
            raise ValueError(
                f"setpoints must satisfy low_dew_c < high_dew_c "
                f"(low={self.low_dew_c}, high={self.high_dew_c})"
            )
        if not (0 < self.rh_ceiling_pct <= 100):
            raise ValueError(f"rh_ceiling_pct must be in (0, 100] (got {self.rh_ceiling_pct})")
        for name in (
            "stale_s",
            "min_on_s",
            "min_off_s",
            "max_on_s",
            "cooldown_s",
            "max_daily_on_s",
            "lease_s",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.lease_s < 2:
            raise ValueError(
                "lease_s must be >= 2: the socket opens the relay when the "
                "lease expires, and a 1 s lease cannot survive a poll cycle"
            )


@dataclass
class ControlState:
    """Mutable history the decision needs (persisted across cycles)."""

    relay_on: bool = False
    on_since_s: float = 0.0
    off_since_s: float = 0.0
    on_accum_s: float = 0.0  # completed ON runs today
    day_index: int = -1
    cooldown_until_s: float = 0.0

    def on_total_s(self, now_s: float) -> float:
        """Today's ON time including the run in progress."""
        live = (now_s - self.on_since_s) if self.relay_on else 0.0
        return self.on_accum_s + live

    # ---- persistence ----------------------------------------------------
    #
    # A controller restart must not reset the daily budget or a cooldown: a
    # crash loop would otherwise run the appliance indefinitely on a dry
    # reading.  The caller writes this dict after every decision.

    def to_dict(self) -> dict:
        return {
            "relay_on": self.relay_on,
            "on_since_s": self.on_since_s,
            "off_since_s": self.off_since_s,
            "on_accum_s": self.on_accum_s,
            "day_index": self.day_index,
            "cooldown_until_s": self.cooldown_until_s,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ControlState:
        """Rebuild state from ``to_dict`` output; unknown keys are ignored."""
        if not isinstance(data, dict):
            raise ValueError("state must be a JSON object")
        return cls(
            relay_on=bool(data.get("relay_on", False)),
            on_since_s=float(data.get("on_since_s", 0.0)),
            off_since_s=float(data.get("off_since_s", 0.0)),
            on_accum_s=float(data.get("on_accum_s", 0.0)),
            day_index=int(data.get("day_index", -1)),
            cooldown_until_s=float(data.get("cooldown_until_s", 0.0)),
        )


@dataclass(frozen=True)
class Decision:
    relay_on: bool
    reason: str
    fail_safe: bool = False


def reconcile(st: ControlState, socket_relay: bool, now_s: float) -> bool:
    """Adopt the socket's actual relay into ``st`` at startup; True if changed.

    The socket is authoritative, not the persisted state: its lease expired
    while this process was down, so a persisted ``relay_on=True`` is history.
    Counting the downtime as ON time would spend the daily budget on minutes
    the appliance never ran, and leaving ``relay_on`` stale would make the
    next cycle believe it is already humidifying.
    """
    changed = st.relay_on != socket_relay
    st.relay_on = socket_relay
    if socket_relay:
        st.on_since_s = now_s
    return changed


def decide(
    sp: Setpoints,
    st: ControlState,
    temp_c: float | None,
    humidity_pct: int | None,
    sample_age_s: float | None,
    now_s: float,
    day_index: int,
) -> Decision:
    """Apply the control law for one cycle, mutating ``st`` in place."""

    if day_index != st.day_index:
        st.day_index = day_index
        st.on_accum_s = 0.0

    def switch(on: bool) -> None:
        if on == st.relay_on:
            return
        if st.relay_on:
            st.on_accum_s += now_s - st.on_since_s
            st.off_since_s = now_s
        else:
            st.on_since_s = now_s
        st.relay_on = on

    # ---- fail-safe: no trustworthy room data -> OFF ----
    if humidity_pct is None or temp_c is None:
        switch(False)
        return Decision(False, "no room data", fail_safe=True)
    if not (0 <= humidity_pct <= 100):
        switch(False)
        return Decision(False, f"implausible humidity {humidity_pct}%", fail_safe=True)
    if not (-40.0 <= temp_c <= 80.0):
        switch(False)
        return Decision(False, f"implausible temperature {temp_c:.1f}C", fail_safe=True)
    if sample_age_s is None or sample_age_s > sp.stale_s:
        switch(False)
        age = "unknown" if sample_age_s is None else f"{sample_age_s:.0f}s"
        return Decision(False, f"stale room data ({age})", fail_safe=True)

    dew = dew_point_c(temp_c, humidity_pct)

    # ---- safety limits ----
    if st.relay_on and (now_s - st.on_since_s) >= sp.max_on_s:
        switch(False)
        st.cooldown_until_s = now_s + sp.cooldown_s
        return Decision(False, f"max continuous run ({sp.max_on_s}s)")
    if now_s < st.cooldown_until_s:
        switch(False)
        return Decision(False, "cooldown after max run")
    if st.on_total_s(now_s) >= sp.max_daily_on_s:
        switch(False)
        return Decision(False, f"daily budget spent ({sp.max_daily_on_s}s)")

    # ---- physical ceiling: never humidify a room near saturation ----
    if humidity_pct >= sp.rh_ceiling_pct:
        switch(False)
        return Decision(False, f"room humid ({humidity_pct}% >= {sp.rh_ceiling_pct}%)")

    # ---- hysteresis on absolute moisture ----
    if st.relay_on:
        if dew >= sp.high_dew_c:
            if (now_s - st.on_since_s) >= sp.min_on_s:
                switch(False)
                return Decision(False, f"dew {dew:.1f}C >= {sp.high_dew_c:.1f}C")
            return Decision(True, f"min on time (dew {dew:.1f}C)")
        return Decision(True, f"humidifying (dew {dew:.1f}C < {sp.high_dew_c:.1f}C)")

    if dew <= sp.low_dew_c:
        if (now_s - st.off_since_s) >= sp.min_off_s:
            switch(True)
            return Decision(True, f"dew {dew:.1f}C <= {sp.low_dew_c:.1f}C")
        return Decision(False, f"min off time (dew {dew:.1f}C)")
    if dew >= sp.high_dew_c:
        # Already OFF and at/above the ceiling: nothing to do, but do not
        # report this as "inside the band" (it is the band's top edge).
        return Decision(False, f"dew {dew:.1f}C >= {sp.high_dew_c:.1f}C")
    return Decision(False, f"in band ({sp.low_dew_c:.1f}C < {dew:.1f}C < {sp.high_dew_c:.1f}C)")
