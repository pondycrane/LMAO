#pragma once

#include <functional>
#include <string>

#include "rtreticulum/destination.h"
#include "rtreticulum/identity.h"
#include "lma_chart.h"

// LXMF/RNS networking for the RoboDog home client:
//   - registers the IN lxmf/delivery destination that receives server replies
//   - announces with an LXMF peer-data display name (the device name)
//   - sends SensorReports (air temp + humidity) and HistoryRequests
namespace env_net {

    using ReplyCallback = std::function<void(const lma_chart::ChartBundle&)>;

    // Register the IN lxmf/delivery destination; `cb` runs on the Reticulum
    // context whenever a ChartBundle reply arrives.
    RNS::Destination init_delivery(const RNS::Identity& identity, ReplyCallback cb);

    // LXMF announce app_data carrying the display name:
    //   msgpack([name(bin), nil, [SF_COMPRESSION]]) — LXMF peer-data shape.
    RNS::Bytes announce_app_data(const std::string& display_name);

    // LMAOEnvelope{SensorReport{sensor_id 3 (air temp), sensor_id 2 (humidity)}}.
    bool send_sensor_report(const RNS::Identity& my_identity,
                            const RNS::Identity& server_identity,
                            const std::string& node_id, uint32_t seq,
                            float temp_c, float rh_pct, uint64_t timestamp_ms);

    // LMAOEnvelope{request.history} for the air series.
    bool send_history_request(const RNS::Identity& my_identity,
                              const RNS::Identity& server_identity,
                              uint32_t seq, uint32_t count, uint64_t since_ms);

}
