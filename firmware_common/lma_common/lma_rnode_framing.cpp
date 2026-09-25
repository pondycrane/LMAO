#include "lma_rnode_framing.h"

namespace lma_attachment {

    RnodeFrame parse_rnode_frame(const uint8_t* buf, size_t len) {
        RnodeFrame f;
        if (buf == nullptr || len < 1) return f;
        const uint8_t header = buf[0];
        f.split = (header & RNODE_FLAG_SPLIT) != 0;
        f.seq   = (uint8_t)(header & RNODE_SEQ_MASK);
        f.payload = (const char*)(buf + 1);
        f.len     = len - 1;
        return f;
    }

    RnodeSplitAssembler::RnodeSplitAssembler(size_t max_packet, uint32_t timeout_ms)
        : _max_packet(max_packet), _timeout_ms(timeout_ms) {}

    RnodeSplitAssembler::Result RnodeSplitAssembler::push(const RnodeFrame& frame,
                                                          uint32_t now_ms) {
        Result r;

        // Retire fragments whose partner never came.  Done before the lookup so
        // a reused tag starts cleanly rather than appending to a corpse.
        if (_pending_active && (uint32_t)(now_ms - _partial_at_ms) > _timeout_ms) {
            r.stale = true;
            r.stale_len = (uint32_t)_partial.size();
            _dropped++;
            _pending_active = false;
            _partial.clear();
        }

        if (_pending_active) {
            // The very next frame is the partner, whatever its header says.
            // Measured on air: the second half of a split carries neither the
            // flag nor the first half's tag (see lma_rnode_framing.h), so a
            // receiver that demands either can never reassemble these packets.
            std::string& p = _partial;
            if (p.size() + frame.len > _max_packet) {
                // A real framing error, not a deadline: refuse rather than
                // deliver a truncated packet upward.
                r.dropped = true;
                _dropped++;
                _pending_active = false;
                _partial.clear();
                return r;
            }
            p.append(frame.payload, frame.len);

            r.complete = true;
            r.data = std::move(p);
            _pending_active = false;
            _partial.clear();
            _completed++;
            return r;
        }

        if (!frame.split) {
            // Complete on its own — the common case (announces, small packets).
            r.complete = true;
            r.data.assign(frame.payload, frame.len);
            _completed++;
            return r;
        }

        // Flagged frame, nothing pending: hold it.  The header claims more of
        // this packet is coming, so wait for the next frame.
        _partial.assign(frame.payload, frame.len);
        _partial_at_ms = now_ms;
        _pending_active = true;
        return r;
    }

    void RnodeSplitAssembler::clear() {
        _pending_active = false;
        _partial.clear();
    }

}
