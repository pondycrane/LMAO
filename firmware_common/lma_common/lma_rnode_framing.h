#pragma once

#include <cstddef>
#include <cstdint>
#include <string>

// RNode wire framing — the 1-byte header on every LoRa frame.
//
// Bit 0 marks a split: the packet continues in the *next* frame.  Payloads over
// 254 B are split (508 B max), which is how an LMAF manifest or chunk rides a
// LoRa frame.
//
// What the wire actually does — measured on air, from the addressed node's own
// frame log, for a packet from the server's RNode:
//
//   RX frame 255 B header=0xcb split=1 tag=c0     <- first half, flagged
//   RX frame 102 B header=0x50 split=0 tag=50     <- second half, NOT flagged,
//                                                    different tag
//
// So the pair is "flagged frame, then the next frame" — the second half carries
// neither the flag nor the same tag.  Implementations that require either (RNS's
// own reference rule is `isSplitPacket && seq == sequence`) can never assemble
// those packets; this node spent a whole session logging `dropping stale split
// fragment (254 B)` for exactly those halves while every packet addressed to it
// was silently lost.
//
// Hence: a flagged frame opens a pair and the very next frame completes it,
// whatever its header says.  The tag is kept for logging only.  Pure logic (no
// radio, no ESP-IDF), host-tested, used by the device interface.
namespace lma_attachment {

    static const uint8_t RNODE_SEQ_MASK   = 0xF0;
    static const uint8_t RNODE_FLAG_SPLIT = 0x01;

    struct RnodeFrame {
        bool        split = false;
        uint8_t     seq   = 0;          // tag, upper nibble of the header
        const char* payload = nullptr;  // borrowed from the caller's buffer
        size_t      len     = 0;
    };

    // Never throws.  A zero-length frame yields an empty, split=false result.
    RnodeFrame parse_rnode_frame(const uint8_t* buf, size_t len);

    class RnodeSplitAssembler {
    public:
        struct Result {
            bool        complete = false;  // `data` holds a whole RNS packet
            bool        dropped  = false;  // this push discarded something
            bool        stale    = false;  // an aged-out fragment was discarded
            uint32_t    stale_len = 0;
            std::string data;
        };

        explicit RnodeSplitAssembler(size_t max_packet = 1024,
                                     uint32_t timeout_ms = 500);

        // Feed one parsed frame.  `now_ms` is a monotonic millisecond clock.
        Result push(const RnodeFrame& frame, uint32_t now_ms);

        uint32_t completed() const { return _completed; }
        uint32_t dropped()   const { return _dropped; }
        size_t   in_flight() const { return _pending_active ? 1 : 0; }
        void     clear();

    private:
        size_t      _max_packet;
        uint32_t    _timeout_ms;
        // One pair in flight, as the wire sends it: a flagged frame, then the
        // next frame.  The tag is not usable for pairing (measured above).
        bool        _pending_active = false;
        std::string _partial;
        uint32_t    _partial_at_ms = 0;
        uint32_t    _completed = 0;
        uint32_t    _dropped   = 0;
    };

}
