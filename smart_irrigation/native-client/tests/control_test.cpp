// Host unit tests for the Sprout irrigation control engine (control.h/.cpp).
//
// Pure C++: the engine has no ESP-IDF dependency, so this exercises exactly the
// code the device firmware compiles.  Every scenario is deterministic — the
// clock, the probe and the pump are all simulated here, and the engine's
// decisions are the only thing under test.
#include <cstdio>

#include "control.h"
#include "moisture.h"

using namespace sprout;

static int failures = 0;

#define CHECK_TRUE(cond, label)                                               \
    do {                                                                      \
        if (!(cond)) {                                                        \
            std::printf("FAIL %s\n", label);                                  \
            failures++;                                                       \
        } else {                                                              \
            std::printf("ok   %s\n", label);                                  \
        }                                                                     \
    } while (0)

#define CHECK_EQ(actual, expected, label)                                     \
    do {                                                                      \
        long a = (long)(actual);                                              \
        long e = (long)(expected);                                            \
        if (a != e) {                                                         \
            std::printf("FAIL %s: %ld != %ld\n", label, a, e);                \
            failures++;                                                       \
        } else {                                                              \
            std::printf("ok   %s\n", label);                                  \
        }                                                                     \
    } while (0)

// ── Device-loop simulator ────────────────────────────────────────────────────
// Mirrors what app_main does: feed the engine the current probe/RH state and a
// monotonic ms clock, then actuate the pump from its decision.
struct Sim {
    Control ctl;
    ControlLimits limits{};
    uint32_t now = 0;
    int32_t  moisture_q8 = q8_from_pct(30);
    bool     moisture_ok = true;
    int32_t  rh_q8 = q8_from_pct(50);
    bool     rh_ok = true;

    // Measurements taken from the decisions (what the pump would have done).
    uint32_t pump_on_ms = 0;          // total contiguous ON time observed
    uint32_t max_pulse_ms = 0;        // widest single pulse
    uint32_t pulses = 0;              // rising edges
    uint32_t min_pulse_gap_ms = 0xFFFFFFFFu;
    uint32_t session_pump_ms = 0;     // on-time reported by the last session
    uint32_t session_elapsed_ms = 0;
    uint32_t session_pulses = 0;
    uint32_t sessions = 0;
    bool     last_pump = false;
    uint32_t on_start = 0;
    uint32_t last_off = 0;
    bool     have_off = false;
    // Median-window size for this scenario.  The harness default of 1 drives
    // the instantaneous path so the state-machine tests stay focused; the
    // filter itself has dedicated tests (test_probe_filter_*).
    uint8_t  window = 1;

    void begin(int profile = default_profile_index()) {
        limits.window_samples = window;
        ctl.begin(nullptr, 0, limits);
        ctl.set_profile_index(profile);
    }

    Outputs step(uint32_t ms) {
        now += ms;
        Inputs in;
        in.now_ms = now;
        in.moisture_q8 = moisture_q8;
        in.moisture_ok = moisture_ok;
        in.rh_q8 = rh_q8;
        in.rh_ok = rh_ok;
        Outputs out = ctl.tick(in);

        if (out.pump_on && !last_pump) {
            if (have_off) {
                const uint32_t gap = now - last_off;
                if (gap < min_pulse_gap_ms) min_pulse_gap_ms = gap;
            }
            on_start = now;
            pulses++;
        }
        if (!out.pump_on && last_pump) {
            const uint32_t width = now - on_start;
            pump_on_ms += width;
            if (width > max_pulse_ms) max_pulse_ms = width;
            last_off = now;
            have_off = true;
        }
        last_pump = out.pump_on;

        if (out.session_ended) {
            sessions++;
            session_pump_ms = out.session_pump_ms;
            session_elapsed_ms = out.session_elapsed_ms;
            session_pulses = out.pulses_done;
        }
        return out;
    }
};

template <typename Pred>
static Outputs run_until(Sim& s, Pred stop, uint32_t budget_ms, uint32_t step_ms = 1000) {
    uint32_t spent = 0;
    Outputs out{};
    while (spent < budget_ms) {
        out = s.step(step_ms);
        spent += step_ms;
        if (stop(out)) return out;
    }
    return out;
}

static int kale() { return profile_index_by_name("kale"); }
static int herbs() { return profile_index_by_name("herbs"); }
static int tomato() { return profile_index_by_name("tomato"); }

// ── Profile table ────────────────────────────────────────────────────────────
static void test_profile_table() {
    CHECK_EQ(profile_count(), 5, "5 plant profiles");
    CHECK_EQ(kale(), 0, "kale is the default profile");
    CHECK_EQ(profile_index_by_name("tomato"), 2, "tomato index");
    CHECK_EQ(profile_index_by_name("nope"), -1, "unknown profile -> -1");
    CHECK_TRUE(profile_at(-7).name == profile_at(0).name, "profile_at clamps to default");
    CHECK_EQ(profile_at(kale()).dry_q8(), q8_from_pct(37), "kale dry = 45 - 8");
    CHECK_EQ(profile_at(kale()).wet_q8(), q8_from_pct(53), "kale wet = 45 + 8");

    // A plant added later must not be able to break the short-session
    // invariants or the hardness of the safety ceilings.
    const PlantProfile* prof = nullptr;
    for (int i = 0; i < profile_count(); ++i) {
        prof = &profile_at(i);
        CHECK_TRUE(prof->pulse_on_ms >= 5000 && prof->pulse_on_ms <= 15000,
                   "profile pulse width inside [5 s, 15 s]");
        CHECK_TRUE(prof->soak_ms >= 60000, "profile soak >= probe settle");
        CHECK_TRUE(prof->max_pulses >= 1 && prof->max_pulses <= 5,
                   "profile pulse count inside [1, 5]");
        CHECK_TRUE(prof->max_daily_ms <= 300000, "profile daily cap <= 5 min");
        CHECK_TRUE(prof->target_pct - prof->hyst_pct >= 0 &&
                       prof->target_pct + prof->hyst_pct <= 100,
                   "profile band inside 0..100 %");
        CHECK_TRUE(prof->min_session_gap_ms >= prof->soak_ms,
                   "profile session gap >= soak");
    }
}

// ── Pulse dosing ─────────────────────────────────────────────────────────────
static void test_dry_starts_a_session() {
    Sim s;
    s.begin(kale());
    s.moisture_q8 = q8_from_pct(30);
    Outputs out = s.step(1000);
    CHECK_TRUE(out.pump_on, "dry soil energises the pump");
    CHECK_EQ((int)out.state, (int)State::Watering, "state = watering");
    CHECK_EQ(out.pulses_done, 1, "first pulse counted");
}

static void test_pulse_width_and_session_dose() {
    Sim s;
    s.begin(kale());
    s.moisture_q8 = q8_from_pct(30);
    Outputs out = run_until(s, [](const Outputs& o) { return o.session_ended; }, 600000);
    CHECK_TRUE(out.session_ended, "session ends");
    CHECK_EQ(s.session_pulses, 1, "kale = 1 pulse while uncalibrated");
    CHECK_EQ(s.session_pump_ms, 5000, "session dose = exactly one 5 s pulse");
    CHECK_EQ(s.max_pulse_ms, 5000, "widest pulse = 5 s");
    CHECK_TRUE(!out.pump_on, "pump de-energised at session end");
    CHECK_TRUE(s.session_elapsed_ms < 300000, "session stays short");
}

static void test_target_reached_stops_early() {
    Sim dry;
    dry.begin(herbs());
    dry.moisture_q8 = q8_from_pct(20);
    run_until(dry, [](const Outputs& o) { return o.session_ended; }, 900000);
    CHECK_EQ(dry.session_pulses, 2, "herbs doses 2 pulses when soil stays dry");

    Sim wet;
    wet.begin(herbs());
    wet.moisture_q8 = q8_from_pct(20);
    run_until(wet, [](const Outputs& o) { return o.pump_changed && !o.pump_on; }, 60000);
    wet.moisture_q8 = q8_from_pct(60);  // >= herbs wet (48 %)
    Outputs out = run_until(wet, [](const Outputs& o) { return o.session_ended; }, 900000);
    CHECK_TRUE(out.session_ended, "session ends on target");
    CHECK_EQ(wet.session_pulses, 1, "stop early at the wet threshold, not the dose cap");
    CHECK_EQ(out.session_pump_ms, 5000, "dose stops at one pulse");
}

static void test_soak_before_next_pulse() {
    Sim s;
    s.begin(herbs());
    s.moisture_q8 = q8_from_pct(20);  // stays dry -> 2 pulses
    run_until(s, [](const Outputs& o) { return o.session_ended; }, 900000);
    CHECK_EQ(s.pulses, 2, "two pulses");
    CHECK_TRUE(s.min_pulse_gap_ms >= 90000,
               "second pulse waits at least the 90 s soak (probe settled)");
}

static void test_pulse_delays_are_injectable() {
    // Upper pulse clamp: a profile pulse longer than the ceiling is truncated.
    Sim s;
    s.limits.pump_min_on_ms = 1000;
    s.limits.pulse_on_max_ms = 2000;
    s.begin(kale());
    s.moisture_q8 = q8_from_pct(30);
    run_until(s, [](const Outputs& o) { return o.session_ended; }, 600000);
    CHECK_EQ(s.session_pump_ms, 2000, "pulse clamped to pulse_on_max_ms");

    // Lower clamp: never below the verified motor spin-up width.
    Sim m;
    m.limits.pump_min_on_ms = 8000;
    m.begin(kale());
    m.moisture_q8 = q8_from_pct(30);
    run_until(m, [](const Outputs& o) { return o.session_ended; }, 600000);
    CHECK_EQ(m.session_pump_ms, 8000, "pulse raised to pump_min_on_ms");
}

static void test_min_off_guard_between_pulses() {
    Sim s;
    // 2.5 min, longer than the 90 s soak but short enough to fit inside the
    // 5 min session backstop.
    s.limits.pump_min_off_ms = 150000;
    s.begin(herbs());
    s.moisture_q8 = q8_from_pct(20);
    run_until(s, [](const Outputs& o) { return o.session_ended; }, 900000);
    CHECK_EQ(s.pulses, 2, "still doses twice");
    CHECK_TRUE(s.min_pulse_gap_ms >= 150000,
               "min-off guard delays the 2nd pulse past the soak");
}

static void test_pulse_count_ceiling() {
    Sim s;
    s.limits.max_pulses_ceiling = 2;  // tomato's profile asks for 3
    s.begin(tomato());
    s.moisture_q8 = q8_from_pct(20);
    run_until(s, [](const Outputs& o) { return o.session_ended; }, 900000);
    CHECK_EQ(s.session_pulses, 2, "hard pulse ceiling truncates the profile dose");
}

static void test_session_wall_clock_backstop() {
    Sim s;
    s.limits.session_max_ms = 60000;
    s.begin(tomato());          // 3 pulses, 120 s soak -> 375 s if unchecked
    s.moisture_q8 = q8_from_pct(20);
    Outputs out = run_until(s, [](const Outputs& o) { return o.session_ended; }, 600000);
    CHECK_TRUE(out.session_ended, "backstop ends the session");
    CHECK_EQ(s.session_elapsed_ms, 60000, "session aborted at session_max_ms");
    CHECK_EQ(s.session_pulses, 1, "only the first pulse was delivered");
    CHECK_TRUE(!out.pump_on, "pump off after the backstop");
}

// ── Safety overrides ─────────────────────────────────────────────────────────
static void test_saturation_lockout() {
    Sim s;
    s.begin(kale());
    s.moisture_q8 = q8_from_pct(90);  // > 85 % saturation
    Outputs out = s.step(1000);
    CHECK_TRUE(!out.pump_on, "saturated probe -> no pump");
    CHECK_EQ((int)out.state, (int)State::Lockout, "locked out");

    s.moisture_q8 = q8_from_pct(10);  // very dry, but the lockout must hold
    for (int i = 0; i < 119; ++i) s.step(60000);  // 1 h 59 min
    CHECK_EQ(s.pulses, 0, "no watering inside the 2 h lockout");
    run_until(s, [](const Outputs& o) { return o.pump_on; }, 3600000, 60000);
    CHECK_TRUE(s.pulses >= 1, "watering resumes after the lockout expires");
}

static void test_rh_lockout_and_missing_rh() {
    Sim humid;
    humid.begin(kale());
    humid.moisture_q8 = q8_from_pct(30);
    humid.rh_q8 = q8_from_pct(95);  // > 90 %
    Outputs out = humid.step(1000);
    CHECK_TRUE(!out.pump_on, "air RH >= 90 % -> no pump");

    // #124: the Port A contact can drop the SHT30.  A missing RH skips only
    // the RH clause — the probe saturation lockout and every timer still apply.
    Sim no_rh;
    no_rh.begin(kale());
    no_rh.moisture_q8 = q8_from_pct(30);
    no_rh.rh_ok = false;
    CHECK_TRUE(no_rh.step(1000).pump_on, "missing RH does not block a dry-soil session");
}

static void test_probe_failure_fails_off() {
    Sim s;
    s.begin(kale());
    s.moisture_q8 = q8_from_pct(30);
    s.step(1000);
    CHECK_TRUE(s.last_pump, "pump on before the fault");

    s.moisture_ok = false;
    Outputs out = s.step(1000);
    CHECK_TRUE(!out.pump_on, "probe read failure de-energises the pump immediately");
    CHECK_EQ((int)out.state, (int)State::Fault, "state = fault");

    s.moisture_ok = true;
    out = s.step(1000);
    CHECK_EQ((int)out.state, (int)State::Idle, "recovered to idle");
    run_until(s, [](const Outputs& o) { return o.pump_on; }, 1700000);  // < 30 min
    CHECK_EQ(s.pulses, 1, "no new session before min_session_gap");
    run_until(s, [](const Outputs& o) { return o.pump_on; }, 120000);
    CHECK_EQ(s.pulses, 2, "session restarts after min_session_gap");
}

static void test_daily_cap_and_its_ceiling() {
    Sim s;
    s.limits.daily_cap_max_ms = 5000;  // clamps kale's 60 s profile cap
    s.begin(kale());
    s.moisture_q8 = q8_from_pct(30);
    run_until(s, [](const Outputs& o) { return o.session_ended; }, 600000);
    CHECK_EQ(s.session_pump_ms, 5000, "one pulse spent the whole daily budget");

    Outputs out = s.step(1000);
    CHECK_EQ((int)out.state, (int)State::Lockout, "daily cap -> lockout");
    run_until(s, [](const Outputs& o) { return o.pump_on; }, 21600000, 60000);  // 6 h
    CHECK_EQ(s.pulses, 1, "no watering while the daily cap holds");
}

static void test_hysteresis_band() {
    // Between dry (37 %) and wet (53 %) the engine must neither water nor
    // re-trigger after a session.
    Sim s;
    s.begin(kale());
    s.moisture_q8 = q8_from_pct(45);
    run_until(s, [](const Outputs& o) { return o.pump_on; }, 7200000, 60000);  // 2 h
    CHECK_EQ(s.pulses, 0, "in-band moisture never waters");

    s.moisture_q8 = q8_from_pct(36);  // just below dry
    run_until(s, [](const Outputs& o) { return o.pump_on; }, 3600000, 60000);
    CHECK_EQ(s.pulses, 1, "below the dry threshold waters");
}

static void test_implausible_probe_floor() {
    // Probe pinned at the 0 % AIR anchor — the observed unseated-probe state
    // (2026-09-18: <= 1.4 % for 14 h, then 42-47 % once seated in the pot).
    Sim air;
    air.begin(kale());
    air.moisture_q8 = q8_from_pct(0);
    Outputs out = run_until(air, [](const Outputs& o) { return o.state == State::Fault; },
                            600000, 60000);
    CHECK_EQ((int)out.state, (int)State::Fault, "air-anchor reading -> fault");
    CHECK_TRUE(out.probe_implausible, "fault flagged as implausible");
    run_until(air, [](const Outputs& o) { return o.pump_on; }, 21600000, 60000);  // 6 h
    CHECK_EQ(air.pulses, 0, "no watering while the probe reads air");

    // Boundary: just below the floor is implausible, just above is dry soil.
    Sim low;
    low.begin(kale());
    low.moisture_q8 = q8_from_pct(4);
    CHECK_TRUE(!low.step(1000).pump_on, "4 % (below floor) does not water");

    Sim real_dry;
    real_dry.begin(kale());
    real_dry.moisture_q8 = q8_from_pct(6);  // above floor, below kale dry 37
    CHECK_TRUE(real_dry.step(1000).pump_on, "6 % (above floor) waters normally");

    // Recovery: reseating the probe restores normal control.
    Sim back;
    back.begin(kale());
    back.moisture_q8 = q8_from_pct(0);
    run_until(back, [](const Outputs& o) { return o.state == State::Fault; }, 600000);
    back.moisture_q8 = q8_from_pct(45);  // in band again
    Outputs rec = back.step(1000);
    CHECK_EQ((int)rec.state, (int)State::Idle, "plausible reading clears the fault");
    CHECK_TRUE(!rec.probe_implausible, "implausible flag cleared");

    // A reading taken while the pump is running is NOT settled: drive noise
    // must not abort a pulse or latch a lockout.
    Sim running;
    running.begin(kale());
    running.moisture_q8 = q8_from_pct(30);
    CHECK_TRUE(running.step(1000).pump_on, "session started");
    running.moisture_q8 = q8_from_pct(0);  // noise during drive
    CHECK_TRUE(running.step(1000).pump_on, "mid-pulse low reading keeps the pulse");
    running.moisture_q8 = q8_from_pct(90);  // noise during drive
    CHECK_TRUE(running.step(1000).pump_on, "mid-pulse wet reading does not latch a lockout");
}

// ── Input conditioning: the settled-sample median window ─────────────────────
static void test_probe_filter_rejects_spikes() {
    Sim s;
    s.window = 5;
    s.begin(kale());
    s.moisture_q8 = q8_from_pct(46);
    for (int i = 0; i < 5; ++i) s.step(1000);  // let the window fill
    CHECK_EQ(s.pulses, 0, "steady in-band moisture never waters");

    // A single-sample spike below `dry` — the observed 36.9 % against a
    // 42-47.5 % cluster — must not start a session.
    s.moisture_q8 = q8_from_pct(10);
    Outputs spike = s.step(1000);
    CHECK_EQ(s.pulses, 0, "single-sample dry spike does not start a session");
    CHECK_TRUE(!spike.probe_implausible, "single-sample spike is not a plausibility fault");

    // Neither must a single air-anchor glitch.
    s.moisture_q8 = q8_from_pct(0);
    Outputs glitch = s.step(1000);
    CHECK_TRUE(!glitch.probe_implausible, "single-sample air glitch does not fault");
    CHECK_EQ((int)glitch.state, (int)State::Idle, "still idle after the glitches");

    // A sustained dry reading fills the window and is acted on normally.
    s.moisture_q8 = q8_from_pct(10);
    run_until(s, [](const Outputs& o) { return o.pump_on; }, 60000);
    CHECK_EQ(s.pulses, 1, "sustained dry reading starts a session");

    // A sustained air reading trips the plausibility floor.
    Sim air;
    air.window = 5;
    air.begin(kale());
    air.moisture_q8 = q8_from_pct(0);
    Outputs faulted = run_until(air, [](const Outputs& o) { return o.state == State::Fault; },
                                60000);
    CHECK_TRUE(faulted.probe_implausible, "sustained air reading -> implausible fault");
    CHECK_EQ(air.pulses, 0, "no session on a sustained air reading");
}

static void test_filter_window_resets_after_a_pulse() {
    // The window is cleared on the pump edge, so the post-pulse reading is made
    // only of post-pulse samples.  Without that, the median would still be the
    // pre-pulse dry soil and the engine would keep dosing.
    Sim s;
    s.window = 5;
    s.begin(herbs());                    // 2 pulses allowed, wet = 48 %
    s.moisture_q8 = q8_from_pct(20);     // well below herbs dry (32 %)
    run_until(s, [](const Outputs& o) { return o.pump_changed && !o.pump_on; }, 60000);
    CHECK_EQ(s.pulses, 1, "one pulse delivered");

    s.moisture_q8 = q8_from_pct(60);     // the water arrived: probe now clearly wet
    Outputs out = run_until(s, [](const Outputs& o) { return o.session_ended; }, 600000);
    CHECK_TRUE(out.session_ended, "session ends");
    CHECK_EQ(s.session_pulses, 1, "stopped early on the fresh post-pulse reading");
    CHECK_EQ(s.pulses, 1, "no blind second pulse");
}

// ── Sweep regression (AGENTS.md: drier soil => >= pump time) ─────────────────
static void test_sweep_monotonic_dose() {
    // Starts at 5 % — below the probe plausibility floor the engine correctly
    // refuses to water (test_implausible_probe_floor covers that boundary).
    const int pct[] = {5, 10, 20, 30, 36, 40, 45, 50, 60, 70};
    uint32_t prev = 0xFFFFFFFFu;
    bool monotonic = true;
    bool edge_ok = true;
    const PlantProfile& p = profile_at(tomato());
    for (unsigned i = 0; i < sizeof(pct) / sizeof(pct[0]); ++i) {
        Sim s;
        s.begin(tomato());
        s.moisture_q8 = q8_from_pct(pct[i]);
        run_until(s, [](const Outputs& o) { return o.session_ended; }, 900000);
        const uint32_t dose = s.session_pump_ms;
        if (dose > prev) monotonic = false;
        const bool expected_water = pct[i] < (p.target_pct - p.hyst_pct);
        if ((dose > 0) != expected_water) edge_ok = false;
        prev = dose;
    }
    CHECK_TRUE(monotonic, "sweep: drier soil never gets less water");
    CHECK_TRUE(edge_ok, "sweep: watering starts exactly below the dry threshold");
}

// ── Persistence ──────────────────────────────────────────────────────────────
static void test_state_roundtrip_and_daily_window() {
    Sim s;
    s.begin(kale());
    s.moisture_q8 = q8_from_pct(30);
    run_until(s, [](const Outputs& o) { return o.session_ended; }, 600000);
    ControlState snap = s.ctl.state();
    CHECK_EQ(snap.daily_used_ms, 5000, "daily totaliser accounts the dose");
    CHECK_EQ(snap.magic, kStateMagic, "state stamped");

    Control resumed;
    resumed.begin(&snap, 0);
    resumed.set_profile_index(kale());
    CHECK_EQ(resumed.state().daily_used_ms, 5000, "daily totaliser survives a reboot");
    CHECK_EQ(resumed.state().uptime_ms, snap.uptime_ms, "awake clock continues");

    // The window rolls over on awake time (no RTC on the node).
    ControlState near = snap;
    near.window_start_uptime_ms = 0;
    near.uptime_ms = 24UL * 60UL * 60UL * 1000UL - 1000;
    near.daily_used_ms = 5000;
    Control rolled;
    rolled.begin(&near, 0);
    rolled.tick(Inputs{});  // dt 0
    CHECK_EQ(rolled.state().daily_used_ms, 5000, "no rollover before 24 h");
    Inputs in;
    in.now_ms = 2000;
    in.moisture_ok = true;
    in.moisture_q8 = q8_from_pct(30);
    rolled.tick(in);
    CHECK_EQ(rolled.state().daily_used_ms, 0, "daily totaliser resets after 24 h");

    // A corrupt blob must fall back to clean defaults, never to stale caps.
    ControlState bad = snap;
    bad.magic = 0;
    Control fresh;
    fresh.begin(&bad, 0);
    CHECK_EQ(fresh.state().daily_used_ms, 0, "bad magic -> clean state");
}

// ── Wire-level: the Q8.8 probe path the engine consumes ──────────────────────
static void test_q8_probe_conversion() {
    CHECK_EQ(moisture_q8_from_counts(2068), 0, "air counts -> 0 %");
    CHECK_EQ(moisture_q8_from_counts(1580), 25600, "submerged counts -> 100 %");
    CHECK_EQ(moisture_q8_from_counts(1824), 12800, "midpoint -> 50 %");
    CHECK_EQ(moisture_q8_from_counts(4000), 0, "above dry clamps");
    CHECK_EQ(moisture_q8_from_counts(0), 25600, "below wet clamps");

    // The Q8.8 curve must agree with the float curve the telemetry uses.
    bool agree = true;
    for (int raw = 1500; raw <= 2100; raw += 37) {
        const float pct = moisture_percent_from_counts(raw);
        const int32_t q8 = moisture_q8_from_counts(raw);
        const float err = (float)q8 / 256.0f - pct;
        if (err > 0.01f || err < -0.01f) agree = false;
    }
    CHECK_TRUE(agree, "Q8.8 and float curves agree within 0.01 %");
}

int main() {
    test_profile_table();
    test_dry_starts_a_session();
    test_pulse_width_and_session_dose();
    test_target_reached_stops_early();
    test_soak_before_next_pulse();
    test_pulse_delays_are_injectable();
    test_min_off_guard_between_pulses();
    test_pulse_count_ceiling();
    test_session_wall_clock_backstop();
    test_saturation_lockout();
    test_rh_lockout_and_missing_rh();
    test_probe_failure_fails_off();
    test_daily_cap_and_its_ceiling();
    test_hysteresis_band();
    test_implausible_probe_floor();
    test_probe_filter_rejects_spikes();
    test_filter_window_resets_after_a_pulse();
    test_sweep_monotonic_dose();
    test_state_roundtrip_and_daily_window();
    test_q8_probe_conversion();

    if (failures == 0) {
        std::printf("ALL PASS\n");
        return 0;
    }
    std::printf("%d failure(s)\n", failures);
    return 1;
}
