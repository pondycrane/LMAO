#pragma once

/* Inbound LXMF receive — the receive-side mirror of lxmf_send.

 * The server answers a client's SensorReport with an OPPORTUNISTIC LXM
 * message (title "p:Envelope") carrying the reply in the RNS DATA packet.
 * RTReticulum's Transport decrypts the RNS layer (Destination::decrypt) and
 * hands the plaintext to Destination::receive() → the destination's packet
 * callback.  That plaintext is exactly the LXM body the Python server would
 * have packed (minus the 16-byte destination hash, which lives in the RNS
 * packet header), i.e.:
 *
 *     src_hash(16) + signature(64) + msgpack([ts, title, content, fields])
 *
 * This module parses that body and (optionally) verifies the sender's
 * Ed25519 signature, so the Cardputer can lift the reply content
 * (a serialized LMAOEnvelope protobuf holding the TextMessage whose content
 * carries the "ACK ...\nDATA ..." chart line) without porting all of LXMF.
 *
 * Shared by both firmware trees (Sprout can adopt the same receive path),
 * DRY — single canonical copy in firmware_common/lma_common/.
 */

#include <cstdint>
#include <string>

#include "rtreticulum/bytes.h"
#include "rtreticulum/identity.h"

namespace lxm_recv {

    /* Result of parsing a decrypted inbound OPPORTUNISTIC LXM body. */
    struct Decoded {
        bool   valid = false;    /* false when the body was malformed */
        RNS::Bytes  source_hash;       /* sender lxmf/delivery hash (16 bytes) */
        RNS::Bytes  signature;        /* Ed25519 signature (64 bytes) */
        RNS::Bytes  packed_payload;   /* original msgpack bytes [ts,title,content,...] */
        RNS::Bytes  title;            /* element 1 (handles str and bin tags) */
        RNS::Bytes  content;          /* element 2 (serialized LMAOEnvelope) */
    };

    /* Parse the RNS-decrypted plaintext delivered by Destination::receive().
     * Returns valid=false on a body that is too short or not a parseable
     * 4-element array — never throws. */
    Decoded parse_opportunistic_body(const RNS::Bytes& plaintext);

    /* Verify the LXM signature the same way LXMF's unpack_from_bytes does:
     *   hashed_part = dest_hash + src_hash + packed_payload
     *   message_hash = full_hash(hashed_part)      (SHA-256)
     *   signed_part  = hashed_part + message_hash
     * then check `sender` (the learned server identity) against it.  Returns
     * false if validation is impossible (no keys) or the signature is bad. */
    bool validate_signature(const Decoded& decoded,
                            const RNS::Bytes& our_delivery_hash,
                            const RNS::Identity& sender);

}
