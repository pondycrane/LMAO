#include "lma_attachment.h"

#include <cstring>

// Protobuf wire format primitives.  These deliberately mirror the helpers in
// lma_encoder.cpp (same numeric encodings); they are kept local because they
// are also used by the decoder and the field-skipping path, which the encoder
// unit has no need for.
namespace {

    constexpr uint32_t WT_VARINT = 0;
    constexpr uint32_t WT_64BIT  = 1;
    constexpr uint32_t WT_LEN    = 2;
    constexpr uint32_t WT_32BIT  = 5;

    void put_varint(std::string& o, uint64_t v) {
        while (v >= 0x80) {
            o.push_back((char)((v & 0x7f) | 0x80));
            v >>= 7;
        }
        o.push_back((char)v);
    }

    // Reads a varint at *pos; on success advances *pos past it.
    bool get_varint(const std::string& b, size_t& pos, uint64_t* out) {
        uint64_t result = 0;
        int shift = 0;
        while (pos < b.size()) {
            const uint8_t byte = (uint8_t)b[pos++];
            if (shift > 63) return false;                       // malformed
            result |= (uint64_t)(byte & 0x7f) << shift;
            if (!(byte & 0x80)) {
                if (out) *out = result;
                return true;
            }
            shift += 7;
        }
        return false;                                           // truncated
    }

    void put_field_varint(std::string& o, uint32_t fn, uint64_t v) {
        put_varint(o, ((uint64_t)fn << 3) | WT_VARINT);
        put_varint(o, v);
    }

    void put_field_len(std::string& o, uint32_t fn, const std::string& payload) {
        put_varint(o, ((uint64_t)fn << 3) | WT_LEN);
        put_varint(o, payload.size());
        o += payload;
    }

    void put_field_fixed32(std::string& o, uint32_t fn, uint32_t v) {
        put_varint(o, ((uint64_t)fn << 3) | WT_32BIT);
        // little-endian, like every protobuf fixed32
        o.push_back((char)(v & 0xff));
        o.push_back((char)((v >> 8) & 0xff));
        o.push_back((char)((v >> 16) & 0xff));
        o.push_back((char)((v >> 24) & 0xff));
    }

    // Reads the (tag, wire type) pair at *pos.
    bool get_tag(const std::string& b, size_t& pos, uint32_t* fn, uint32_t* wt) {
        uint64_t tag = 0;
        if (!get_varint(b, pos, &tag)) return false;
        *fn = (uint32_t)(tag >> 3);
        *wt = (uint32_t)(tag & 0x07);
        return *fn != 0;
    }

    // Standard protobuf behaviour: unknown/mismatched fields are skipped, so a
    // newer sender (extra fields, e.g. Manifest.meta) never breaks an older
    // receiver.  Groups (wire types 3/4) are not used by LMAF.
    bool skip_field(const std::string& b, size_t& pos, uint32_t wt) {
        switch (wt) {
            case WT_VARINT: return get_varint(b, pos, nullptr);
            case WT_64BIT:  pos += 8; return pos <= b.size();
            case WT_32BIT:  pos += 4; return pos <= b.size();
            case WT_LEN: {
                uint64_t len = 0;
                if (!get_varint(b, pos, &len)) return false;
                pos += (size_t)len;
                return pos <= b.size();
            }
            default: return false;
        }
    }

    bool get_field_len(const std::string& b, size_t& pos, std::string* out) {
        uint64_t len = 0;
        if (!get_varint(b, pos, &len)) return false;
        if (pos + (size_t)len > b.size()) return false;
        if (out) out->assign(b, pos, (size_t)len);
        pos += (size_t)len;
        return true;
    }

    // Reads a varint field that may legally arrive either unpacked (wire type
    // 0) or packed (wire type 2) — protobuf decoders must accept both.
    bool append_repeated_varints(const std::string& b, size_t& pos, uint32_t wt,
                                 std::vector<uint32_t>* out) {
        if (wt == WT_VARINT) {
            uint64_t v = 0;
            if (!get_varint(b, pos, &v)) return false;
            if (out) out->push_back((uint32_t)v);
            return true;
        }
        if (wt == WT_LEN) {
            std::string packed;
            if (!get_field_len(b, pos, &packed)) return false;
            size_t p = 0;
            while (p < packed.size()) {
                uint64_t v = 0;
                if (!get_varint(packed, p, &v)) return false;
                if (out) out->push_back((uint32_t)v);
            }
            return true;
        }
        return false;
    }

    double default_now() {
        using clock = std::chrono::steady_clock;
        return std::chrono::duration<double>(clock::now().time_since_epoch()).count();
    }

}

namespace lma_attachment {

    double retry_delay_seconds(const RetryPolicy& policy, uint32_t attempt) {
        double delay = policy.first_delay_seconds;
        for (uint32_t i = 0; i < attempt; ++i) delay *= policy.backoff_factor;
        return delay;
    }


    // ── Encoders ────────────────────────────────────────────────────────────

    std::string encode_manifest(const Manifest& m) {
        std::string o;
        if (!m.id.empty())              put_field_len(o, 1, m.id);
        if (!m.payload_sha256.empty())  put_field_len(o, 2, m.payload_sha256);
        if (m.kind != 0)                put_field_varint(o, 3, m.kind);
        if (!m.codec.empty())           put_field_len(o, 4, m.codec);
        if (m.chunk_size != 0)          put_field_varint(o, 5, m.chunk_size);
        if (m.chunk_count != 0)         put_field_varint(o, 6, m.chunk_count);
        if (m.total_bytes != 0)         put_field_varint(o, 7, m.total_bytes);
        if (m.sample_rate != 0)         put_field_varint(o, 8, m.sample_rate);
        if (m.channels != 0)            put_field_varint(o, 9, m.channels);
        if (m.duration_ms != 0)         put_field_varint(o, 10, m.duration_ms);
        if (m.width != 0)               put_field_varint(o, 11, m.width);
        if (m.height != 0)              put_field_varint(o, 12, m.height);
        if (!m.node_id.empty())         put_field_len(o, 13, m.node_id);
        if (m.created_ms != 0)          put_field_varint(o, 14, m.created_ms);
        return o;
    }

    std::string encode_chunk(const std::string& id, uint32_t index,
                             const std::string& data, uint32_t crc) {
        std::string o;
        if (!id.empty())     put_field_len(o, 1, id);
        if (index != 0)      put_field_varint(o, 2, index);
        if (!data.empty())   put_field_len(o, 3, data);
        if (crc != 0)        put_field_fixed32(o, 4, crc);
        return o;
    }

    std::string encode_ack(const std::string& id, uint32_t status, uint32_t have_count,
                           const std::vector<uint32_t>& missing, const std::string& reason) {
        std::string o;
        if (!id.empty())         put_field_len(o, 1, id);
        if (status != 0)         put_field_varint(o, 2, status);
        if (have_count != 0)     put_field_varint(o, 3, have_count);
        for (uint32_t idx : missing) put_field_varint(o, 4, idx);
        if (!reason.empty())     put_field_len(o, 5, reason);
        return o;
    }

    std::string encode_capability(uint32_t lmaf_version, const std::vector<uint32_t>& kinds,
                                  const std::vector<std::string>& codecs,
                                  uint32_t max_chunk_size, uint64_t max_attachment_bytes,
                                  uint32_t rx_window, uint32_t airtime_budget_bps) {
        std::string o;
        if (lmaf_version != 0)          put_field_varint(o, 1, lmaf_version);
        for (uint32_t k : kinds)        put_field_varint(o, 2, k);
        for (const auto& c : codecs)    put_field_len(o, 3, c);
        if (max_chunk_size != 0)        put_field_varint(o, 4, max_chunk_size);
        if (max_attachment_bytes != 0)  put_field_varint(o, 5, max_attachment_bytes);
        if (rx_window != 0)             put_field_varint(o, 6, rx_window);
        if (airtime_budget_bps != 0)    put_field_varint(o, 7, airtime_budget_bps);
        return o;
    }

    std::string envelope_manifest(const Manifest& m) {
        std::string o;
        put_field_len(o, 40, encode_manifest(m));
        return o;
    }

    std::string envelope_chunk(const std::string& id, uint32_t index,
                               const std::string& data, uint32_t crc) {
        std::string o;
        put_field_len(o, 41, encode_chunk(id, index, data, crc));
        return o;
    }

    std::string envelope_ack(const std::string& id, uint32_t status, uint32_t have_count,
                             const std::vector<uint32_t>& missing, const std::string& reason) {
        std::string o;
        put_field_len(o, 42, encode_ack(id, status, have_count, missing, reason));
        return o;
    }

    std::string envelope_capability(uint32_t lmaf_version, const std::vector<uint32_t>& kinds,
                                    const std::vector<std::string>& codecs,
                                    uint32_t max_chunk_size, uint64_t max_attachment_bytes,
                                    uint32_t rx_window, uint32_t airtime_budget_bps) {
        std::string o;
        put_field_len(o, 50, encode_capability(lmaf_version, kinds, codecs, max_chunk_size,
                                               max_attachment_bytes, rx_window,
                                               airtime_budget_bps));
        return o;
    }

    // ── Decoding ────────────────────────────────────────────────────────────

    namespace {

        void decode_manifest_into(const std::string& b, Manifest* m) {
            size_t pos = 0;
            while (pos < b.size()) {
                uint32_t fn = 0, wt = 0;
                if (!get_tag(b, pos, &fn, &wt)) return;
                uint64_t v = 0;
                switch (fn) {
                    case 1:  if (wt != WT_LEN || !get_field_len(b, pos, &m->id)) return; break;
                    case 2:  if (wt != WT_LEN || !get_field_len(b, pos, &m->payload_sha256)) return; break;
                    case 3:  if (wt != WT_VARINT || !get_varint(b, pos, &v)) return; m->kind = (uint32_t)v; break;
                    case 4:  if (wt != WT_LEN || !get_field_len(b, pos, &m->codec)) return; break;
                    case 5:  if (wt != WT_VARINT || !get_varint(b, pos, &v)) return; m->chunk_size = (uint32_t)v; break;
                    case 6:  if (wt != WT_VARINT || !get_varint(b, pos, &v)) return; m->chunk_count = (uint32_t)v; break;
                    case 7:  if (wt != WT_VARINT || !get_varint(b, pos, &v)) return; m->total_bytes = v; break;
                    case 8:  if (wt != WT_VARINT || !get_varint(b, pos, &v)) return; m->sample_rate = (uint32_t)v; break;
                    case 9:  if (wt != WT_VARINT || !get_varint(b, pos, &v)) return; m->channels = (uint32_t)v; break;
                    case 10: if (wt != WT_VARINT || !get_varint(b, pos, &v)) return; m->duration_ms = (uint32_t)v; break;
                    case 11: if (wt != WT_VARINT || !get_varint(b, pos, &v)) return; m->width = (uint32_t)v; break;
                    case 12: if (wt != WT_VARINT || !get_varint(b, pos, &v)) return; m->height = (uint32_t)v; break;
                    case 13: if (wt != WT_LEN || !get_field_len(b, pos, &m->node_id)) return; break;
                    case 14: if (wt != WT_VARINT || !get_varint(b, pos, &v)) return; m->created_ms = v; break;
                    default: if (!skip_field(b, pos, wt)) return; break;   // e.g. 15 = meta
                }
            }
        }

        void decode_chunk_into(const std::string& b, Decoded* d) {
            size_t pos = 0;
            while (pos < b.size()) {
                uint32_t fn = 0, wt = 0;
                if (!get_tag(b, pos, &fn, &wt)) return;
                uint64_t v = 0;
                switch (fn) {
                    case 1: if (wt != WT_LEN || !get_field_len(b, pos, &d->chunk_id)) return; break;
                    case 2: if (wt != WT_VARINT || !get_varint(b, pos, &v)) return; d->chunk_index = (uint32_t)v; break;
                    case 3: if (wt != WT_LEN || !get_field_len(b, pos, &d->chunk_data)) return; break;
                    case 4:
                        if (wt != WT_32BIT || pos + 4 > b.size()) return;
                        d->chunk_crc32 = (uint32_t)(uint8_t)b[pos]
                                       | ((uint32_t)(uint8_t)b[pos + 1] << 8)
                                       | ((uint32_t)(uint8_t)b[pos + 2] << 16)
                                       | ((uint32_t)(uint8_t)b[pos + 3] << 24);
                        pos += 4;
                        break;
                    default: if (!skip_field(b, pos, wt)) return; break;
                }
            }
        }

        void decode_ack_into(const std::string& b, Decoded* d) {
            size_t pos = 0;
            while (pos < b.size()) {
                uint32_t fn = 0, wt = 0;
                if (!get_tag(b, pos, &fn, &wt)) return;
                uint64_t v = 0;
                switch (fn) {
                    case 1: if (wt != WT_LEN || !get_field_len(b, pos, &d->ack_id)) return; break;
                    case 2: if (wt != WT_VARINT || !get_varint(b, pos, &v)) return; d->ack_status = (uint32_t)v; break;
                    case 3: if (wt != WT_VARINT || !get_varint(b, pos, &v)) return; d->ack_have = (uint32_t)v; break;
                    case 4: if (!append_repeated_varints(b, pos, wt, &d->ack_missing)) return; break;
                    case 5: if (wt != WT_LEN || !get_field_len(b, pos, &d->ack_reason)) return; break;
                    default: if (!skip_field(b, pos, wt)) return; break;
                }
            }
        }

        void decode_caps_into(const std::string& b, Decoded* d) {
            size_t pos = 0;
            while (pos < b.size()) {
                uint32_t fn = 0, wt = 0;
                if (!get_tag(b, pos, &fn, &wt)) return;
                uint64_t v = 0;
                std::string s;
                switch (fn) {
                    case 1: if (wt != WT_VARINT || !get_varint(b, pos, &v)) return; d->caps_version = (uint32_t)v; break;
                    case 2: if (!append_repeated_varints(b, pos, wt, &d->caps_kinds)) return; break;
                    case 3:
                        if (wt != WT_LEN || !get_field_len(b, pos, &s)) return;
                        d->caps_codecs.push_back(s);
                        break;
                    case 4: if (wt != WT_VARINT || !get_varint(b, pos, &v)) return; d->caps_max_chunk = (uint32_t)v; break;
                    case 5: if (wt != WT_VARINT || !get_varint(b, pos, &v)) return; d->caps_max_bytes = v; break;
                    case 6: if (wt != WT_VARINT || !get_varint(b, pos, &v)) return; d->caps_rx_window = (uint32_t)v; break;
                    case 7: if (wt != WT_VARINT || !get_varint(b, pos, &v)) return; d->caps_airtime_bps = (uint32_t)v; break;
                    default: if (!skip_field(b, pos, wt)) return; break;
                }
            }
        }

    }

    Decoded decode_envelope(const std::string& bytes) {
        Decoded d;
        size_t pos = 0;
        while (pos < bytes.size()) {
            uint32_t fn = 0, wt = 0;
            if (!get_tag(bytes, pos, &fn, &wt)) return d;   // unparsable → NONE

            if (fn == 40 || fn == 41 || fn == 42 || fn == 50) {
                if (wt != WT_LEN) return d;
                std::string inner;
                if (!get_field_len(bytes, pos, &inner)) return d;
                d = Decoded();
                switch (fn) {
                    case 40: decode_manifest_into(inner, &d.manifest); d.payload = Payload::MANIFEST; break;
                    case 41: decode_chunk_into(inner, &d);             d.payload = Payload::CHUNK;    break;
                    case 42: decode_ack_into(inner, &d);               d.payload = Payload::ACK;      break;
                    default: decode_caps_into(inner, &d);              d.payload = Payload::CAPABILITY; break;
                }
                continue;
            }

            // A valid LMAOEnvelope carrying something else (sensor/text/...).
            if (d.payload == Payload::NONE) d.payload = Payload::OTHER;
            if (!skip_field(bytes, pos, wt)) { Decoded none; return none; }
        }
        return d;
    }

    uint32_t crc32(const void* data, size_t len, uint32_t seed) {
        const uint8_t* p = (const uint8_t*)data;
        uint32_t c = seed ^ 0xFFFFFFFFu;
        for (size_t i = 0; i < len; i++) {
            c ^= p[i];
            for (int k = 0; k < 8; k++) {
                c = (c >> 1) ^ (0xEDB88320u & (uint32_t)(-(int32_t)(c & 1u)));
            }
        }
        return c ^ 0xFFFFFFFFu;
    }

    // ── Reassembly ──────────────────────────────────────────────────────────

    Reassembler::Reassembler(const Limits& limits, NowFn now)
        : _limits(limits), _now(std::move(now)) {}

    double Reassembler::now() const {
        return _now ? _now() : default_now();
    }

    Reassembler::Session* Reassembler::find(const std::string& id) {
        auto it = _sessions.find(id);
        return it == _sessions.end() ? nullptr : &it->second;
    }

    Reassembler::Outcome Reassembler::begin(const Manifest& m, Sink sink, std::string* err) {
        auto fail = [&](const char* why) {
            if (err) *err = why;
            return Outcome::BAD_MANIFEST;
        };

        if (m.id.empty())          return fail("empty id");
        if (m.chunk_size == 0)     return fail("chunk_size 0");
        if (m.chunk_count == 0)    return fail("chunk_count 0");
        if (m.total_bytes == 0)    return fail("total_bytes 0");
        if (m.chunk_size > _limits.max_chunk_size) return fail("chunk_size over limit");
        if (m.total_bytes > _limits.max_total_bytes) return fail("total_bytes over limit");
        const uint64_t expected = (m.total_bytes + m.chunk_size - 1) / m.chunk_size;
        if ((uint64_t)m.chunk_count != expected) return fail("chunk_count/total_bytes mismatch");

        Session* existing = find(m.id);
        if (existing) {
            const bool same_shape = existing->man.chunk_size == m.chunk_size
                                 && existing->man.chunk_count == m.chunk_count
                                 && existing->man.total_bytes == m.total_bytes;
            if (same_shape) {
                // Resume: keep what we already hold, refresh the metadata.
                existing->man = m;
                existing->sink = std::move(sink);
                existing->updated = now();
                return Outcome::ACCEPTED;
            }
            _sessions.erase(m.id);   // shape changed — start over
        }

        if (_sessions.size() >= _limits.max_sessions) {
            expire();
            if (_sessions.size() >= _limits.max_sessions) {
                // Evict the least recently updated session.
                auto oldest = _sessions.begin();
                for (auto it = _sessions.begin(); it != _sessions.end(); ++it) {
                    if (it->second.updated < oldest->second.updated) oldest = it;
                }
                _sessions.erase(oldest);
            }
        }

        Session s;
        s.man = m;
        s.sink = std::move(sink);
        s.got.assign(m.chunk_count, false);
        s.have = 0;
        s.updated = now();
        _sessions.emplace(m.id, std::move(s));
        if (err) err->clear();
        return Outcome::ACCEPTED;
    }

    Reassembler::Outcome Reassembler::on_chunk(const std::string& id, uint32_t index,
                                               const std::string& data, uint32_t crc) {
        Session* s = find(id);
        if (!s) return Outcome::NO_SESSION;
        if (index >= s->man.chunk_count) return Outcome::BAD_INDEX;
        if (data.size() > s->man.chunk_size) return Outcome::TOO_LARGE;
        // CRC is optional on the wire (0 = sender did not compute one), but
        // when present it must match before anything is stored.
        if (crc != 0 && crc != crc32(data.data(), data.size())) return Outcome::BAD_CRC;

        s->updated = now();
        if (s->got[index]) return Outcome::DUPLICATE;
        if (s->sink && !s->sink(index, data.data(), data.size())) return Outcome::IO_ERROR;

        s->got[index] = true;
        s->have++;
        return s->have == s->man.chunk_count ? Outcome::COMPLETE : Outcome::ACCEPTED;
    }

    std::vector<uint32_t> Reassembler::missing(const std::string& id) const {
        std::vector<uint32_t> out;
        auto it = _sessions.find(id);
        if (it == _sessions.end()) return out;
        for (uint32_t i = 0; i < it->second.got.size(); i++) {
            if (!it->second.got[i]) out.push_back(i);
        }
        return out;
    }

    uint32_t Reassembler::have_count(const std::string& id) const {
        auto it = _sessions.find(id);
        return it == _sessions.end() ? 0 : it->second.have;
    }

    bool Reassembler::complete(const std::string& id) const {
        auto it = _sessions.find(id);
        return it != _sessions.end() && it->second.have == it->second.man.chunk_count;
    }

    bool Reassembler::manifest(const std::string& id, Manifest* out) const {
        auto it = _sessions.find(id);
        if (it == _sessions.end()) return false;
        if (out) *out = it->second.man;
        return true;
    }

    void Reassembler::reset(const std::string& id) {
        _sessions.erase(id);
    }

    void Reassembler::reset_all() {
        _sessions.clear();
    }

    void Reassembler::expire() {
        const double cutoff = now() - _limits.ttl_seconds;
        for (auto it = _sessions.begin(); it != _sessions.end();) {
            if (it->second.updated < cutoff) it = _sessions.erase(it);
            else ++it;
        }
    }

    uint32_t Reassembler::session_count() const {
        return (uint32_t)_sessions.size();
    }

}
