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
                               Unit unit, uint64_t timestamp_ms) {
        std::string r;
        field_varint(r, 1, sensor_id);
        field_fixed32(r, 2, value);
        if (unit != UNIT_UNSPECIFIED) field_varint(r, 3, (uint64_t)unit);
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

    std::string encode_envelope_audio(const std::string& audio_message_bytes) {
        std::string e;
        field_len(e, 21, audio_message_bytes);   // LMAOEnvelope.audio = 21
        return e;
    }

    std::string encode_audio_message(const std::string& node_id,
                                     const std::string& audio_data,
                                     const std::string& codec,
                                     uint32_t duration_ms,
                                     uint64_t timestamp_ms) {
        std::string a;
        field_len(a, 1, node_id);
        field_len(a, 2, audio_data);
        field_len(a, 3, codec);
        field_varint(a, 4, duration_ms);
        field_varint(a, 5, timestamp_ms);
        return a;
    }

}
