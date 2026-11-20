#pragma once

#include <string>
#include <vector>

#include "rtreticulum/bytes.h"
#include "rtreticulum/identity.h"

// Send-only LMAO reporting shared by the LAN sensor nodes: build the LXMF
// announce peer-data (display name) and the LMAOEnvelope{SensorReport} wire
// frame, and hand it to RNS.  The Sprout and the RoboDog home client have
// their own fielded copies of this logic; new firmware uses this one so the
// envelope + LXMF framing has a single canonical implementation.
namespace lma_report {

    // LXMF announce app_data carrying the display name (the server's contact
    // book and the host clients see this name):
    //   msgpack([name(bin), nil, [SF_COMPRESSION]])
    RNS::Bytes announce_app_data(const std::string& display_name);

    // Build one opportunistic LXM envelope to the server's lxmf/delivery
    // destination and broadcast it on every registered interface.
    bool send_envelope(const RNS::Identity& my_identity,
                       const RNS::Identity& server_identity,
                       const std::string& envelope, uint64_t unix_s);

    // LMAOEnvelope{SensorReport{node_id, seq, battery, readings}}.  *readings*
    // are lma_encoder::encode_reading() blobs, so the caller decides which
    // sensor ids it can actually supply (a missing sensor is omitted, never
    // sent as a fabricated value).
    bool send_sensor_report(const RNS::Identity& my_identity,
                            const RNS::Identity& server_identity,
                            const std::string& node_id, uint32_t seq,
                            const std::vector<std::string>& readings,
                            uint64_t timestamp_ms, float battery);

}
