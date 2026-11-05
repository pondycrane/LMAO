#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

#include "rtreticulum/bytes.h"

// Air-series wire codec for the RoboDog home client (and any env display).
// It implements ONLY the fields pinned in proto/lma_messages.proto:
//
//   LMAOEnvelope     { uint32 seq = 1; Request request = 3; ChartBundle chart = 23; }
//   Request          { oneof kind { HistoryRequest history = 1; } }
//   HistoryRequest   { uint32 count = 1; uint64 since_ms = 2;
//                      repeated Series series = 3; }   // packed enum
//   ChartBundle      { uint32 format = 1; string node_id = 2; uint32 dry = 3;
//                      uint32 wet = 4; uint64 start_ms = 5; uint32 period_ms = 6;
//                      repeated uint32 soil = 7; repeated uint32 temp = 8;
//                      repeated uint32 hum = 9; uint64 watered = 10; }
//   Series           { SERIES_UNKNOWN=0; SOIL=1; AIR_TEMP=2; AIR_HUMIDITY=3;
//                      PUMP=4; }
//
// Wire units: ChartBundle.temp is TENTHS of a degree Celsius; hum is integer
// percent.  The encoder emits the air-only request (series = AIR_TEMP +
// AIR_HUMIDITY), which the server answers from its all-node air buffer
// (lma_core/env_history.py).
//
// The pure protobuf part is host-testable (no ESP-IDF); parse_lxm_reply needs
// RTReticulum's msgpack reader for the LXMF body wrapper.
namespace lma_chart {

    struct ChartBundle {
        uint32_t format = 0;
        std::string node_id;
        uint32_t dry = 0;
        uint32_t wet = 0;
        uint64_t start_ms = 0;
        uint32_t period_ms = 0;
        std::vector<uint32_t> soil;  // %
        std::vector<uint32_t> temp;  // tenths of degC
        std::vector<uint32_t> hum;   // %
        uint64_t watered = 0;
    };

    // LMAOEnvelope{ seq, request.history } with the selected series.  The
    // default selection is air-only (AIR_TEMP + AIR_HUMIDITY).
    std::string encode_history_request(uint32_t seq, uint32_t count,
                                       uint64_t since_ms,
                                       bool air_temp = true,
                                       bool air_humidity = true,
                                       bool soil = false,
                                       bool pump = false);

    // Decode LMAOEnvelope{ chart } (field 23).  Returns false on malformed
    // input or when no chart field is present.
    bool decode_chart_envelope(const uint8_t* data, size_t len,
                               ChartBundle* out);

    // Decode an inbound LXMF opportunistic plaintext
    // (src16 + sig64 + msgpack([ts, title, content, fields])) and extract the
    // ChartBundle from its content envelope.  Tolerates the dest16-prefixed
    // form some peers emit.  Returns false when the reply is not a chart.
    bool parse_lxm_reply(const RNS::Bytes& data, uint32_t* seq_out,
                         ChartBundle* out);

}
