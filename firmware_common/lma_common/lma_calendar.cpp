#include "lma_calendar.h"

namespace {
    void varint(std::string& o, uint64_t v) {
        while (v >= 0x80) {
            o.push_back((char)((v & 0x7f) | 0x80));
            v >>= 7;
        }
        o.push_back((char)v);
    }
    void field_varint(std::string& o, uint32_t fn, uint64_t v) {
        varint(o, (fn << 3) | 0);   // wire type 0
        varint(o, v);
    }
    void field_len(std::string& o, uint32_t fn, const std::string& payload) {
        varint(o, (fn << 3) | 2);   // wire type 2
        varint(o, payload.size());
        o += payload;
    }

    // ── Bounded reader for decoding ──
    struct Reader {
        const uint8_t* p;
        size_t len;
        size_t pos = 0;

        bool read_varint(uint64_t* out) {
            uint64_t v = 0;
            int shift = 0;
            while (pos < len && shift < 64) {
                uint8_t b = p[pos++];
                v |= (uint64_t)(b & 0x7f) << shift;
                if (!(b & 0x80)) { *out = v; return true; }
                shift += 7;
            }
            return false;
        }
        // Read one field tag; returns false on truncation.
        bool read_field(uint32_t* fn, uint32_t* wire) {
            uint64_t tag;
            if (!read_varint(&tag)) return false;
            *fn = (uint32_t)(tag >> 3);
            *wire = (uint32_t)(tag & 0x7);
            return *fn != 0;
        }
        // Length-delimited payload bounds-checked; advances past it.
        bool read_len(const uint8_t** data, size_t* n) {
            uint64_t l;
            if (!read_varint(&l)) return false;
            if (l > len - pos) return false;
            *data = p + pos;
            *n = (size_t)l;
            pos += (size_t)l;
            return true;
        }
        // Skip a field of the given wire type (0,1,2,5). Returns false on
        // truncation or unknown wire type.
        bool skip(uint32_t wire) {
            switch (wire) {
                case 0: { uint64_t v; return read_varint(&v); }
                case 1:
                    if (len - pos < 8) return false;
                    pos += 8;
                    return true;
                case 2: { const uint8_t* d; size_t n; return read_len(&d, &n); }
                case 5:
                    if (len - pos < 4) return false;
                    pos += 4;
                    return true;
                default:
                    return false;
            }
        }
        bool done() const { return pos >= len; }
    };

    bool decode_cal_event(const uint8_t* data, size_t len, lma_calendar::CalEvent* out) {
        Reader r{data, len, 0};
        uint32_t fn, wire;
        while (!r.done()) {
            if (!r.read_field(&fn, &wire)) return false;
            const uint8_t* d;
            size_t n;
            uint64_t v;
            switch (fn) {
                case 1:   // uid (string)
                    if (wire != 2 || !r.read_len(&d, &n)) return false;
                    out->uid.assign((const char*)d, n);
                    break;
                case 2:   // title (string)
                    if (wire != 2 || !r.read_len(&d, &n)) return false;
                    out->title.assign((const char*)d, n);
                    break;
                case 3:   // notes (string)
                    if (wire != 2 || !r.read_len(&d, &n)) return false;
                    out->notes.assign((const char*)d, n);
                    break;
                case 4:   // start_ms (varint)
                    if (wire != 0 || !r.read_varint(&v)) return false;
                    out->start_ms = v;
                    break;
                case 5:   // end_ms (varint)
                    if (wire != 0 || !r.read_varint(&v)) return false;
                    out->end_ms = v;
                    break;
                case 6:   // deleted (bool varint)
                    if (wire != 0 || !r.read_varint(&v)) return false;
                    out->deleted = (v != 0);
                    break;
                case 7:   // pic (string)
                    if (wire != 2 || !r.read_len(&d, &n)) return false;
                    out->pic.assign((const char*)d, n);
                    break;
                default:
                    if (!r.skip(wire)) return false;
                    break;
            }
        }
        return true;
    }
}

namespace lma_calendar {

    std::string encode_calendar_request(const std::string& calendar_id, uint64_t since_ms, uint32_t max_events) {
        std::string r;
        if (!calendar_id.empty()) field_len(r, 1, calendar_id);
        if (since_ms) field_varint(r, 2, since_ms);
        if (max_events) field_varint(r, 3, max_events);
        return r;
    }

    std::string encode_calendar_request_envelope(uint32_t seq, const std::string& calendar_id, uint64_t since_ms, uint32_t max_events, bool request_ack) {
        std::string req;   // Request{ calendar = 3 }
        field_len(req, 3, encode_calendar_request(calendar_id, since_ms, max_events));

        std::string e;
        if (seq) field_varint(e, 1, seq);           // LMAOEnvelope.seq
        if (request_ack) field_varint(e, 2, 1);     // LMAOEnvelope.request_ack
        field_len(e, 3, req);                       // LMAOEnvelope.request
        return e;
    }

    bool decode_calendar_bundle(const uint8_t* data, size_t len, CalendarBundle* out) {
        Reader r{data, len, 0};
        uint32_t fn, wire;
        while (!r.done()) {
            if (!r.read_field(&fn, &wire)) return false;
            const uint8_t* d;
            size_t n;
            uint64_t v;
            switch (fn) {
                case 1:   // format (varint)
                    if (wire != 0 || !r.read_varint(&v)) return false;
                    out->format = (uint32_t)v;
                    break;
                case 2:   // calendar_id (string)
                    if (wire != 2 || !r.read_len(&d, &n)) return false;
                    out->calendar_id.assign((const char*)d, n);
                    break;
                case 3: { // events (repeated CalEvent, length-delimited)
                    if (wire != 2 || !r.read_len(&d, &n)) return false;
                    CalEvent ev;
                    if (!decode_cal_event(d, n, &ev)) return false;
                    out->events.push_back(std::move(ev));
                    break;
                }
                case 4:   // watermark_ms (varint)
                    if (wire != 0 || !r.read_varint(&v)) return false;
                    out->watermark_ms = v;
                    break;
                default:
                    if (!r.skip(wire)) return false;
                    break;
            }
        }
        return true;
    }

    bool decode_calendar_reply(const uint8_t* data, size_t len, uint32_t* seq_out, CalendarBundle* out) {
        Reader r{data, len, 0};
        uint32_t fn, wire;
        bool found = false;
        while (!r.done()) {
            if (!r.read_field(&fn, &wire)) return false;
            const uint8_t* d;
            size_t n;
            uint64_t v;
            switch (fn) {
                case 1:   // seq (varint)
                    if (wire != 0 || !r.read_varint(&v)) return false;
                    if (seq_out) *seq_out = (uint32_t)v;
                    break;
                case 24:  // calendar payload (LD CalendarBundle)
                    if (wire != 2 || !r.read_len(&d, &n)) return false;
                    if (!decode_calendar_bundle(d, n, out)) return false;
                    found = true;
                    break;
                default:
                    if (!r.skip(wire)) return false;
                    break;
            }
        }
        return found;
    }

}
