"""Hysteresis control law for a mains humidifier — pure, no I/O.

The relay is the actuator of an appliance that can over-humidify a room, so
every rule here is a *safety* rule first:

* **Stale or missing humidity** -> OFF.  The controller reads the room through
  the LMAO server; if the reporting node stops, the last value must not be
  trusted forever (a humidifier that runs on a frozen 40 % reading never
  stops).
* **Continuous-run cap + cooldown** -> OFF after ``max_on_s``, and refuse to
  restart for ``cooldown_s``.  A stuck sensor that keeps reading dry must not
  run the appliance all day.
* **Daily budget** -> OFF once the day's accumulated ON time is spent.
* **Minimum on/off times** -> the relay is not cycled faster than it can
  tolerate.

The caller renews the socket's lease on every ON cycle (``lease_s``), so the
socket itself is the deadman if this process dies.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Setpoints:
    """Control band + safety limits (all times in seconds).

    The band is derived from the room's own history, not from a comfort table.
    Over 14 days of RoboDog home reports (2026-11-04..11-19, n=8873) the room
    ran: p10 35.1 %, median 37.4 %, p75 39.4 %, p95 42.6 %, max 46.3 %.  So
    ``low`` is the dry decile (act only on the room's own dry tail) and
    ``high`` is the upper quartile rounded up -- the level the room already
    reaches on a good day.  40 % also keeps the dew point at ~11.4 C at the
    room's usual 26 C, under the 12.8 C mould line; 45 % at 26 C is 13.1 C
    (already over it), which is why the old 55 % ceiling was both unreachable
    (the room never exceeded 46.3 %) and physically wrong.

    ``max_on_s`` must be long enough to cross the band: the humidifier adds
    ~3 %/h (measured over a 34 min ON run: 37.4 -> 39.0), so 5 points needs
    ~1.7 h.  ``max_daily_on_s`` covers the deepest observed excursion
    (26.2 % -> 40 % = 4.6 h at that rate).
    """

    low_pct: int = 35  # turn ON at/below this humidity
    high_pct: int = 40  # turn OFF at/above this humidity
    stale_s: int = 900  # no fresh sample for this long -> OFF
    min_on_s: int = 60  # shortest ON run (relay protection)
    min_off_s: int = 120  # shortest OFF run (relay protection)
    max_on_s: int = 2 * 3600  # longest continuous ON run (band crossing)
    cooldown_s: int = 600  # forced OFF after a max_on trip
    max_daily_on_s: int = 5 * 3600
    lease_s: int = 300  # lease granted to the socket on each ON cycle

    def validate(self) -> None:
        if not (0 <= self.low_pct < self.high_pct <= 100):
            raise ValueError(
                f"setpoints must satisfy 0 <= low < high <= 100 "
                f"(low={self.low_pct}, high={self.high_pct})"
            )
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


@dataclass(frozen=True)
class Decision:
    relay_on: bool
    reason: str
    fail_safe: bool = False


def decide(
    sp: Setpoints,
    st: ControlState,
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

    # ---- fail-safe: no trustworthy humidity -> OFF ----
    if humidity_pct is None:
        switch(False)
        return Decision(False, "no humidity data", fail_safe=True)
    if not (0 <= humidity_pct <= 100):
        switch(False)
        return Decision(False, f"implausible humidity {humidity_pct}%", fail_safe=True)
    if sample_age_s is None or sample_age_s > sp.stale_s:
        switch(False)
        age = "unknown" if sample_age_s is None else f"{sample_age_s:.0f}s"
        return Decision(False, f"stale humidity ({age})", fail_safe=True)

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

    # ---- hysteresis ----
    if st.relay_on:
        if humidity_pct >= sp.high_pct:
            if (now_s - st.on_since_s) >= sp.min_on_s:
                switch(False)
                return Decision(False, f"humidity {humidity_pct}% >= {sp.high_pct}%")
            return Decision(True, f"min on time (humidity {humidity_pct}%)")
        return Decision(True, f"humidifying ({humidity_pct}% < {sp.high_pct}%)")

    if humidity_pct <= sp.low_pct:
        if (now_s - st.off_since_s) >= sp.min_off_s:
            switch(True)
            return Decision(True, f"humidity {humidity_pct}% <= {sp.low_pct}%")
        return Decision(False, f"min off time (humidity {humidity_pct}%)")
    if humidity_pct >= sp.high_pct:
        # Already OFF and at/above the ceiling: nothing to do, but do not
        # report this as "inside the band" (it is the band's top edge).
        return Decision(False, f"humidity {humidity_pct}% >= {sp.high_pct}%")
    return Decision(False, f"in band ({sp.low_pct}% < {humidity_pct}% < {sp.high_pct}%)")
