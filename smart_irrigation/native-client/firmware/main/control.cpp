#include "control.h"

#include <string.h>

namespace sprout {

namespace {

// Per-plant optimum + dose limits.  Percentages are the sensor's own 0..100
// scale (100 % == probe submerged), NOT volumetric water content, so `target`
// values are horticultural starting points to be re-fitted from the logged
// decay curves (evaluation §4).  `pulse_on_ms` is 5000 everywhere because that
// is the only pulse width with a verified motor spin-up; sessions are kept
// short by `max_pulses` + `soak_ms`, not by sub-5 s pulses.
const PlantProfile kProfiles[] = {
    // name        target hyst pulse_ms soak_ms  pulses gap_ms     daily_ms
    {"kale",           45,   8,    5000,   90000,     1, 1800000UL,  60000UL},
    {"herbs",          40,   8,    5000,   90000,     2, 1800000UL,  60000UL},
    {"tomato",         55,  10,    5000,  120000,     3, 1200000UL,  90000UL},
    {"succulent",      20,   6,    5000,  120000,     1, 7200000UL,  30000UL},
    {"generic",        40,   8,    5000,   90000,     1, 1800000UL,  60000UL},
};
constexpr int kProfileCount = (int)(sizeof(kProfiles) / sizeof(kProfiles[0]));

// Default = kale: the plant the rig is currently installed on (Kale, 2026-09-18).
constexpr int kDefaultProfile = 0;

}  // namespace

int profile_count() { return kProfileCount; }

const PlantProfile& profile_at(int index) {
    if (index < 0 || index >= kProfileCount) index = kDefaultProfile;
    return kProfiles[index];
}

int profile_index_by_name(const char* name) {
    if (name == nullptr) return -1;
    for (int i = 0; i < kProfileCount; ++i) {
        if (strcmp(kProfiles[i].name, name) == 0) return i;
    }
    return -1;
}

int default_profile_index() { return kDefaultProfile; }

const char* state_name(State s) {
    switch (s) {
        case State::Idle:     return "idle";
        case State::Watering: return "watering";
        case State::Lockout:  return "lockout";
        case State::Fault:    return "fault";
    }
    return "?";
}

void Control::begin(const ControlState* persisted, uint32_t now_ms,
                    const ControlLimits& limits) {
    limits_ = limits;
    state_ = ControlState{};
    state_.magic = kStateMagic;
    profile_index_ = kDefaultProfile;
    if (persisted != nullptr && persisted->magic == kStateMagic) {
        state_ = *persisted;
        state_.magic = kStateMagic;
    }
    last_now_ms_ = now_ms;
    pump_on_ = false;
    have_pulsed_ = false;
    pulse_start_ms_ = 0;
    last_off_ms_ = 0;
    session_start_ms_ = 0;
    session_pump_ms_ = 0;
    pulses_done_ = 0;
    phase_ = State::Idle;
    dirty_ = false;
    window_clear();
}

void Control::set_profile_index(int index) {
    if (index < 0 || index >= kProfileCount) index = kDefaultProfile;
    profile_index_ = index;
}

const PlantProfile& Control::profile() const { return profile_at(profile_index_); }

uint32_t Control::effective_pulse_on_ms() const {
    uint32_t ms = profile().pulse_on_ms;
    if (ms < limits_.pump_min_on_ms) ms = limits_.pump_min_on_ms;
    if (ms > limits_.pulse_on_max_ms) ms = limits_.pulse_on_max_ms;
    return ms;
}

// The soak is at least the probe settle time, so the reading taken at the end
// of every soak is trustworthy by construction (this is what makes the
// closed-loop stop safe).
uint32_t Control::effective_soak_ms() const {
    const uint32_t ms = profile().soak_ms;
    return ms < limits_.probe_settle_ms ? limits_.probe_settle_ms : ms;
}

uint8_t Control::effective_max_pulses() const {
    const uint8_t n = profile().max_pulses;
    return n > limits_.max_pulses_ceiling ? limits_.max_pulses_ceiling : n;
}

uint32_t Control::effective_daily_cap_ms() const {
    const uint32_t ms = profile().max_daily_ms;
    return ms > limits_.daily_cap_max_ms ? limits_.daily_cap_max_ms : ms;
}

int Control::effective_window_samples() const {
    int n = (int)limits_.window_samples;
    if (n < 1) n = 1;
    if (n > kWindowMax) n = kWindowMax;
    return n;
}

void Control::window_clear() {
    window_count_ = 0;
    window_head_ = 0;
}

void Control::window_push(q8 v) {
    window_[window_head_] = v;
    window_head_ = (window_head_ + 1) % kWindowMax;
    if (window_count_ < kWindowMax) ++window_count_;
}

// Median of the settled samples in the window.  Insertion sort on a member
// scratch buffer: no allocation, no stack growth, ≤ 60 int32 per call at 1 Hz —
// and unlike a mean it survives up to half the window being a glitch.
q8 Control::window_median() {
    const int n = window_count_;
    if (n <= 0) return 0;
    for (int i = 0; i < n; ++i) sort_buf_[i] = window_[i];
    for (int i = 1; i < n; ++i) {
        const q8 key = sort_buf_[i];
        int j = i - 1;
        while (j >= 0 && sort_buf_[j] > key) {
            sort_buf_[j + 1] = sort_buf_[j];
            --j;
        }
        sort_buf_[j + 1] = key;
    }
    if (n % 2) return sort_buf_[n / 2];
    return (q8)(((int32_t)sort_buf_[n / 2 - 1] + (int32_t)sort_buf_[n / 2]) / 2);
}

bool Control::start_pulse(uint32_t uptime, Outputs& out) {
    if (pump_on_) return true;
    if (have_pulsed_ && uptime - last_off_ms_ < limits_.pump_min_off_ms) return false;
    pump_on_ = true;
    pulse_start_ms_ = uptime;
    ++pulses_done_;
    out.pump_changed = true;
    return true;
}

void Control::pump_off(uint32_t uptime, Outputs& out) {
    if (!pump_on_) return;
    pump_on_ = false;
    // The window holds pre-pulse soil; drop it so the post-pulse reading that
    // authorises the next pulse is made only of post-pulse samples.
    window_clear();
    const uint32_t width = uptime - pulse_start_ms_;
    session_pump_ms_ += width;
    state_.daily_used_ms += width;
    last_off_ms_ = uptime;
    have_pulsed_ = true;
    out.pump_changed = true;
    dirty_ = true;
}

void Control::end_session(uint32_t uptime, Outputs& out) {
    out.session_ended = true;
    out.session_elapsed_ms = uptime - session_start_ms_;
    out.session_pump_ms = session_pump_ms_;
    out.pulses_done = (uint8_t)pulses_done_;
    pulses_done_ = 0;
    session_pump_ms_ = 0;
    phase_ = State::Idle;
    state_.next_session_at_ms = uptime + profile().min_session_gap_ms;
    dirty_ = true;
}

Outputs Control::tick(const Inputs& in) {
    // 1. Advance the awake-time clock.  Wrap-safe 32-bit delta on the caller's
    //    monotonic ms; a jump larger than the daily window is treated as 0 so a
    //    bogus clock cannot manufacture elapsed time (and therefore watering).
    uint32_t dt = in.now_ms - last_now_ms_;
    if (dt > limits_.daily_window_ms) dt = 0;
    last_now_ms_ = in.now_ms;
    state_.uptime_ms += dt;
    const uint32_t now = state_.uptime_ms;

    // 2. Daily window rollover (awake time only — see ControlState).
    if (now - state_.window_start_uptime_ms >= limits_.daily_window_ms) {
        state_.window_start_uptime_ms = now;
        state_.daily_used_ms = 0;
        dirty_ = true;
    }

    Outputs out;
    out.state = phase_;
    out.profile_name = profile().name;

    // 3. Input conditioning.  Only settled samples enter the window (the pump
    //    is OFF and has been for probe_settle_ms — electrode noise during drive
    //    plus water redistribution makes anything sooner untrustworthy), and
    //    decisions then use the MEDIAN once the window is full.  A single
    //    glitch below `dry` would otherwise move water.
    const bool settled = !pump_on_ &&
        (!have_pulsed_ || (now - last_off_ms_ >= limits_.probe_settle_ms));
    if (settled && in.moisture_ok) window_push(in.moisture_q8);
    const bool usable = settled && window_count_ >= effective_window_samples();
    const q8 moisture = window_median();
    out.moisture_usable = usable;
    out.moisture_q8 = usable ? moisture : 0;

    // 4. Fail-off first: a failed read, or a settled reading pinned at/below
    //    the plausibility floor, de-energises the pump immediately (safety
    //    overrides ignore pump_min_on) and abandons the session.  The floor
    //    exists because the 0 % calibration anchor is AIR: without it an
    //    unseated probe reads "bone dry" and the engine would keep dosing a pot
    //    nothing is measuring (see ControlLimits::plausible_floor_q8).
    const bool implausible = usable && moisture < limits_.plausible_floor_q8;
    if (!in.moisture_ok || implausible) {
        pump_off(now, out);
        if (phase_ != State::Fault) {
            phase_ = State::Fault;
            state_.next_session_at_ms = now + profile().min_session_gap_ms;
            dirty_ = true;
        }
        out.state = phase_;
        out.pump_on = false;
        out.probe_implausible = implausible;
        return out;
    }
    if (phase_ == State::Fault) phase_ = State::Idle;  // probe recovered

    // 5. Latch the lockouts, then apply every hard override before actuation.
    if (in.rh_ok && in.rh_q8 > limits_.rh_lockout_q8) {
        state_.lockout_until_ms = now + limits_.lockout_ms;
        dirty_ = true;
    }
    if (usable && moisture > limits_.saturation_q8) {
        state_.lockout_until_ms = now + limits_.lockout_ms;
        dirty_ = true;
    }
    const bool locked_out = (int32_t)(state_.lockout_until_ms - now) > 0;
    const bool daily_capped = state_.daily_used_ms >= effective_daily_cap_ms();
    if (locked_out || daily_capped) {
        pump_off(now, out);
        if (phase_ == State::Watering) end_session(now, out);
        phase_ = State::Lockout;
        out.state = phase_;
        out.pump_on = false;
        return out;
    }
    if (phase_ == State::Lockout) phase_ = State::Idle;  // lockout expired

    // 6. Control law: one pulse train per session, stopped by whichever of
    //    target-reached / pulse cap / wall-clock cap comes first.
    switch (phase_) {
        case State::Idle: {
            const bool due = (int32_t)(state_.next_session_at_ms - now) <= 0;
            if (due && usable && moisture < profile().dry_q8()) {
                session_start_ms_ = now;
                session_pump_ms_ = 0;
                pulses_done_ = 0;
                phase_ = State::Watering;
                start_pulse(now, out);
            }
            break;
        }
        case State::Watering: {
            if (now - session_start_ms_ >= limits_.session_max_ms) {
                end_session(now, out);  // wall-clock backstop
            } else if (pump_on_) {
                if (now - pulse_start_ms_ >= effective_pulse_on_ms()) {
                    pump_off(now, out);
                }
            } else if (now - last_off_ms_ >= effective_soak_ms() && usable) {
                if (moisture >= profile().wet_q8()) {
                    end_session(now, out);  // target reached: stop early
                } else if (pulses_done_ >= effective_max_pulses()) {
                    end_session(now, out);  // dose cap
                } else {
                    start_pulse(now, out);
                }
            }
            break;
        }
        case State::Lockout:
        case State::Fault:
            break;
    }

    out.state = phase_;
    out.pump_on = pump_on_;
    // Pulses of the session in progress; end_session leaves the completed count
    // on the tick it closes, and an idle/faulted engine reports none.
    if (phase_ == State::Watering) out.pulses_done = (uint8_t)pulses_done_;
    return out;
}

}  // namespace sprout
