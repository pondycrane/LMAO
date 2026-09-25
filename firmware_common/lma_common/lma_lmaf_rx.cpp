#include "lma_lmaf_rx.h"

#include "lma_lxm.h"

#include <cstring>

namespace lma_attachment {

    LmafReceiver::LmafReceiver(LmafRxConfig config, VerifyFn verify, Reassembler::NowFn now)
        : _config(std::move(config)), _verify(std::move(verify)), _now(now) {
        Limits limits;
        limits.max_total_bytes = _config.max_payload_bytes;
        limits.max_chunk_size  = _config.max_chunk_size;
        limits.max_sessions    = _config.max_sessions;
        limits.ttl_seconds     = _config.ttl_seconds;
        _reasm = Reassembler(limits, now);
    }

    bool LmafReceiver::maybe_request_manifest(const std::string& id) {
        const double now = _now ? _now() : 0.0;

        auto it = _manifest_requests.find(id);
        if (it == _manifest_requests.end()) {
            // Bound the bookkeeping: drop the least recently asked entry.
            if (_manifest_requests.size() >= 4) {
                auto oldest = _manifest_requests.begin();
                for (auto scan = _manifest_requests.begin(); scan != _manifest_requests.end(); ++scan) {
                    if (scan->second.last < oldest->second.last) oldest = scan;
                }
                _manifest_requests.erase(oldest);
            }
            it = _manifest_requests.emplace(id, ManifestRequest()).first;
        }

        ManifestRequest& req = it->second;
        if (req.count >= max_manifest_requests) return false;
        if (req.count > 0 && (now - req.last) < manifest_request_interval) return false;

        req.count++;
        req.last = now;
        set_ack(id, ACK_NEED, {}, "manifest");
        return true;
    }

    void LmafReceiver::set_ack(const std::string& id, uint32_t status,
                               const std::vector<uint32_t>& missing,
                               const std::string& reason) {
        _has_ack     = true;
        _ack_id      = id;
        _ack_status  = status;
        _ack_missing = missing;
        _ack = envelope_ack(id, status, _reasm.have_count(id), missing, reason);
    }

    bool LmafReceiver::is_completed(const std::string& id) {
        if (id.empty()) return false;
        const double now = _now ? _now() : 0.0;
        auto it = _completed.find(id);
        if (it == _completed.end()) return false;
        if (it->second <= now) { _completed.erase(it); return false; }
        return true;
    }

    bool LmafReceiver::remember_completed(const std::string& id) {
        if (id.empty()) return false;
        const double now = _now ? _now() : 0.0;
        _completed[id] = now + _config.completed_ttl_seconds;
        // Cap the memory at the configured size, dropping the entry that
        // expires first (the oldest delivery).
        while (_completed.size() > (size_t)_config.completed_memory && !_completed.empty()) {
            auto oldest = _completed.begin();
            for (auto it = _completed.begin(); it != _completed.end(); ++it) {
                if (it->second < oldest->second) oldest = it;
            }
            _completed.erase(oldest);
        }
        return true;
    }

    LmafReceiver::Result LmafReceiver::on_manifest(const Manifest& m) {
        // A sender's retry (see RetryPolicy) re-offers the manifest of a
        // transfer this receiver already delivered and verified.  Answer
        // COMPLETE so it stops, without touching storage or asking for a single
        // chunk: the retry must cost one small ack, not a second download.
        if (is_completed(m.id)) {
            set_ack(m.id, ACK_COMPLETE, {}, "duplicate");
            return Result::COMPLETE;
        }

        // Reject before touching storage: every field here comes off the wire.
        if (m.id.empty() || m.chunk_size == 0 || m.chunk_count == 0
            || m.total_bytes == 0 || m.total_bytes > _config.max_payload_bytes
            || m.chunk_size > _config.max_chunk_size) {
            set_ack(m.id, ACK_REJECTED, {}, "limits");
            return Result::MANIFEST_REJECTED;
        }

        // A re-sent manifest must not wipe the chunks already on disk/buffer:
        // only (re)allocate when this is not a resume of the same shape.
        Manifest prev;
        const bool resuming = _reasm.manifest(m.id, &prev)
                              && prev.chunk_size == m.chunk_size
                              && prev.chunk_count == m.chunk_count
                              && prev.total_bytes == m.total_bytes
                              && _payload.size() == (size_t)m.total_bytes;
        if (!resuming) {
            _payload.assign((size_t)m.total_bytes, '\0');
        }

        const uint32_t chunk_size = m.chunk_size;
        auto sink = [this, chunk_size](uint32_t index, const char* data, size_t len) -> bool {
            const size_t off = (size_t)index * chunk_size;
            if (off + len > _payload.size()) return false;
            if (len) memcpy(&_payload[off], data, len);
            return true;
        };

        std::string err;
        if (_reasm.begin(m, sink, &err) != Reassembler::Outcome::ACCEPTED) {
            _payload.clear();
            set_ack(m.id, ACK_REJECTED, {}, err.empty() ? "manifest" : err);
            return Result::MANIFEST_REJECTED;
        }
        _manifest = m;
        return Result::MANIFEST_ACCEPTED;
    }

    LmafReceiver::Result LmafReceiver::on_chunk(const Decoded& d) {
        // A late chunk for an already-delivered transfer: the sender has (or is
        // about to get) the COMPLETE ack from the re-offered manifest, so
        // acking every duplicate chunk would only spend uplink airtime.
        if (is_completed(d.chunk_id)) return Result::IGNORED;

        switch (_reasm.on_chunk(d.chunk_id, d.chunk_index, d.chunk_data, d.chunk_crc32)) {
            case Reassembler::Outcome::ACCEPTED:
                return Result::CHUNK_ACCEPTED;

            case Reassembler::Outcome::DUPLICATE:
                return Result::CHUNK_DUPLICATE;

            case Reassembler::Outcome::BAD_CRC:
                set_ack(d.chunk_id, ACK_NEED, { d.chunk_index }, "crc");
                return Result::NEED_ACK;

            case Reassembler::Outcome::COMPLETE: {
                Manifest m;
                const bool known = _reasm.manifest(d.chunk_id, &m);
                const bool ok = known && _verify && _verify(_payload, m.payload_sha256);
                if (!ok) {
                    // Data arrived intact per-chunk but the whole payload does
                    // not hash: abort rather than loop on retransmits.
                    set_ack(d.chunk_id, ACK_ABORTED, {}, "hash");
                    _payload.clear();
                    _reasm.reset(d.chunk_id);
                    return Result::HASH_MISMATCH;
                }
                _manifest = m;
                set_ack(d.chunk_id, ACK_COMPLETE, {}, "");
                remember_completed(d.chunk_id);
                _reasm.reset(d.chunk_id);   // payload is kept; the session is done
                return Result::COMPLETE;
            }

            case Reassembler::Outcome::IO_ERROR:
                set_ack(d.chunk_id, ACK_ABORTED, {}, "io");
                _payload.clear();
                _reasm.reset(d.chunk_id);
                return Result::IO_ERROR;

            case Reassembler::Outcome::BAD_INDEX:
            case Reassembler::Outcome::TOO_LARGE:
                // Inconsistent framing (index or chunk size disagree with the
                // manifest) cannot be fixed by a resend.
                set_ack(d.chunk_id, ACK_ABORTED, {}, "framing");
                _payload.clear();
                _reasm.reset(d.chunk_id);
                return Result::MANIFEST_REJECTED;

            case Reassembler::Outcome::NO_SESSION:
                // The manifest (first packet of the transfer) never arrived, so
                // the offset of every later chunk is unknown.  Ask the sender to
                // resend the whole transfer (NEED with no indices) — bounded, so
                // a lossy link cannot become a request storm.
                if (!maybe_request_manifest(d.chunk_id)) return Result::NO_SESSION;
                return Result::NEED_ACK;

            case Reassembler::Outcome::BAD_MANIFEST:
            default:
                return Result::IGNORED;
        }
    }

    LmafReceiver::Result LmafReceiver::feed(const std::string& plaintext) {
        _has_ack = false;
        _ack.clear();
        _ack_id.clear();
        _ack_missing.clear();
        _ack_status = 0;

        std::string title, content;
        if (!lma_lxm::extract_content(plaintext, &title, &content)) {
            return Result::IGNORED;
        }
        const Decoded d = decode_envelope(content);
        switch (d.payload) {
            case Payload::MANIFEST: return on_manifest(d.manifest);
            case Payload::CHUNK:    return on_chunk(d);
            default:                return Result::IGNORED;
        }
    }

    std::string LmafReceiver::capability_envelope() const {
        return envelope_capability(_config.lmaf_version, _config.kinds, _config.codecs,
                                   LO_OPP_CHUNK_SIZE, _config.max_payload_bytes,
                                   _config.rx_window);
    }

}
