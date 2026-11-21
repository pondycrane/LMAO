#include "directive.h"

#include <cstring>

namespace humidifier {

namespace {

const char* skip_ws(const char* p, const char* end) {
    while (p < end && (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r')) ++p;
    return p;
}

// `p` points at the opening quote; returns the byte after the closing quote
// (or `end` when unterminated).
const char* skip_string(const char* p, const char* end) {
    ++p;
    while (p < end) {
        if (*p == '\\') {
            p += 2;
            continue;
        }
        if (*p == '"') return p + 1;
        ++p;
    }
    return end;
}

// Skip one JSON value.  Returns nullptr on malformed input so the caller can
// reject the whole body rather than half-apply it.
const char* skip_value(const char* p, const char* end) {
    p = skip_ws(p, end);
    if (p >= end) return nullptr;
    if (*p == '"') return skip_string(p, end);
    if (*p == '{' || *p == '[') {
        const char open = *p;
        const char close = (open == '{') ? '}' : ']';
        int depth = 0;
        while (p < end) {
            if (*p == '"') {
                p = skip_string(p, end);
                continue;
            }
            if (*p == open) {
                ++depth;
            } else if (*p == close) {
                if (--depth == 0) return p + 1;
            }
            ++p;
        }
        return nullptr;
    }
    while (p < end && *p != ',' && *p != '}' && *p != ']') ++p;
    return p;
}

bool key_is(const char* begin, size_t len, const char* want) {
    const size_t want_len = std::strlen(want);
    return len == want_len && std::memcmp(begin, want, len) == 0;
}

// Digits only; a fractional part is skipped (the relay sends issued_s as a
// float).  Returns false when no digit is present.
bool parse_uint(const char*& p, const char* end, uint32_t* out) {
    uint32_t v = 0;
    const char* start = p;
    while (p < end && *p >= '0' && *p <= '9') {
        v = v * 10u + (uint32_t)(*p - '0');
        ++p;
    }
    if (p == start) return false;
    if (p < end && *p == '.') {
        ++p;
        while (p < end && *p >= '0' && *p <= '9') ++p;
    }
    *out = v;
    return true;
}

}  // namespace

bool parse_directive(const char* body, size_t len, Directive* out) {
    if (body == nullptr || out == nullptr || len == 0) return false;
    *out = Directive{};

    const char* p = body;
    const char* end = body + len;
    p = skip_ws(p, end);
    if (p >= end || *p != '{') return false;
    ++p;

    bool saw_on = false;
    for (;;) {
        p = skip_ws(p, end);
        if (p >= end) return false;
        if (*p == '}') break;
        if (*p != '"') return false;

        const char* key_begin = p + 1;
        const char* key_end = skip_string(p, end);
        if (key_end >= end) return false;
        const size_t key_len = (size_t)(key_end - 1 - key_begin);

        p = skip_ws(key_end, end);
        if (p >= end || *p != ':') return false;
        ++p;
        p = skip_ws(p, end);
        if (p >= end) return false;

        if (key_is(key_begin, key_len, "on")) {
            if ((size_t)(end - p) >= 4 && std::memcmp(p, "true", 4) == 0) {
                out->on = true;
                saw_on = true;
                p += 4;
            } else if ((size_t)(end - p) >= 5 && std::memcmp(p, "false", 5) == 0) {
                out->on = false;
                saw_on = true;
                p += 5;
            } else {
                return false;
            }
        } else if (key_is(key_begin, key_len, "lease_s")) {
            if (!parse_uint(p, end, &out->lease_s)) return false;
        } else if (key_is(key_begin, key_len, "issued_s")) {
            if (!parse_uint(p, end, &out->issued_s)) return false;
        } else {
            p = skip_value(p, end);
            if (p == nullptr) return false;
        }

        p = skip_ws(p, end);
        if (p < end && *p == ',') {
            ++p;
            continue;
        }
        if (p < end && *p == '}') break;
        return false;
    }

    out->valid = saw_on;
    return saw_on;
}

Action apply_directive(const Directive& d, uint32_t now_ms, bool relay_on,
                       uint32_t on_since_ms, const LocalLimits& limits,
                       LocalState* st, const char** why) {
    if (!d.valid || !d.on) {
        *why = "directive off";
        return Action::Off;
    }

    // Runaway guard: a law that keeps saying ON must not run the appliance
    // past the continuous-run cap.  Cut it and refuse to restart at once.
    if (relay_on &&
        (uint32_t)(now_ms - on_since_ms) >= limits.max_on_s * 1000u) {
        st->cooldown_until_ms = now_ms + limits.cooldown_s * 1000u;
        *why = "max continuous run";
        return Action::Off;
    }
    if (st->cooldown_until_ms != 0 &&
        (int32_t)(now_ms - st->cooldown_until_ms) < 0) {
        *why = "cooldown after max run";
        return Action::Off;
    }

    *why = "directive on";
    return Action::On;
}

}  // namespace humidifier
