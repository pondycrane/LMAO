// Host unit test for the Cardputer native SensorReport/LMAOEnvelope encoder.
//
// Golden bytes verified against the reference Python encoder
// (cardputer_client/proto/lma_encoder.py) so the C++ port must produce
// byte-identical output (wire compatibility with the server's proto).
#include <cstdio>
#include <string>
#include <vector>

#include "lma_encoder.h"

static int failures = 0;

#define CHECK_EQ(actual, expected, label)                                    \
    do {                                                                     \
        std::string a = (actual);                                            \
        std::string e = std::string(expected);                               \
        if (a != e) {                                                        \
            std::printf("FAIL %s\n  actual  : '%s'\n  expected: '%s'\n",     \
                        label, a.c_str(), e.c_str());                        \
            failures++;                                                      \
        } else {                                                             \
            std::printf("ok   %s\n", label);                                 \
        }                                                                    \
    } while (0)

static std::string to_hex(const std::string& b) {
    static const char* H = "0123456789abcdef";
    std::string s;
    for (unsigned char c : b) {
        s.push_back(H[c >> 4]);
        s.push_back(H[c & 0x0f]);
    }
    return s;
}

int main() {
    using namespace lma_encoder;

    // Die temp (id 1, the Cardputer's core reading) and humidity (id 2).
    std::string rd_die = encode_reading(1, 42.5f, "C", 123458);
    CHECK_EQ(to_hex(rd_die), "08011500002a421a014320c2c407", "reading die (id1)");
    std::string rd_h = encode_reading(2, 59.0f, "%", 123457);
    CHECK_EQ(to_hex(rd_h), "08021500006c421a012520c1c407", "reading hum (id2)");

    // One SensorReport bundling die temp + humidity (DHT20 attached).
    std::string rep2 = encode_sensor_report("a1b2c3", 7, 3.7f, {rd_die, rd_h});
    CHECK_EQ(
        to_hex(rep2),
        "0a0661316232633310071dcdcc6c40220e08011500002a421a014320c2c407"
        "220e08021500006c421a012520c1c407",
        "sensor_report_die_hum");
    CHECK_EQ(
        to_hex(encode_envelope(rep2)),
        "522f0a0661316232633310071dcdcc6c40220e08011500002a421a014320c2c407"
        "220e08021500006c421a012520c1c407",
        "lmao_envelope_die_hum");

    // Single reading (die temp only — no DHT20 attached).
    std::string rep1 = encode_sensor_report("a1b2c3", 7, 3.7f, {rd_die});
    CHECK_EQ(
        to_hex(rep1),
        "0a0661316232633310071dcdcc6c40220e08011500002a421a014320c2c407",
        "sensor_report_single");
    CHECK_EQ(
        to_hex(encode_envelope(rep1)),
        "521f0a0661316232633310071dcdcc6c40220e08011500002a421a014320c2c407",
        "lmao_envelope_single");

    if (failures == 0) {
        std::printf("ALL PASS\n");
        return 0;
    }
    std::printf("%d failure(s)\n", failures);
    return 1;
}
