#pragma once

#include "rtreticulum/bytes.h"
#include "rtreticulum/destination.h"
#include "rtreticulum/identity.h"

// Send-only LXMF for Sprout (wire-compatible with the canonical urns/slim
// lxmf overlay): builds the LXM message body addressed to the server's
// "lxmf/delivery" destination and hands it to RNS for on-air transmission.
namespace lxmf_send {

    // Build the full LXM wire body:
    //   dest16 + src16 + ed25519_sig + msgpack([ts, title, content, fields])
    // where the 64-byte signature covers sha256(dest16+src16+packed_payload).
    // *server_delivery* / *my_delivery* are the "lxmf"/"delivery" OUT delivery
    // destinations whose .hash are the LXMF delivery hashes (16 bytes).
    RNS::Bytes build_body(const RNS::Destination& my_delivery,
                          const RNS::Destination& server_delivery,
                          const RNS::Identity& signer,
                          const RNS::Bytes& content,
                          const RNS::Bytes& title,
                          uint64_t unix_seconds);

    // Build the RNS frame for an opportunistic send: Packet(server_delivery,
    // body[minues 16-byte dest hash]). Return its raw() after pack().
    RNS::Bytes opportunistic_frame(const RNS::Destination& server_delivery,
                                   const RNS::Bytes& body);
}
