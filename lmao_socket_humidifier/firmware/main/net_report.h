#pragma once

#include <stdint.h>

// Report the Atom Socket's own ENV III reading to the LMAO server, so the room
// keeps its air history even when the socket is the only sensor in the room
// (the RoboDog home board is no longer required for humidity control).
//
// Send-only: the socket announces its node destination (so the server's
// contact book learns its name) and sends LMAOEnvelope{SensorReport} to the
// server's lxmf/delivery destination over the Reticulum TCP interface.  No IN
// destination is registered — nothing is addressed back to the socket.
namespace net_report {

    struct Config {
        const char* host;   // server RNS TCP host (tp4 / localhost)
        uint16_t port;      // server RNS TCP port (4246)
        const char* name;   // SensorReport.node_id + announce display name
    };

    // Load the pinned identity (HF_NODE_IDENTITY_HEX, NVS fallback) and, when
    // provisioned, bake the server identity from HF_SERVER_PUBKEY_HEX.
    void init(const Config& cfg);

    // After WiFi is up: open the TCP interface, register the node destination
    // and start the Reticulum loop.  Returns false when RNS cannot start; the
    // HTTP relay control keeps working without it.
    bool start();

    // True when the server identity is known and the TCP link is connected.
    bool ready();

    // Announce + one SensorReport per interval.  Call every main-loop tick;
    // sensor_ok=false skips the report (never fabricate a reading).
    void tick(uint32_t now_ms, bool sensor_ok, float temp_c, float hum_pct);

    // /status diagnostics.
    bool server_known();
    uint32_t reports_sent();
    uint32_t reports_failed();

}
