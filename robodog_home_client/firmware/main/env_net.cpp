// LXMF/RNS networking for the RoboDog home client (see env_net.h).
//
// Outbound: LMAOEnvelope -> LXMF body (dest16 + src16 + sig64 + msgpack)
// -> opportunistic RNS packet -> Transport::broadcast (the TCP interface).
//
// Inbound: the server's opportunistic reply is decrypted by Transport for our
// registered IN lxmf/delivery destination and delivered as
//   src16 + sig64 + msgpack([ts, title, content, fields])
// where content is the LMAOEnvelope{chart} reply.
#include "env_net.h"

#include <vector>

#include "esp_log.h"
#include "rtreticulum/msgpack.h"
#include "rtreticulum/transport.h"
#include "lma_encoder.h"
#include "lxmf_send.h"

static const char* TAG = "env_net";

namespace {

// Build + transmit one opportunistic LXM envelope to the server.
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

}  // namespace

namespace env_net {

RNS::Destination init_delivery(const RNS::Identity& identity,
                               ReplyCallback cb) {
    RNS::Destination delivery(identity, RNS::Type::Destination::IN,
                              RNS::Type::Destination::SINGLE, "lxmf",
                              "delivery");
    delivery.set_packet_callback([cb](const RNS::Bytes& data,
                                      const RNS::Packet&) {
        lma_chart::ChartBundle bundle;
        uint32_t seq = 0;
        if (lma_chart::parse_lxm_reply(data, &seq, &bundle)) {
            ESP_LOGI(TAG, "chart reply node=%s temp=%u hum=%u",
                     bundle.node_id.c_str(), (unsigned)bundle.temp.size(),
                     (unsigned)bundle.hum.size());
            if (cb) cb(bundle);
        } else {
            ESP_LOGW(TAG, "unparseable LXM reply (%u bytes)",
                     (unsigned)data.size());
        }
    });
    RNS::Transport::register_destination(delivery);
    ESP_LOGI(TAG, "IN lxmf/delivery registered");
    return delivery;
}

RNS::Bytes announce_app_data(const std::string& display_name) {
    RNS::Bytes out;
    RNS::MsgPack::pack_array_header(out, 3);
    RNS::MsgPack::pack_bin(out, RNS::Bytes(display_name));
    RNS::MsgPack::pack_nil(out);
    RNS::MsgPack::pack_array_header(out, 1);
    RNS::MsgPack::pack_int(out, 1);  // SF_COMPRESSION
    return out;
}

bool send_sensor_report(const RNS::Identity& my_identity,
                        const RNS::Identity& server_identity,
                        const std::string& node_id, uint32_t seq,
                        float temp_c, float rh_pct, uint64_t timestamp_ms) {
    std::vector<std::string> readings;
    readings.push_back(lma_encoder::encode_reading(
        3, temp_c, lma_encoder::UNIT_CELSIUS, timestamp_ms));
    readings.push_back(lma_encoder::encode_reading(
        2, rh_pct, lma_encoder::UNIT_PERCENT, timestamp_ms));
    std::string report =
        lma_encoder::encode_sensor_report(node_id, seq, 3.7f, readings);
    std::string envelope = lma_encoder::encode_envelope(report);
    const uint64_t unix_s =
        timestamp_ms >= 1000 ? timestamp_ms / 1000 : 0;
    const bool ok = send_envelope(my_identity, server_identity, envelope, unix_s);
    ESP_LOGI(TAG, "SensorReport seq=%u temp=%.1fC rh=%.0f%% -> %s",
             (unsigned)seq, temp_c, rh_pct, ok ? "sent" : "FAILED");
    return ok;
}

bool send_history_request(const RNS::Identity& my_identity,
                          const RNS::Identity& server_identity, uint32_t seq,
                          uint32_t count, uint64_t since_ms) {
    std::string envelope =
        lma_chart::encode_history_request(seq, count, since_ms);
    const uint64_t unix_s = (uint64_t)time(nullptr);
    const bool ok = send_envelope(my_identity, server_identity, envelope,
                                  unix_s > 1700000000 ? unix_s : 0);
    ESP_LOGI(TAG, "HistoryRequest seq=%u count=%u since=%llu -> %s",
             (unsigned)seq, (unsigned)count, (unsigned long long)since_ms,
             ok ? "sent" : "FAILED");
    return ok;
}

}  // namespace env_net
