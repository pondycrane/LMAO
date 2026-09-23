#include "lxm_recv.h"

#include "rtreticulum/msgpack.h"
#include "rtreticulum/type.h"

namespace {

    constexpr size_t SRC_HASH_LEN  = RNS::Type::Identity::TRUNCATED_HASHLENGTH / 8;  // 16
    constexpr size_t SIGNATURE_LEN = RNS::Type::Identity::SIGLENGTH / 8;             // 64

}

namespace lxm_recv {

    Decoded parse_opportunistic_body(const RNS::Bytes& plaintext) {
        Decoded out;
        if (plaintext.size() < SRC_HASH_LEN + SIGNATURE_LEN + 1)
            return out;

        out.source_hash   = plaintext.left(SRC_HASH_LEN);
        out.signature     = plaintext.mid(SRC_HASH_LEN, SIGNATURE_LEN);
        out.packed_payload = plaintext.mid(SRC_HASH_LEN + SIGNATURE_LEN);
        if (out.packed_payload.empty())
            return out;

        // msgpack([ts, title, content, fields]) — four elements.
        RNS::MsgPack::Reader reader(out.packed_payload);
        if (reader.read_array_header() < 4)
            return out;

        reader.skip();                       // ts
        out.title   = reader.read_bin();     // title (str or bin)
        out.content = reader.read_bin();     // content (str or bin)
        if (!out.title || !out.content)
            return out;

        out.valid = true;
        return out;
    }

    bool validate_signature(const Decoded& decoded,
                            const RNS::Bytes& our_delivery_hash,
                            const RNS::Identity& sender) {
        if (!decoded.valid || !sender)
            return false;

        RNS::Bytes hashed_part;
        hashed_part << our_delivery_hash
                    << decoded.source_hash
                    << decoded.packed_payload;
        RNS::Bytes message_hash = RNS::Identity::full_hash(hashed_part);
        hashed_part << message_hash;

        return sender.validate(decoded.signature, hashed_part);
    }

}
