#include "lma_chart.h"

#include <cstring>

#include "rtreticulum/msgpack.h"

namespace {

void varint(std::string& o, uint64_t v) {
    while (v >= 0x80) {
        o.push_back((char)((v & 0x7f) | 0x80));
        v >>= 7;
    }
    o.push_back((char)v);
}

void field_varint(std::string& o, uint32_t fn, uint64_t v) {
    varint(o, (fn << 3) | 0);  // wire type 0
    varint(o, v);
}

void field_len(std::string& o, uint32_t fn, const std::string& payload) {
    varint(o, (fn << 3) | 2);  // wire type 2
    varint(o, payload.size());
    o += payload;
}

// ---- protobuf reader ---------------------------------------------------

struct Reader {
    const uint8_t* p;
    size_t n;
    size_t i = 0;

    bool at_end() const { return i >= n; }

    bool read_varint(uint64_t* out) {
        uint64_t v = 0;
        int shift = 0;
        while (i < n && shift < 64) {
            const uint8_t b = p[i++];
            v |= (uint64_t)(b & 0x7f) << shift;
            if (!(b & 0x80)) {
                *out = v;
                return true;
            }
            shift += 7;
        }
        return false;
    }

    bool read_key(uint32_t* field, uint32_t* wire) {
        uint64_t key = 0;
        if (!read_varint(&key)) return false;
        *field = (uint32_t)(key >> 3);
        *wire = (uint32_t)(key & 7);
        return true;
    }

    bool read_len(const uint8_t** data, size_t* len) {
        uint64_t l = 0;
        if (!read_varint(&l)) return false;
        if (l > n - i) return false;
        *data = p + i;
        *len = (size_t)l;
        i += (size_t)l;
        return true;
    }

    bool skip(uint32_t wire) {
        switch (wire) {
            case 0: { uint64_t v; return read_varint(&v); }
            case 1: if (n - i < 8) return false; i += 8; return true;
            case 2: { const uint8_t* d; size_t l; return read_len(&d, &l); }
            case 5: if (n - i < 4) return false; i += 4; return true;
            default: return false;
        }
    }
};

// Append a packed repeated uint32 field, or accept the unpacked form.
bool append_u32s(std::vector<uint32_t>* out, const uint8_t* data, size_t len) {
    Reader r{data, len};
    while (!r.at_end()) {
        uint64_t v = 0;
        if (!r.read_varint(&v)) return false;
        out->push_back((uint32_t)v);
    }
    return true;
}

bool decode_chart_bundle(const uint8_t* data, size_t len,
                         lma_chart::ChartBundle* out) {
    Reader r{data, len};
    while (!r.at_end()) {
        uint32_t fn = 0, wire = 0;
        if (!r.read_key(&fn, &wire)) return false;
        switch (fn) {
            case 1: { uint64_t v; if (!r.read_varint(&v)) return false; out->format = (uint32_t)v; break; }
            case 2: {
                const uint8_t* d; size_t l;
                if (!r.read_len(&d, &l)) return false;
                out->node_id.assign((const char*)d, l);
                break;
            }
            case 3: { uint64_t v; if (!r.read_varint(&v)) return false; out->dry = (uint32_t)v; break; }
            case 4: { uint64_t v; if (!r.read_varint(&v)) return false; out->wet = (uint32_t)v; break; }
            case 5: { uint64_t v; if (!r.read_varint(&v)) return false; out->start_ms = v; break; }
            case 6: { uint64_t v; if (!r.read_varint(&v)) return false; out->period_ms = (uint32_t)v; break; }
            case 7:
            case 8:
            case 9: {
                std::vector<uint32_t>* dst =
                    fn == 7 ? &out->soil : (fn == 8 ? &out->temp : &out->hum);
                if (wire == 0) {
                    uint64_t v;
                    if (!r.read_varint(&v)) return false;
                    dst->push_back((uint32_t)v);
                } else if (wire == 2) {
                    const uint8_t* d; size_t l;
                    if (!r.read_len(&d, &l)) return false;
                    if (!append_u32s(dst, d, l)) return false;
                } else {
                    if (!r.skip(wire)) return false;
                }
                break;
            }
            case 10: { uint64_t v; if (!r.read_varint(&v)) return false; out->watered = v; break; }
            default:
                if (!r.skip(wire)) return false;
                break;
        }
    }
    return true;
}

bool try_parse_lxm_at(const RNS::Bytes& data, size_t off, uint32_t* seq_out,
                      lma_chart::ChartBundle* out) {
    if (off >= data.size()) return false;
    RNS::MsgPack::Reader r(data.data() + off, data.size() - off);
    const uint8_t p = r.peek();
    if (!((p & 0xF0) == 0x90 || p == 0xDC || p == 0xDD)) return false;
    if (r.read_array_header() < 4) return false;
    // [0] timestamp (float64 or int)
    if (r.peek() == 0xCB) r.read_float64();
    else r.read_int();
    // [1] title (bin/str) — consume
    (void)r.read_bin();
    // [2] content (bin) = LMAOEnvelope reply
    RNS::Bytes content = r.read_bin();
    if (content.size() == 0) return false;
    return lma_chart::decode_chart_envelope(content.data(), content.size(), out);
}

}  // namespace

namespace lma_chart {

std::string encode_history_request(uint32_t seq, uint32_t count,
                                   uint64_t since_ms, bool air_temp,
                                   bool air_humidity, bool soil, bool pump) {
    std::string history;
    // proto3 defaults are omitted on the wire (Python's serializer does the
    // same), so a 0 count / 0 since_ms simply does not appear.
    if (count) field_varint(history, 1, count);     // HistoryRequest.count
    if (since_ms) field_varint(history, 2, since_ms);  // HistoryRequest.since_ms
    std::string series;
    if (soil) varint(series, 1);         // Series.SOIL
    if (air_temp) varint(series, 2);     // Series.AIR_TEMP
    if (air_humidity) varint(series, 3); // Series.AIR_HUMIDITY
    if (pump) varint(series, 4);         // Series.PUMP
    if (!series.empty()) field_len(history, 3, series);  // packed enum

    std::string request;
    field_len(request, 1, history);  // Request.history

    std::string envelope;
    field_varint(envelope, 1, seq);       // LMAOEnvelope.seq
    field_len(envelope, 3, request);      // LMAOEnvelope.request
    return envelope;
}

bool decode_chart_envelope(const uint8_t* data, size_t len,
                           ChartBundle* out) {
    if (out == nullptr) return false;
    *out = ChartBundle{};
    Reader r{data, len};
    bool found = false;
    while (!r.at_end()) {
        uint32_t fn = 0, wire = 0;
        if (!r.read_key(&fn, &wire)) return false;
        if (fn == 23 && wire == 2) {  // LMAOEnvelope.chart
            const uint8_t* d; size_t l;
            if (!r.read_len(&d, &l)) return false;
            ChartBundle bundle;
            if (!decode_chart_bundle(d, l, &bundle)) return false;
            *out = bundle;
            found = true;
        } else if (!r.skip(wire)) {
            return false;
        }
    }
    return found;
}

bool parse_lxm_reply(const RNS::Bytes& data, uint32_t* seq_out,
                     ChartBundle* out) {
    if (seq_out) *seq_out = 0;
    if (!out) return false;
    // Canonical opportunistic plaintext: src16 + sig64 + msgpack.
    constexpr size_t kOffNoDest = 16 + 64;
    // Tolerated: dest16 + src16 + sig64 + msgpack.
    constexpr size_t kOffWithDest = 16 + 16 + 64;
    return try_parse_lxm_at(data, kOffNoDest, seq_out, out) ||
           try_parse_lxm_at(data, kOffWithDest, seq_out, out);
}

}  // namespace lma_chart
