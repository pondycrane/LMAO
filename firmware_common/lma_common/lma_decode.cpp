#include "lma_decode.h"

#include <cstdint>

namespace {

    /* Walk length-delimited protobuf fields; return the payload of the first
     * field with the given number, or the empty string.  Handles only
     * wire-type 2 (length-delimited), which is all LMAOEnvelope/TextMessage
     * string and message fields use. */
    std::string find_len_field(const std::string& data, uint32_t field_number) {
        size_t pos = 0;
        while (pos < data.size()) {
            // key varint
            uint64_t key = 0;
            unsigned shift = 0;
            bool done = false;
            while (pos < data.size() && shift < 64) {
                uint8_t b = (uint8_t)data[pos++];
                key |= (uint64_t)(b & 0x7f) << shift;
                if (!(b & 0x80)) { done = true; break; }
                shift += 7;
            }
            if (!done) return "";
            const uint32_t fn   = (uint32_t)(key >> 3);
            const uint32_t wire = (uint32_t)(key & 7);

            if (wire != 2) return "";          // only length-delimited supported
            uint64_t len = 0;
            shift = 0;
            done = false;
            while (pos < data.size() && shift < 64) {
                uint8_t b = (uint8_t)data[pos++];
                len |= (uint64_t)(b & 0x7f) << shift;
                if (!(b & 0x80)) { done = true; break; }
                shift += 7;
            }
            if (!done || pos + len > data.size()) return "";

            if (fn == field_number)
                return data.substr(pos, (size_t)len);
            pos += (size_t)len;
        }
        return "";
    }

}

namespace lma_decode {

    std::string text_content(const std::string& envelope_bytes) {
        // LMAOEnvelope.text = field 20 → serialized TextMessage
        const std::string text_msg = find_len_field(envelope_bytes, 20);
        if (text_msg.empty()) return "";
        // TextMessage.content = field 2 (string)
        return find_len_field(text_msg, 2);
    }

}
