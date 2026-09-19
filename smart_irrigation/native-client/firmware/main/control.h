#pragma once

#include <stdint.h>

// Sprout irrigation control engine — pure C++ (no ESP-IDF), so the exact code
// the device runs is exercised by host unit tests under Bazel.
//
// Implements the verdict of docs/algorithm-evaluation.md §1.3 as amended by
// §1.4 (amendment A1, 2026-09-18):
//   * hysteresis state machine on soil moisture (dry -> water, wet -> stop),
//   * a watering session is a PULSE TRAIN (short ON, soak, re-read) instead of
//     one fixed-length burst, so a session can never out-run the pot,
//   * the plant's optimum is a PlantProfile (target + hysteresis band + dose
//     limits) so the node is configured per plant type,
//   * the hard safety override layer is always evaluated last and always wins
//     (AGENTS.md: safety is hard-coded, never tuned by a plant profile).

namespace sprout {

// ─── Fixed point ─────────────────────────────────────────────────────────────
// AGENTS.md: "No float if avoidable — fixed-point Q8.8 for the control engine."
// Q8.8 has 1.0 == 256, so 1 % == 256 and the 0..100 % moisture scale spans
// 0..25600 exactly (see moisture_q8_from_counts in moisture.h).
using q8 = int32_t;
constexpr q8 Q8_ONE = 256;
constexpr q8 q8_from_pct(int pct) { return (q8)pct * Q8_ONE; }

// ─── Plant profile ───────────────────────────────────────────────────────────
// The per-plant "optimal moisture" surface.  Percentages are in the sensor's
// own 0..100 scale (100 % == probe submerged), NOT volumetric water content, so
// these are seeds to be re-fitted from the logged decay curves (evaluation §4).
struct PlantProfile {
    const char* name;                // NVS value, e.g. "kale"
    int16_t     target_pct;          // optimal moisture: band centre
    int16_t     hyst_pct;            // half-band: dry = target-hyst, wet = target+hyst
    uint16_t    pulse_on_ms;         // one pump pulse (clamped by the limits)
    uint32_t    soak_ms;             // pump OFF between pulses (>= probe settle)
    uint8_t     max_pulses;          // dose cap per session (clamped by the limits)
    uint32_t    min_session_gap_ms;  // no new session within this gap
    uint32_t    max_daily_ms;        // on-time cap per 24 h window (clamped)

    constexpr q8 dry_q8() const { return q8_from_pct(target_pct - hyst_pct); }
    constexpr q8 wet_q8() const { return q8_from_pct(target_pct + hyst_pct); }
};

// Profile table (defined in control.cpp).  Indices are not persisted — the
// node stores the profile NAME so reordering the table cannot mis-configure a
// deployed plant.
int profile_count();
const PlantProfile& profile_at(int index);
int profile_index_by_name(const char* name);  // -1 when unknown
int default_profile_index();                  // "kale"

// ─── Hard limits ─────────────────────────────────────────────────────────────
// Not plant-tunable: safety bounds taken from docs/hardware-verification.md §5
// and the phase-0 evaluation.  A plant profile tunes the optimum and the dose
// shape; these ceilings make the overflow/safety invariants hold even if a
// profile (or a future downlink-pushed profile) is wrong.
struct ControlLimits {
    q8       saturation_q8   = q8_from_pct(85);   // probe at/above -> lockout
    q8       rh_lockout_q8   = q8_from_pct(90);   // air RH at/above -> lockout
    uint32_t lockout_ms      = 2UL * 60UL * 60UL * 1000UL;
    uint32_t pump_min_on_ms  = 5000;              // verified 5 s motor spin-up
    uint32_t pump_min_off_ms = 10000;             // pump protection (eval §1.3)
    uint32_t probe_settle_ms = 60000;             // verified ~60 s probe settle
    uint32_t daily_window_ms = 24UL * 60UL * 60UL * 1000UL;
    // Short-session invariants (user requirement 2026-09-18): one pulse can
    // never exceed pulse_on_max_ms, a session can never hold the pump on past
    // max_pulses_ceiling pulses, and a session can never outlive session_max_ms
    // of wall clock no matter how the pulse/soak schedule is configured.
    uint32_t pulse_on_max_ms = 15000;
    uint8_t  max_pulses_ceiling = 5;
    uint32_t session_max_ms = 300000;
    // Ceiling on any profile's daily cap (battery/pot protection).
    uint32_t daily_cap_max_ms = 300000;
    // Probe plausibility floor.  The 2-point curve anchors 0 % at AIR, so a
    // reading pinned near the anchor cannot be told apart from bone-dry soil —
    // and watering on it doses a pot whose probe is not in the medium (observed
    // 2026-09-18: 139/159 samples at <= 1.4 % for 14 h while the probe sat
    // outside the pot, versus 42-47 % once seated).  A settled reading below
    // this floor is treated as an implausible input: fail off, do not water,
    // wait for a plausible reading.
    q8       plausible_floor_q8 = q8_from_pct(5);
    // Input conditioning: decisions use the MEDIAN of the last N settled
    // samples, not a single reading.  The live stream shows sub-second outliers
    // (36.9 % against a 42-47.5 % cluster) on a 488-LSB analog channel, and one
    // glitch below `dry` would otherwise start a session — i.e. move water.
    // A median ignores up to half the window being garbage and needs no tuning
    // beyond N; the window is cleared on every pump edge so a post-pulse
    // reading is never mixed with pre-pulse soil.
    uint8_t  window_samples = 30;   // 1 Hz settled sampling => 30 s
};

// ─── State / IO ──────────────────────────────────────────────────────────────
enum class State : uint8_t { Idle = 0, Watering = 1, Lockout = 2, Fault = 3 };
const char* state_name(State s);

// Persisted across reboots (NVS blob).  The node has no RTC, so uptime_ms is
// the engine's own accumulated AWAKE time and the daily window advances by
// awake time only: a power cycle delays the rollover, which makes the daily
// cap conservative (it can under-water, never over-water).  Documented
// limitation — see evaluation §1.4.
struct ControlState {
    uint32_t magic;                  // kStateMagic when the blob is valid
    uint32_t daily_used_ms;          // pump on-time inside the current window
    uint32_t window_start_uptime_ms;
    uint32_t uptime_ms;              // accumulated awake ms across boots
    uint32_t next_session_at_ms;     // uptime terms
    uint32_t lockout_until_ms;       // uptime terms
};
constexpr uint32_t kStateMagic = 0x53505254u;  // "SPRT"

struct Inputs {
    uint32_t now_ms = 0;         // monotonic hardware clock (esp_timer ms)
    q8       moisture_q8 = 0;    // valid only when moisture_ok
    bool     moisture_ok = false;
    q8       rh_q8 = 0;          // valid only when rh_ok
    bool     rh_ok = false;
};

struct Outputs {
    bool     pump_on = false;          // the decision; the caller gates actuation
    bool     pump_changed = false;     // edge this tick (for event reporting)
    State    state = State::Idle;
    q8       moisture_q8 = 0;          // filtered (median) value behind the decision
    bool     moisture_usable = false;  // window full: moisture_q8 is valid
    uint8_t  pulses_done = 0;          // pulses in the current/last session
    bool     session_ended = false;    // true only on the closing tick
    bool     probe_implausible = false;  // this tick's fault: reading below the floor
    uint32_t session_elapsed_ms = 0;   // wall time of the session just ended
    uint32_t session_pump_ms = 0;      // its actual pump on-time (ML dose field)
    const char* profile_name = "";
};

class Control {
public:
    // Load persisted state (nullptr or bad magic => clean defaults, kale).
    // now_ms seeds the internal clock; the first tick's delta is 0.
    void begin(const ControlState* persisted, uint32_t now_ms,
               const ControlLimits& limits = ControlLimits{});

    void set_profile_index(int index);       // clamped to the table
    int  profile_index() const { return profile_index_; }
    const PlantProfile& profile() const;
    const ControlState& state() const { return state_; }

    // True when the persisted blob changed (daily counter / window / gap /
    // lockout) and the caller should re-write NVS.
    bool dirty() const { return dirty_; }
    void clear_dirty() { dirty_ = false; }

    Outputs tick(const Inputs& in);

private:
    bool start_pulse(uint32_t uptime, Outputs& out);
    void pump_off(uint32_t uptime, Outputs& out);
    void end_session(uint32_t uptime, Outputs& out);
    uint32_t effective_pulse_on_ms() const;
    uint32_t effective_soak_ms() const;
    uint8_t  effective_max_pulses() const;
    uint32_t effective_daily_cap_ms() const;

    // Settled-sample median window (see ControlLimits::window_samples).
    static constexpr int kWindowMax = 60;
    void window_push(q8 v);
    void window_clear();
    q8   window_median();
    int  effective_window_samples() const;

    ControlState state_{};
    ControlLimits limits_{};
    int      profile_index_ = 0;
    uint32_t last_now_ms_ = 0;
    bool     pump_on_ = false;
    bool     have_pulsed_ = false;      // last_off_ms_ is meaningful
    uint32_t pulse_start_ms_ = 0;       // uptime
    uint32_t last_off_ms_ = 0;          // uptime
    uint32_t session_start_ms_ = 0;     // uptime
    uint32_t session_pump_ms_ = 0;
    uint32_t pulses_done_ = 0;
    State    phase_ = State::Idle;
    bool     dirty_ = false;

    q8       window_[kWindowMax]{};   // ring of settled samples
    q8       sort_buf_[kWindowMax]{}; // scratch for window_median() (no stack use)
    int      window_count_ = 0;
    int      window_head_ = 0;
};

}  // namespace sprout
