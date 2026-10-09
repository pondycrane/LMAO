#include "lxmf_audio.h"

#include "lma_decoder.h"
#include <cstdint>

namespace {

    // Minimal msgpack reader for the LXMF payload layout only:
    // [timestamp, title(bin), content(bin), fields(map)].
    // Handles the encodings the reference packer emits (fixint/int8..64,
    // uint8..64, bin8/16/32 + fixstr/str8, fixarray/array16/array32,
    // fixmap/map16/map32). Self-contained so the shared component stays
    // host-testable (no RTReticulum Bytes/tlsf dependency).
    class MReader {
    public:
        MReader(const uint8_t* d, size_t n) : p(d), e(d + n) {}

        bool at_end() const { return p >= e; }

        // Peek the format byte without consuming.
        uint8_t peek() const { return p < e ? *p : 0; }

        bool read_int(int64_t& v) {
            if (p >= e) return false;
            const uint8_t m = *p++;
            if (m <= 0x7f) { v = m; return true; }
            if (m >= 0xe0) { v = (int8_t)m; return true; }
            switch (m) {
                case 0xcc: return take_uint(1, (uint64_t&)v);
                case 0xcd: return take_uint(2, (uint64_t&)v);
                case 0xce: return take_uint(4, (uint64_t&)v);
                case 0xcf: return take_uint(8, (uint64_t&)v);
                case 0xd0: return take_int(1, v);
                case 0xd1: return take_int(2, v);
                case 0xd2: return take_int(4, v);
                case 0xd3: return take_int(8, v);
                default: return false;
            }
        }

        // Read a length-delimited binary/string value.
        bool read_bin(const uint8_t*& out, size_t& len) {
            if (p >= e) return false;
            const uint8_t m = *p++;
            size_t n = 0;
            if (m >= 0xa0 && m <= 0xbf) { n = m - 0xa0; }        // fixstr
            else if (m == 0xc4) { if (!take_uint(1, (uint64_t&)n)) return false; }
            else if (m == 0xc5) { if (!take_uint(2, (uint64_t&)n)) return false; }
            else if (m == 0xc6) { if (!take_uint(4, (uint64_t&)n)) return false; }
            else if (m == 0xd9) { if (!take_uint(1, (uint64_t&)n)) return false; }
            else { return false; }
            if (p + n > e) return false;
            out = p;
            p += n;
            len = n;
            return true;
        }

        size_t read_array_header() {
            if (p >= e) return 0;
            const uint8_t m = *p++;
            if (m >= 0x90 && m <= 0x9f) return m - 0x90;         // fixarray
            size_t n = 0;
            if (m == 0xdc && take_uint(2, (uint64_t&)n)) return n;
            if (m == 0xdd && take_uint(4, (uint64_t&)n)) return n;
            return 0;
        }

        // Skip one value (map/array/basic/binary).
        bool skip() {
            if (p >= e) return false;
            const uint8_t m = *p++;
            if (m <= 0x7f || m >= 0xe0) return true;             // fixint
            if (m >= 0xa0 && m <= 0xbf) { p += m - 0xa0; return true; }
            if (m >= 0x90 && m <= 0x9f) {                        // fixarray
                for (size_t i = 0; i < m - 0x90; i++) if (!skip()) return false;
                return true;
            }
            switch (m) {
                case 0x80: case 0x90: case 0xc0:                 // fixmap(0)/fixarray(0)/nil
                    return true;
                case 0xc2: case 0xc3: return true;               // bool
                case 0xc4: { uint64_t n; if (!take_uint(1, n)) return false; p += n; return true; }
                case 0xc5: { uint64_t n; if (!take_uint(2, n)) return false; p += n; return true; }
                case 0xc6: { uint64_t n; if (!take_uint(4, n)) return false; p += n; return true; }
                case 0xcc: case 0xd0: p += 1; return true;
                case 0xcd: case 0xd1: p += 2; return true;
                case 0xce: case 0xd2: case 0xdc: { uint64_t n; if (!take_uint(2, n)) return false; p += 2; return true; }
                case 0xcf: case 0xd3: p += 8; return true;
                case 0xdd: { uint64_t n; if (!take_uint(4, n)) return false; p += 4; return true; }
                case 0xd9: { uint64_t n; if (!take_uint(1, n)) return false; p += n; return true; }
                default: return false;
            }
        }

    private:
        bool take_uint(size_t n, uint64_t& v) {
            if (p + n > e) return false;
            v = 0;
            for (size_t i = 0; i < n; i++) v = (v << 8) | *p++;
            return true;
        }
        bool take_int(size_t n, int64_t& v) {
            uint64_t u = 0;
            if (!take_uint(n, u)) return false;
            // sign-extend from n bytes
            if (n < 8 && (u & (1ULL << (n * 8 - 1)))) u |= ~((1ULL << (n * 8)) - 1);
            v = (int64_t)u;
            return true;
        }
        const uint8_t* p;
        const uint8_t* e;
    };

}

namespace lxmf_audio {

    bool extract(const uint8_t* data, size_t len,
                 std::string* audio, std::string* codec) {
        if (!data || len < 16 + 64 + 1) {
            return false;
        }
        // Incoming opportunistic LXMF plaintext:
        //   [recipient_delivery.hash (16)][signature (64)][payload]
        // (see lxmf_send.cpp build_body/opportunistic_frame).
        const uint8_t* payload = data + 16 + 64;
        const size_t plen = len - 16 - 64;

        MReader r(payload, plen);
        if (r.read_array_header() != 4) {
            return false;
        }
        int64_t ts = 0;
        if (!r.read_int(ts)) {                 // timestamp
            return false;
        }
        const uint8_t* title = nullptr;
        size_t title_len = 0;
        if (!r.read_bin(title, title_len)) {   // title
            return false;
        }
        const uint8_t* content = nullptr;
        size_t content_len = 0;
        if (!r.read_bin(content, content_len)) {   // the LMAOEnvelope
            return false;
        }
        if (!r.at_end()) {
            r.skip();                          // fields map
        }
        if (audio) {
            audio->clear();
        }
        const std::string env((const char *)content, content_len);
        std::string c;
        const std::string got = lma_decoder::extract_envelope_audio(env, &c);
        if (got.empty()) {
            return false;                      // text/sensor/ack message
        }
        if (audio) {
            *audio = got;
        }
        if (codec) {
            codec->clear();
            *codec = c;
        }
        return true;
    }

}
