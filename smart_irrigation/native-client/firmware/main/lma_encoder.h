#pragma once

#include <stdint.h>
#include <string>
#include <vector>

// Minimal wire-compatible SensorReport encoder (LMAOEnvelope / SensorReport /
// SensorReading), matching cardputer_client/proto/lma_encoder.py so the server
// LXMF handler + ingest (#127) accept it unchanged.
namespace lma_encoder {

    // LMAOEnvelope{ oneof payload { SensorReport sensor = 10 } }
    std::string encode_envelope(const std::string& sensor_report_bytes);

    // SensorReport{ node_id=1, seq=2, battery=3(float), readings=4 (repeated) }
    std::string encode_sensor_report(const std::string& node_id,
                                     uint32_t seq, float battery,
                                     const std::vector<std::string>& readings);

    // SensorReading{ sensor_id=1, value=2(float), unit=3, timestamp_ms=4 }
    std::string encode_reading(uint32_t sensor_id, float value,
                               const std::string& unit, uint64_t timestamp_ms);

}
