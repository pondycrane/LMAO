// Host test for the socket's HLW8032 parser + relay-lease law — the exact
// sources the firmware compiles (Bazel, no ESP-IDF).
#include <cassert>
#include <cstdio>
#include <cstring>

#include "directive.h"
#include "hlw8032.h"
#include "relay_lease.h"
#include "sht30_codec.h"

// ── frame builder: header + 24-bit big-endian fields + checksum ──────────
static void put24(uint8_t* p, uint32_t v) {
    p[0] = (uint8_t)(v >> 16);
    p[1] = (uint8_t)(v >> 8);
    p[2] = (uint8_t)v;
}

static void build_frame(uint8_t* f, uint32_t vol_par, uint32_t vol_data,
                        uint32_t cur_par, uint32_t cur_data, uint32_t pow_par,
                        uint32_t pow_data, uint8_t update_reg, uint16_t pf) {
    memset(f, 0, hlw8032::kFrameLen);
    f[0] = 0x55;
    f[1] = 0x5A;
    put24(f + 2, vol_par);
    put24(f + 5, vol_data);
    put24(f + 8, cur_par);
    put24(f + 11, cur_data);
    put24(f + 14, pow_par);
    put24(f + 17, pow_data);
    f[20] = update_reg;
    f[21] = (uint8_t)(pf >> 8);
    f[22] = (uint8_t)pf;
    uint8_t sum = 0;
    for (int i = 2; i <= 22; i++) sum = (uint8_t)(sum + f[i]);
    f[23] = sum;
}

// The unit's own factory readings (US mains): 118.38 V, ~1.88 V analog unit.
static void test_nominal_frame() {
    uint8_t f[hlw8032::kFrameLen];
    // vol_par/vol_data * 1.88 = 118.38  ->  11838/10000 * 1.88 = 2.2255? No:
    // the M5Stack driver divides parameter/data and scales by 1.88, so pick
    // 62980/1000 -> 62.98*1.88 = 118.38 V.
    build_frame(f, 62980, 1000, 5000, 10000, 5000, 10000, 0x00, 0x1234);
    hlw8032::Sample s;
    assert(hlw8032::parse_frame(f, sizeof(f), &s));
    assert(s.valid);
    const float v = s.voltage;
    assert(v > 118.3f && v < 118.5f);
    // 0.5 A - 0.06 offset = 0.44 A; 0.5 * 1.88 = 0.94 W
    assert(s.current_a > 0.43f && s.current_a < 0.45f);
    assert(s.power_w > 0.93f && s.power_w < 0.95f);
    assert(s.pf_raw == 0x1234);
}

static void test_no_load_reads_zero_current() {
    uint8_t f[hlw8032::kFrameLen];
    // A socket with nothing plugged in: the chip has no current/power data.
    build_frame(f, 62980, 1000, 0, 0, 0, 0, 0x00, 0);
    hlw8032::Sample s;
    assert(hlw8032::parse_frame(f, sizeof(f), &s));
    assert(s.voltage > 118.3f);
    assert(s.current_a == 0.0f);
    assert(s.power_w == 0.0f);
}

static void test_rejects_bad_frames() {
    uint8_t f[hlw8032::kFrameLen];
    build_frame(f, 62980, 1000, 0, 0, 0, 0, 0, 0);
    hlw8032::Sample s;

    assert(!hlw8032::parse_frame(f, 23, &s));          // short
    uint8_t bad[hlw8032::kFrameLen];
    memcpy(bad, f, sizeof(f));
    bad[0] = 0x54;                                     // no sync
    assert(!hlw8032::parse_frame(bad, sizeof(bad), &s));
    memcpy(bad, f, sizeof(f));
    bad[23] ^= 0xFF;                                   // checksum
    assert(!hlw8032::parse_frame(bad, sizeof(bad), &s));
    memcpy(bad, f, sizeof(f));
    bad[5] = bad[6] = bad[7] = 0;                      // zero voltage divisor
    uint8_t sum = 0;
    for (int i = 2; i <= 22; i++) sum = (uint8_t)(sum + bad[i]);
    bad[23] = sum;
    assert(!hlw8032::parse_frame(bad, sizeof(bad), &s));
}

static void test_assembler_resyncs_through_noise() {
    uint8_t f[hlw8032::kFrameLen];
    build_frame(f, 62980, 1000, 0, 0, 0, 0, 0, 0);

    hlw8032::FrameAssembler a;
    uint8_t out[hlw8032::kFrameLen];
    // Leading noise, then a complete frame.
    const uint8_t noise[] = {0x00, 0x11, 0x22, 0x55, 0x99, 0x5A};
    for (uint8_t b : noise) assert(!a.feed(b, out));
    for (size_t i = 0; i < sizeof(f); i++) {
        const bool got = a.feed(f[i], out);
        assert(got == (i + 1 == sizeof(f)));
    }
    hlw8032::Sample s;
    assert(hlw8032::parse_frame(out, sizeof(out), &s));

    // A corrupted frame followed immediately by a good one: the assembler must
    // drop the bad frame and lock onto the next header pair (0x55 0x5A).
    a.reset();
    uint8_t bad[hlw8032::kFrameLen];
    memcpy(bad, f, sizeof(f));
    bad[23] ^= 0x01;
    for (uint8_t b : bad) assert(!a.feed(b, out));
    for (size_t i = 0; i < sizeof(f); i++) {
        const bool got = a.feed(f[i], out);
        assert(got == (i + 1 == sizeof(f)));
    }
    assert(hlw8032::parse_frame(out, sizeof(out), &s));
}

// ── lease ───────────────────────────────────────────────────────────────
static void test_lease_arms_and_clamps() {
    humidifier::LeaseLimits limits{300, 3600};
    humidifier::Lease lease;

    assert(humidifier::lease_arm(&lease, 0, 1000, limits) == 300);
    assert(lease.on && lease.deadline_ms == 1000 + 300000);
    assert(!humidifier::lease_expired(lease, 1000 + 299999));
    assert(humidifier::lease_expired(lease, 1000 + 300000));

    // Over-max requests are clamped, never refused silently as unbounded.
    assert(humidifier::lease_arm(&lease, 99999, 5000, limits) == 3600);
    assert(lease.deadline_ms == 5000 + 3600000);

    // Renewal keeps the run start (one continuous run, not N short ones).
    const uint32_t start = lease.on_since_ms;
    humidifier::lease_arm(&lease, 60, 60000, limits);
    assert(lease.on_since_ms == start);

    humidifier::lease_clear(&lease, 90000);
    assert(!lease.on);
    assert(lease.on_ms_total == 90000 - start);
}

static void test_lease_wrap_safe() {
    humidifier::LeaseLimits limits{300, 3600};
    humidifier::Lease lease;
    const uint32_t near_wrap = 0xFFFFFFFFu - 1000;
    humidifier::lease_arm(&lease, 300, near_wrap, limits);
    // Deadline wrapped past zero; 299.999 s later it must still be valid.
    assert(!humidifier::lease_expired(lease, near_wrap + 299999));
    assert(humidifier::lease_expired(lease, near_wrap + 300000));
}

static void test_remaining_seconds_rounds_up() {
    humidifier::LeaseLimits limits{300, 3600};
    humidifier::Lease lease;
    humidifier::lease_arm(&lease, 10, 0, limits);
    assert(humidifier::lease_remaining_s(lease, 0) == 10);
    assert(humidifier::lease_remaining_s(lease, 1) == 10);
    assert(humidifier::lease_remaining_s(lease, 9999) == 1);
    assert(humidifier::lease_remaining_s(lease, 10000) == 0);
}

// The ENV III sample is the control input now, so a fabricated reading is a
// safety bug: a CRC-invalid frame (no sensor / bad wiring) must never decode.
static void test_sht30_frame_gate() {
    // 0x6666 -> 25.00 C / 40.00 %RH with valid Sensirion CRCs.
    uint8_t f[6] = {0x66, 0x66, 0, 0x66, 0x66, 0};
    f[2] = lma_sensor::sht30_crc8(f, 2);
    f[5] = lma_sensor::sht30_crc8(f + 3, 2);
    float t = 0, h = 0;
    assert(lma_sensor::sht30_decode(f, &t, &h));
    assert(t > 24.99f && t < 25.01f);
    assert(h > 39.99f && h < 40.01f);

    // Pulled-up bus: 0xFF x6 would otherwise decode to 130 C / 100 %RH.
    uint8_t idle[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
    assert(!lma_sensor::sht30_decode(idle, &t, &h));

    // One flipped bit per half is rejected.
    f[1] ^= 0x01;
    assert(!lma_sensor::sht30_decode(f, &t, &h));
    f[1] ^= 0x01;
    f[4] ^= 0x01;
    assert(!lma_sensor::sht30_decode(f, &t, &h));
}

// ── directive pull: parse + local interlocks ─────────────────────────────

static void test_directive_parse_on_off() {
    humidifier::Directive d;
    const char* on =
        "{\"on\":true,\"lease_s\":300,\"reason\":\"dew 9.4C <= 9.5C\","
        "\"issued_s\":1763700000.123,\"fail_safe\":false}";
    assert(humidifier::parse_directive(on, strlen(on), &d));
    assert(d.valid && d.on && d.lease_s == 300);
    assert(d.issued_s == 1763700000u);

    const char* off = "{\"on\":false,\"lease_s\":0,\"reason\":\"in band\"}";
    assert(humidifier::parse_directive(off, strlen(off), &d));
    assert(d.valid && !d.on && d.lease_s == 0);
}

static void test_directive_parse_rejects_malformed() {
    humidifier::Directive d;
    const char* cases[] = {
        "",                                        // empty
        "not json",                                // not an object
        "[]",                                      // not an object
        "{\"lease_s\":300}",                       // no 'on'
        "{\"on\":\"true\"}",                       // string, not bool
        "{\"on\":true",                            // unterminated
        "{\"on\":true,\"lease_s\":\"300\"}",       // lease not a number
        "{\"on\":true,\"lease_s\":}",              // missing value
        "{\"reason\":{\"on\":true}}",              // nested object
    };
    for (const char* c : cases) {
        assert(!humidifier::parse_directive(c, strlen(c), &d));
        assert(!d.valid);
    }
}

static void test_directive_parse_ignores_on_inside_a_string() {
    humidifier::Directive d;
    // The reason mentions the field; only the real key may set it.
    const char* body =
        "{\"reason\":\"quoted \\\"on\\\":true is not a key\",\"on\":false}";
    assert(humidifier::parse_directive(body, strlen(body), &d));
    assert(!d.on);
}

static void test_directive_interlocks() {
    humidifier::LocalLimits limits{3600, 600};
    humidifier::LocalState st;
    const char* why = "";
    humidifier::Directive on;
    on.valid = true;
    on.on = true;
    on.lease_s = 300;

    // A plain ON directive is applied.
    assert(humidifier::apply_directive(on, 1000, false, 0, limits, &st, &why) ==
           humidifier::Action::On);

    // Continuous run past the cap is cut and starts a cooldown.
    assert(humidifier::apply_directive(on, 3600 * 1000 + 1000, true, 1000, limits,
                                       &st, &why) == humidifier::Action::Off);
    assert(st.cooldown_until_ms == 3600 * 1000u + 1000u + 600 * 1000u);

    // During the cooldown ON is refused...
    assert(humidifier::apply_directive(on, 3600 * 1000u + 2000u, false, 0, limits,
                                       &st, &why) == humidifier::Action::Off);
    // ...and after it, ON is allowed again.
    assert(humidifier::apply_directive(on, 3600 * 1000u + 1000u + 600 * 1000u + 1u,
                                       false, 0, limits, &st, &why) ==
           humidifier::Action::On);

    // An OFF directive and an invalid one both open the relay.
    humidifier::Directive off;
    off.valid = true;
    off.on = false;
    assert(humidifier::apply_directive(off, 5000, true, 4000, limits, &st, &why) ==
           humidifier::Action::Off);
    humidifier::Directive invalid;
    assert(humidifier::apply_directive(invalid, 5000, true, 4000, limits, &st,
                                       &why) == humidifier::Action::Off);
}

int main() {
    test_nominal_frame();
    test_no_load_reads_zero_current();
    test_rejects_bad_frames();
    test_assembler_resyncs_through_noise();
    test_sht30_frame_gate();
    test_lease_arms_and_clamps();
    test_lease_wrap_safe();
    test_remaining_seconds_rounds_up();
    test_directive_parse_on_off();
    test_directive_parse_rejects_malformed();
    test_directive_parse_ignores_on_inside_a_string();
    test_directive_interlocks();
    printf("hlw8032 + relay_lease + directive: all host tests passed\n");
    return 0;
}
