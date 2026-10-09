#include "lma_decoder.h"

#include <cstdint>

namespace {

    // Returns false on malformed input; advances p past the length or value.
    bool read_varint(const uint8_t*& p, const uint8_t* e, uint64_t& v) {
        v = 0;
        unsigned shift = 0;
        while (p < e && shift < 64) {
            const uint8_t c = *p++;
            v |= (uint64_t)(c & 0x7f) << shift;
            if (!(c & 0x80)) {
                return true;
            }
            shift += 7;
        }
        return false;
    }

    uint64_t field_len(const uint8_t*& p, const uint8_t* e) {
        uint64_t len = 0;
        read_varint(p, e, len);
        return len;
    }

    // Advance past a single field body given its wire type.
    void skip_payload(uint32_t wire, const uint8_t*& p, const uint8_t* e) {
        switch (wire) {
            case 0: {  // varint
                uint64_t v;
                read_varint(p, e, v);
                break;
            }
            case 1:   // 64-bit
                p += 8;
                break;
            case 2: {  // length-delimited
                uint64_t len = field_len(p, e);
                p += (size_t)len;
                break;
            }
            case 5:   // 32-bit
                p += 4;
                break;
            default:
                p = e;   // unknown wire type: stop parsing safely
                break;
        }
    }

    // Walks a proto message; for each field where field_num == want, returns
    // the length-delimited payload (wire 2). Any other field is skipped.
    bool find_len_field(const uint8_t*& p, const uint8_t* e, uint32_t want,
                        const uint8_t*& payload, size_t& len) {
        while (p < e) {
            uint64_t tag = 0;
            if (!read_varint(p, e, tag)) {
                return false;
            }
            const uint32_t num = (uint32_t)(tag >> 3);
            const uint32_t wire = (uint32_t)(tag & 7);
            if (num == want && wire == 2) {
                const uint64_t l = field_len(p, e);
                if (p + l > e) {
                    return false;
                }
                payload = p;
                len = (size_t)l;
                return true;
            }
            skip_payload(wire, p, e);
            if (p > e) {
                return false;
            }
        }
        return false;
    }

}

namespace lma_decoder {

    std::string extract_envelope_audio(const std::string& envelope,
                                       std::string* codec_out) {
        if (codec_out) {
            codec_out->clear();
        }
        if (envelope.empty()) {
            return {};
        }
        const uint8_t* p = (const uint8_t *)envelope.data();
        const uint8_t* e = p + envelope.size();

        const uint8_t* am = nullptr;
        size_t am_len = 0;
        if (!find_len_field(p, e, 21, am, am_len)) {
            return {};   // not an audio payload (text/sensor/ack etc.)
        }
        const uint8_t* ape = am + am_len;
        const uint8_t* data = nullptr;
        size_t data_len = 0;
        const uint8_t* q = am;
        find_len_field(q, ape, 2, data, data_len);   // AudioMessage.audio_data
        const uint8_t* r = am;
        const uint8_t* codec = nullptr;
        size_t codec_len = 0;
        find_len_field(r, ape, 3, codec, codec_len); // AudioMessage.codec
        if (codec_out && codec && codec_len) {
            codec_out->assign((const char *)codec, codec_len);
        }
        if (data && data_len) {
            return std::string((const char *)data, data_len);
        }
        return {};
    }

}
