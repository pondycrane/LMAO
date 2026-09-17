#pragma once

// RNS path-request for the Sprout native client (issue #130).
// Wire-compatible with Reticulum 1.3.x: a DATA/broadcast/HEADER_1 packet to the
// PLAIN control destination rnstransport/path/request whose payload is
//   target_destination_hash(16) + random_tag(16).
// The target node (or a peer that knows its path) answers with a PATH_RESPONSE
// announcement; our normal announce handling learns its identity.
namespace path_find {

    // Request a path to *dst_hex* (16-byte hash as 32 hex chars) on all
    // interfaces. Throttled internally (min 20 s between requests).
    void request(const char* dst_hex);

    // True if at least 20 s have passed since the last request.
    bool can_request();

}
