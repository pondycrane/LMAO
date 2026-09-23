#pragma once

#include <string>

#include "rtreticulum/identity.h"

// Node identity persistence shared by the Sprout and Cardputer native firmware.
// Each device stores one 64-byte private key in NVS under a device-specific
// namespace ("sprout" / "cardputer") so a node stays a stable mesh peer across
// boots and its lxmf/delivery hash can be allow-listed on the server.
namespace lma_identity {

    // Load the persisted identity from NVS namespace *ns*, or mint + persist a
    // fresh one on first boot.  NVS must have been initialized by app_main.
    RNS::Identity load_or_create(const char* ns);

    // Hex-encode a byte range (lowercase).  Shared hexstr for log lines.
    std::string hexstr(const void* data, size_t len);

    inline std::string hexstr(const RNS::Bytes& b) {
        return hexstr(b.data(), b.size());
    }

    // The server allow-lists the node's OUT lxmf/delivery destination hash —
    // compute + log it so it can be added to ALLOWED_CLIENTS.
    std::string delivery_hash(const RNS::Identity& identity);

}
