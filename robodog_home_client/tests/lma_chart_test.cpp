// Host test for firmware_common/lma_common/lma_chart.{h,cpp}: the air-series
// HistoryRequest encoder and the ChartBundle decoder, checked against golden
// bytes generated from the Python protobuf stubs (proto/lma_messages.proto).
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "lma_chart.h"

static int failures = 0;

#define CHECK(cond)                                                         \
    do {                                                                    \
        if (!(cond)) {                                                      \
            fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
            failures++;                                                     \
        }                                                                   \
    } while (0)

static std::string unhex(const char* hex) {
    std::string out;
    for (size_t i = 0; hex[i] && hex[i + 1]; i += 2) {
        auto nib = [](char c) -> int {
            if (c >= '0' && c <= '9') return c - '0';
            if (c >= 'a' && c <= 'f') return c - 'a' + 10;
            if (c >= 'A' && c <= 'F') return c - 'A' + 10;
            return 0;
        };
        out.push_back((char)((nib(hex[i]) << 4) | nib(hex[i + 1])));
    }
    return out;
}

static std::string hex(const std::string& s) {
    static const char* H = "0123456789abcdef";
    std::string out;
    for (unsigned char c : s) {
        out.push_back(H[c >> 4]);
        out.push_back(H[c & 0xf]);
    }
    return out;
}

// Golden bytes from:
//   pb.LMAOEnvelope(seq=7, request.history{count=30, series=[AIR_TEMP,
//   AIR_HUMIDITY]}).SerializeToString()
static const char* kRequestHex = "08071a080a06081e1a020203";

// Golden bytes from:
//   pb.LMAOEnvelope(chart{format=1, node_id="cardpute",
//   start_ms=1700000000000, period_ms=300000, temp=[214,226], hum=[50,55]})
static const char* kChartHex =
    "ba01210801120863617264707574652880d095ffbc3130e0a7124204d601e2014a023237";

static void test_request_encoding(void) {
    const std::string got =
        lma_chart::encode_history_request(7, 30, 0);
    CHECK(hex(got) == kRequestHex);
}

static void test_chart_decode(void) {
    const std::string raw = unhex(kChartHex);
    lma_chart::ChartBundle b;
    CHECK(lma_chart::decode_chart_envelope(
        (const uint8_t*)raw.data(), raw.size(), &b));
    CHECK(b.format == 1);
    CHECK(b.node_id == "cardpute");
    CHECK(b.start_ms == 1700000000000ULL);
    CHECK(b.period_ms == 300000);
    CHECK(b.temp.size() == 2 && b.temp[0] == 214 && b.temp[1] == 226);
    CHECK(b.hum.size() == 2 && b.hum[0] == 50 && b.hum[1] == 55);
    CHECK(b.soil.empty());
    CHECK(b.watered == 0);
}

static void test_non_chart_envelope_is_rejected(void) {
    // A request envelope has no chart field (23).
    const std::string raw = unhex(kRequestHex);
    lma_chart::ChartBundle b;
    CHECK(!lma_chart::decode_chart_envelope(
        (const uint8_t*)raw.data(), raw.size(), &b));
}

static void test_truncated_input_is_rejected(void) {
    const std::string raw = unhex(kChartHex);
    lma_chart::ChartBundle b;
    CHECK(!lma_chart::decode_chart_envelope(
        (const uint8_t*)raw.data(), raw.size() / 2, &b));
    CHECK(!lma_chart::decode_chart_envelope(nullptr, 0, &b));
}

static void test_unpacked_repeated_field_is_accepted(void) {
    // Some encoders emit repeated uint32 unpacked (wire type 0 per element).
    // Build envelope{chart{temp=215, temp=220}} by hand.
    std::string chart;
    chart.push_back((char)0x40);  // field 8, wire 0
    chart.push_back((char)0xD7);  // 215 varint
    chart.push_back((char)0x01);
    chart.push_back((char)0x40);  // field 8, wire 0
    chart.push_back((char)0xDC);  // 220
    chart.push_back((char)0x01);

    std::string envelope;
    envelope.push_back((char)0xBA);  // field 23, wire 2
    envelope.push_back((char)0x01);
    envelope.push_back((char)chart.size());
    envelope += chart;

    lma_chart::ChartBundle b;
    CHECK(lma_chart::decode_chart_envelope(
        (const uint8_t*)envelope.data(), envelope.size(), &b));
    CHECK(b.temp.size() == 2 && b.temp[0] == 215 && b.temp[1] == 220);
}

static void test_series_selection(void) {
    // Temperature-only request: golden bytes from
    //   pb.LMAOEnvelope(seq=1, request.history{series=[AIR_TEMP]})
    const std::string got =
        lma_chart::encode_history_request(1, 0, 0, /*air_temp=*/true,
                                          /*air_humidity=*/false);
    CHECK(hex(got) == "08011a050a031a0102");
}

int main(void) {
    test_request_encoding();
    test_chart_decode();
    test_non_chart_envelope_is_rejected();
    test_truncated_input_is_rejected();
    test_unpacked_repeated_field_is_accepted();
    test_series_selection();

    if (failures) {
        fprintf(stderr, "%d check(s) failed\n", failures);
        return 1;
    }
    printf("lma_chart_test: all checks passed\n");
    return 0;
}
