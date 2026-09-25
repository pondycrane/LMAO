#include "lma_lxm.h"

#include <cstdint>

// Minimal msgpack reader — only what an inbound LXM payload needs.  LXMF packs
// [timestamp, title, content, fields] and appends a stamp when present, so the
// reader walks the array, skipping anything it does not need (including a
// trailing stamp) instead of requiring an exact 4-element shape.
namespace {

    struct Reader {
        const std::string& b;
        size_t pos = 0;

        explicit Reader(const std::string& buf) : b(buf) {}

        bool need(size_t n) const { return pos + n <= b.size(); }
        uint8_t byte() { return (uint8_t)b[pos++]; }

        bool read_be(uint32_t n, uint64_t* out) {
            if (!need(n)) return false;
            uint64_t v = 0;
            for (uint32_t i = 0; i < n; i++) v = (v << 8) | (uint64_t)byte();
            if (out) *out = v;
            return true;
        }

        // Reads a length-delimited blob body (str/bin) once its length is known.
        bool read_blob(uint64_t len, std::string* out) {
            if (!need((size_t)len)) return false;
            if (out) out->assign(b, pos, (size_t)len);
            pos += (size_t)len;
            return true;
        }

        // Reads any string/binary value into *out (null = discard).
        bool read_str_or_bin(uint8_t lead, std::string* out) {
            uint64_t len = 0;
            if (lead >= 0xa0 && lead <= 0xbf) {            // fixstr
                return read_blob(lead & 0x1f, out);
            }
            switch (lead) {
                case 0xd9: return read_be(1, &len) && read_blob(len, out);   // str8
                case 0xda: return read_be(2, &len) && read_blob(len, out);   // str16
                case 0xdb: return read_be(4, &len) && read_blob(len, out);   // str32
                case 0xc4: return read_be(1, &len) && read_blob(len, out);   // bin8
                case 0xc5: return read_be(2, &len) && read_blob(len, out);   // bin16
                case 0xc6: return read_be(4, &len) && read_blob(len, out);   // bin32
                default: return false;
            }
        }

        // Skips one complete value of any supported type.
        bool skip_value() {
            if (!need(1)) return false;
            const uint8_t lead = byte();

            if (lead <= 0x7f) return true;                              // positive fixint
            if (lead >= 0xe0) return true;                              // negative fixint
            if (lead >= 0x80 && lead <= 0x8f) return skip_n(lead & 0x0f);   // fixmap
            if (lead >= 0x90 && lead <= 0x9f) return skip_n(lead & 0x0f);   // fixarray
            if (lead >= 0xa0 && lead <= 0xbf) return read_blob(lead & 0x1f, nullptr);  // fixstr

            uint64_t n = 0;
            switch (lead) {
                case 0xc0: case 0xc2: case 0xc3: return true;           // nil / false / true
                case 0xc1: return false;                                // never used
                case 0xcc: return read_be(1, nullptr);
                case 0xcd: return read_be(2, nullptr);
                case 0xce: return read_be(4, nullptr);
                case 0xcf: return read_be(8, nullptr);
                case 0xd0: return read_be(1, nullptr);
                case 0xd1: return read_be(2, nullptr);
                case 0xd2: return read_be(4, nullptr);
                case 0xd3: return read_be(8, nullptr);
                case 0xca: return read_be(4, nullptr);                  // float32
                case 0xcb: return read_be(8, nullptr);                  // float64
                case 0xd9: case 0xda: case 0xdb:
                case 0xc4: case 0xc5: case 0xc6:
                    return read_str_or_bin(lead, nullptr);
                case 0xdc: return read_be(2, &n) && skip_n((size_t)n);   // array16
                case 0xdd: return read_be(4, &n) && skip_n((size_t)n);   // array32
                case 0xde: return read_be(2, &n) && skip_n((size_t)n * 2); // map16
                case 0xdf: return read_be(4, &n) && skip_n((size_t)n * 2); // map32
                case 0xd4: return read_be(2, nullptr);   // fixext1: type + 1
                case 0xd5: return read_be(3, nullptr);   // fixext2
                case 0xd6: return read_be(5, nullptr);   // fixext4
                case 0xd7: return read_be(9, nullptr);   // fixext8
                case 0xd8: return read_be(17, nullptr);  // fixext16
                case 0xc7: {                             // ext8
                    uint64_t len = 0;
                    if (!read_be(1, &len)) return false;
                    return read_be(1, nullptr) && read_blob(len, nullptr);
                }
                case 0xc8: {                             // ext16
                    uint64_t len = 0;
                    if (!read_be(2, &len)) return false;
                    return read_be(1, nullptr) && read_blob(len, nullptr);
                }
                case 0xc9: {                             // ext32
                    uint64_t len = 0;
                    if (!read_be(4, &len)) return false;
                    return read_be(1, nullptr) && read_blob(len, nullptr);
                }
                default: return false;
            }
        }

        // Skips `count` consecutive values.
        bool skip_n(size_t count) {
            for (size_t i = 0; i < count; i++) {
                if (!skip_value()) return false;
            }
            return true;
        }
    };

}

namespace lma_lxm {

    bool parse_payload(const std::string& payload, std::string* title, std::string* content) {
        Reader r(payload);
        if (!r.need(1)) return false;
        const uint8_t lead = r.byte();

        uint64_t count = 0;
        if (lead >= 0x90 && lead <= 0x9f) {
            count = lead & 0x0f;
        } else if (lead == 0xdc) {
            if (!r.read_be(2, &count)) return false;
        } else if (lead == 0xdd) {
            if (!r.read_be(4, &count)) return false;
        } else {
            return false;   // not the [ts, title, content, fields] array
        }
        if (count < 3) return false;

        if (!r.skip_value()) return false;                       // [0] timestamp

        if (!r.need(1)) return false;
        if (!r.read_str_or_bin(r.byte(), title)) return false;   // [1] title

        if (!r.need(1)) return false;
        if (!r.read_str_or_bin(r.byte(), content)) return false; // [2] content

        return true;   // trailing fields / stamp need not be parsed
    }

    bool extract_content(const std::string& plaintext, std::string* title, std::string* content) {
        if (plaintext.size() <= HEADER_BYTES) return false;
        const std::string payload = plaintext.substr(HEADER_BYTES);
        return parse_payload(payload, title, content);
    }

}
