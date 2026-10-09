#pragma once

#include <cstddef>
#include <cstdint>
#include <string>

// Wire-format extraction of codec2 voice audio from an incoming LXMF message
// (Kitchen voice-over-LXMF, issue #234).
//
// The RNS plaintext of an opportunistic LXMF delivery is:
//   [recipient_delivery.hash (16)][signature (64)][payload]
// where payload = msgpack([ts, title(bin), content(bin), fields(map)])
// and `content` is the LMAOEnvelope (proto) — see lxmf_send.cpp (build_body /
// opportunistic_frame) which produces the same layout on TX.
namespace lxmf_audio {

    // Parse an incoming RX payload; on success returns true and fills `audio`
    // with the codec2 700C frames + `codec` with the codec tag ("codec2").
    // Returns false when the message carries no audio (text/sensor/ack...).
    bool extract(const uint8_t* data, size_t len,
                 std::string* audio, std::string* codec);

}
