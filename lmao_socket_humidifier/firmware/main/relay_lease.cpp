#include "relay_lease.h"

namespace humidifier {

bool time_reached(uint32_t now_ms, uint32_t deadline_ms) {
    // Signed difference keeps the comparison correct across the 32-bit wrap.
    return (int32_t)(now_ms - deadline_ms) >= 0;
}

uint32_t lease_arm(Lease* lease, uint32_t lease_s, uint32_t now_ms,
                   const LeaseLimits& limits) {
    if (lease == nullptr) return 0;
    uint32_t granted = lease_s ? lease_s : limits.default_s;
    if (granted > limits.max_s) granted = limits.max_s;
    if (granted == 0) granted = 1;  // never grant a zero lease
    if (!lease->on) {
        lease->on = true;
        lease->on_since_ms = now_ms;
    }
    lease->deadline_ms = now_ms + granted * 1000u;
    return granted;
}

void lease_clear(Lease* lease, uint32_t now_ms) {
    if (lease == nullptr || !lease->on) return;
    lease->on_ms_total += now_ms - lease->on_since_ms;
    lease->on = false;
    lease->deadline_ms = 0;
}

bool lease_expired(const Lease& lease, uint32_t now_ms) {
    return lease.on && time_reached(now_ms, lease.deadline_ms);
}

uint32_t lease_remaining_s(const Lease& lease, uint32_t now_ms) {
    if (!lease.on) return 0;
    if (time_reached(now_ms, lease.deadline_ms)) return 0;
    return (lease.deadline_ms - now_ms + 999u) / 1000u;
}

}  // namespace humidifier
