#pragma once

#include <cstdint>

// Relay lease logic — pure C++ (no ESP-IDF), so the host tests exercise the
// exact code the socket runs.
//
// The relay is the actuator of a mains appliance.  A controller that dies (or
// loses WiFi) must not leave the humidifier running, so every ON request
// carries a lease and the firmware opens the relay when it expires.  The
// controller renews the lease on each control cycle; a missed renewal is the
// deadman.
namespace humidifier {

    struct LeaseLimits {
        uint32_t default_s = 300;
        uint32_t max_s = 3600;
    };

    struct Lease {
        bool on = false;
        uint32_t deadline_ms = 0;   // valid while on
        uint32_t on_since_ms = 0;   // start of the current continuous run
        uint32_t on_ms_total = 0;   // accumulated ON time since boot
    };

    // Wrap-safe "deadline reached" test for a free-running millisecond clock
    // (esp_timer wraps after ~49.7 days).
    bool time_reached(uint32_t now_ms, uint32_t deadline_ms);

    // Arm (or renew) the lease.  A zero/absent lease takes the configured
    // default; the grant is clamped to max_s.  Renewing an already-ON relay
    // extends the deadline and keeps on_since_ms, so on_ms_total measures one
    // continuous run.  Returns the granted lease in seconds.
    uint32_t lease_arm(Lease* lease, uint32_t lease_s, uint32_t now_ms,
                       const LeaseLimits& limits);

    // Open the relay, folding the current run into on_ms_total.
    void lease_clear(Lease* lease, uint32_t now_ms);

    bool lease_expired(const Lease& lease, uint32_t now_ms);
    uint32_t lease_remaining_s(const Lease& lease, uint32_t now_ms);

}
