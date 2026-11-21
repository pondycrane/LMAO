// Send-only LMAO reporting (see lma_report.h).
//
// Outbound: LMAOEnvelope -> LXMF body (dest16 + src16 + sig64 + msgpack)
// -> opportunistic RNS packet -> Transport::broadcast (all interfaces).
#include "lma_report.h"

#include "esp_log.h"
#include "rtreticulum/destination.h"
#include "rtreticulum/msgpack.h"
#include "rtreticulum/transport.h"
#include "rtreticulum/type.h"

#include "lma_encoder.h"
#include "lxmf_send.h"

static const char* TAG = "lma_report";

namespace lma_report {

RNS::Bytes announce_app_data(const std::string& display_name) {
    RNS::Bytes out;
    RNS::MsgPack::pack_array_header(out, 3);
    RNS::MsgPack::pack_bin(out, RNS::Bytes(display_name));
    RNS::MsgPack::pack_nil(out);
    RNS::MsgPack::pack_array_header(out, 1);
    RNS::MsgPack::pack_int(out, 1);  // SF_COMPRESSION
    return out;
}

bool send_envelope(const RNS::Identity& my_identity,
                   const RNS::Identity& server_identity,
                   const std::string& envelope, uint64_t unix_s) {
    RNS::Destination my_delivery(my_identity, RNS::Type::Destination::OUT,
                                 RNS::Type::Destination::SINGLE, "lxmf",
                                 "delivery");
    RNS::Destination server_delivery(
        server_identity, RNS::Type::Destination::OUT,
        RNS::Type::Destination::SINGLE, "lxmf", "delivery");
    RNS::Bytes body = lxmf_send::build_body(
        my_delivery, server_delivery, my_identity, RNS::Bytes(envelope),
        RNS::Bytes("p:Envelope"), unix_s);
    RNS::Bytes frame = lxmf_send::opportunistic_frame(server_delivery, body);
    if (frame.empty()) {
        ESP_LOGW(TAG, "empty opportunistic frame");
        return false;
    }
    RNS::Transport::broadcast(frame, nullptr);
    return true;
}

bool send_sensor_report(const RNS::Identity& my_identity,
                        const RNS::Identity& server_identity,
                        const std::string& node_id, uint32_t seq,
                        const std::vector<std::string>& readings,
                        uint64_t timestamp_ms, float battery) {
    if (readings.empty()) {
        ESP_LOGW(TAG, "no readings — nothing to report");
        return false;
    }
    std::string report =
        lma_encoder::encode_sensor_report(node_id, seq, battery, readings);
    std::string envelope = lma_encoder::encode_envelope(report);
    const uint64_t unix_s = timestamp_ms >= 1000 ? timestamp_ms / 1000 : 0;
    const bool ok = send_envelope(my_identity, server_identity, envelope, unix_s);
    ESP_LOGI(TAG, "SensorReport node='%s' seq=%u readings=%u -> %s",
             node_id.c_str(), (unsigned)seq, (unsigned)readings.size(),
             ok ? "sent" : "FAILED");
    return ok;
}

}  // namespace lma_report
