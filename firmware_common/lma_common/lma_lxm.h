#pragma once

#include <string>

// Decoding helpers for an inbound LXMF message on the native clients.
//
// The server (reference LXMF) sends opportunistic messages whose decrypted
// plaintext is:
//     source_hash(16) + signature(64) + msgpack([ts, title, content, fields])
// (the destination hash is omitted — the RNS packet header already carries it).
// This unit extracts `title` and `content`; the signature is not verified here
// (identify the peer through RNS.Transport announcements instead, as the
// sensor nodes already do for the server's delivery identity).
//
// Pure C++ (no RTReticulum / ESP-IDF) so the host test — see
// tests/lma_lxm_test.cpp — exercises exactly the device code.
namespace lma_lxm {

    // source_hash(16) + ed25519 signature(64)
    static const size_t HEADER_BYTES = 16 + 64;

    // Parse a decrypted LXM plaintext (source + signature + msgpack payload).
    // `title`/`content` are only written on success.  Strings and binary are
    // both accepted for either slot (reference LXMF packs bytes, µReticulum may
    // pack str).  Returns false for anything malformed or unsupported.
    bool extract_content(const std::string& plaintext, std::string* title, std::string* content);

    // Same, for an already-stripped msgpack payload.  `title` may be null when
    // the caller does not care.
    bool parse_payload(const std::string& payload, std::string* title, std::string* content);

}
