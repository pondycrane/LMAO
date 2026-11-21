#pragma once

#include <cstddef>
#include <cstdint>

// Pull-side directive — pure C++ (no ESP-IDF), so the host tests exercise the
// exact parse + interlock logic the socket runs.
//
// The socket polls the LMAO server's directive relay and applies what it says.
// The controller's dew-point law is the normal stop; these interlocks are the
// last line of defence if the controller, the relay, or the law misbehaves:
//   * a missing, invalid, or absent directive is OFF;
//   * a continuous run longer than `max_on_s` is cut and a cooldown starts;
//   * during that cooldown, ON is refused.
// The lease remains the deadman: an ON directive that stops being renewed
// expires on its own.
namespace humidifier {

struct Directive {
    bool valid = false;
    bool on = false;
    uint32_t lease_s = 0;
    uint32_t issued_s = 0;  // server wall-clock stamp (diagnostics only)
};

struct LocalLimits {
    uint32_t max_on_s = 3 * 3600;
    uint32_t cooldown_s = 600;
};

struct LocalState {
    uint32_t cooldown_until_ms = 0;  // 0 = no cooldown
};

enum class Action { None, On, Off };

// Parse the relay's flat JSON object.  Returns false (and leaves *out invalid)
// when the body is not an object or carries no boolean "on".  Every value is
// skipped as JSON, so a quoted reason containing `"on":true` cannot spoof it.
bool parse_directive(const char* body, size_t len, Directive* out);

// Apply the directive plus the local interlocks.  `relay_on`/`on_since_ms`
// describe the relay as the firmware currently has it (the lease's start of
// the current continuous run).  Mutates only `st`'s cooldown.  `*why` is a
// static string, safe to store.
Action apply_directive(const Directive& d, uint32_t now_ms, bool relay_on,
                       uint32_t on_since_ms, const LocalLimits& limits,
                       LocalState* st, const char** why);

}  // namespace humidifier
