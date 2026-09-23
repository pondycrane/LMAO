#include "lma_encoder.h"

#include <cstring>

namespace {
    void varint(std::string& o, uint64_t v) {
        while (v >= 0x80) {
            o.push_back((char)((v & 0x7f) | 0x80));
            v >>= 7;
        }
        o.push_back((char)v);
    }
    void field_varint(std::string& o, uint32_t fn, uint64_t v) {
        varint(o, (fn << 3) | 0);   // wire type 0
        varint(o, v);
    }
    void field_len(std::string& o, uint32_t fn, const std::string& payload) {
        varint(o, (fn << 3) | 2);   // wire type 2
        varint(o, payload.size());
        o += payload;
    }
    void field_fixed32(std::string& o, uint32_t fn, float f) {
        varint(o, (fn << 3) | 5);   // wire type 5
        uint32_t bits;
        memcpy(&bits, &f, 4);
        o.push_back((char)(bits & 0xff));
        o.push_back((char)((bits >> 8) & 0xff));
        o.push_back((char)((bits >> 16) & 0xff));
        o.push_back((char)((bits >> 24) & 0xff));
    }
}

namespace lma_encoder {

    std::string encode_reading(uint32_t sensor_id, float value,
                               const std::string& unit, uint64_t timestamp_ms) {
        std::string r;
        field_varint(r, 1, sensor_id);
        field_fixed32(r, 2, value);
        field_len(r, 3, unit);
        field_varint(r, 4, timestamp_ms);
        return r;
    }

    std::string encode_sensor_report(const std::string& node_id,
                                     uint32_t seq, float battery,
                                     const std::vector<std::string>& readings) {
        std::string r;
        field_len(r, 1, node_id);
        field_varint(r, 2, seq);
        field_fixed32(r, 3, battery);
        for (const auto& rd : readings) field_len(r, 4, rd);
        return r;
    }

    std::string encode_envelope(const std::string& sensor_report_bytes) {
        std::string e;
        field_len(e, 10, sensor_report_bytes);   // LMAOEnvelope.sensor = 10
        return e;
    }

}
