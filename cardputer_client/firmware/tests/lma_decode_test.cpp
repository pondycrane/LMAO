// Host tests for firmware_common/lma_common/lma_decode.{h,cpp} — TextMessage
// content extraction from a serialized LMAOEnvelope (the receive-side mirror
// of lma_encoder).  Golden bytes hand-built as LMAOEnvelope{text=20:
// TextMessage{node_id=1, content=2, timestamp=3}}.
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <string>

#include "lma_decode.h"

namespace {

    void varint(std::string& o, uint64_t v) {
        while (v >= 0x80) { o.push_back((char)((v & 0x7f) | 0x80)); v >>= 7; }
        o.push_back((char)v);
    }
    void field_len(std::string& o, uint32_t fn, const std::string& payload) {
        varint(o, (fn << 3) | 2);
        varint(o, payload.size());
        o += payload;
    }
    void field_varint(std::string& o, uint32_t fn, uint64_t v) {
        varint(o, (fn << 3) | 0);
        varint(o, v);
    }

    std::string envelope_with_text(const std::string& content) {
        std::string tm;
        field_len(tm, 1, "e824ad2d");          // TextMessage.node_id = 1
        field_len(tm, 2, content);             // TextMessage.content = 2
        field_varint(tm, 3, 1721234567000ULL); // TextMessage.timestamp = 3
        std::string env;
        field_len(env, 20, tm);                // LMAOEnvelope.text = 20
        return env;
    }

    void test_returns_text_content() {
        const std::string want =
            "ACK from LMAO Server — received your message (89 bytes)\n"
            "DATA e824ad2d 37 53 2 25 26 2 54 55 3 46 45 47";
        std::string got = lma_decode::text_content(envelope_with_text(want));
        assert(got == want);
    }

    void test_returns_empty_for_non_text_envelope() {
        // LMAOEnvelope with no text field (e.g. sensor=10) → "".
        std::string env;
        field_len(env, 10, "sensor-bytes");
        assert(lma_decode::text_content(env) == "");
    }

    void test_returns_empty_for_sensor_payload_with_text_inside() {
        // Regression guard: sensor payloads can contain arbitrary bytes; the
        // decoder must only pull LMAOEnvelope.text (field 20), never a
        // coincidental nested field.  A sensor=10 payload that itself carries
        // field-20 bytes must come back empty (it is not LMAOEnvelope.text).
        std::string fake_sensor;
        field_len(fake_sensor, 20, "not-real-text");   // nested inside sensor
        std::string env;
        field_len(env, 10, fake_sensor);
        assert(lma_decode::text_content(env) == "");
    }

    void test_ignores_other_text_fields() {
        // A TextMessage without content (node_id only) → "" (field 2 absent).
        std::string tm;
        field_len(tm, 1, "somenode");
        std::string env;
        field_len(env, 20, tm);
        assert(lma_decode::text_content(env) == "");
    }

    void test_empty_input() {
        assert(lma_decode::text_content("") == "");
    }

}

int main() {
    test_returns_text_content();
    test_returns_empty_for_non_text_envelope();
    test_returns_empty_for_sensor_payload_with_text_inside();
    test_ignores_other_text_fields();
    test_empty_input();
    std::puts("lma_decode host tests passed");
    return 0;
}
