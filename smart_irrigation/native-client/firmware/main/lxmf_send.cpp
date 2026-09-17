#include "lxmf_send.h"

#include <cstring>
#include "rtreticulum/cryptography/hashes.h"
#include "rtreticulum/msgpack.h"
#include "rtreticulum/packet.h"

using namespace RNS;

namespace {

    // msgpack([ts, title(bin), content(bin), fields(=empty map 0x80)])
    Bytes pack_payload(uint64_t ts, const Bytes& title, const Bytes& content) {
        Bytes out;
        MsgPack::pack_array_header(out, 4);
        MsgPack::pack_int(out, (int64_t)ts);
        MsgPack::pack_bin(out, title);
        MsgPack::pack_bin(out, content);
        out.append(Bytes("\x80", 1));  // empty map (msgpack lib has no map helper)
        return out;
    }

}

namespace lxmf_send {

    Bytes build_body(const Destination& my_delivery,
                     const Destination& server_delivery,
                     const Identity& signer,
                     const Bytes& content,
                     const Bytes& title,
                     uint64_t unix_seconds) {
        Bytes payload = pack_payload(unix_seconds, title, content);

        Bytes hashed;
        hashed.append(server_delivery.hash());
        hashed.append(my_delivery.hash());
        hashed.append(payload);

        Bytes hash = Cryptography::sha256(hashed);
        Bytes signed_part(hashed);
        signed_part.append(hash);
        Bytes signature = signer.sign(signed_part);

        Bytes body;
        body.append(server_delivery.hash());
        body.append(my_delivery.hash());
        body.append(signature);
        body.append(payload);
        return body;
    }

    Bytes opportunistic_frame(const Destination& server_delivery,
                              const Bytes& body) {
        // LXMF opportunistic send drops the 16-byte destination hash (the RNS
        // packet header already carries it).
        if (body.size() < 16) return Bytes();
        Bytes data((const uint8_t*)(body.data() + 16), body.size() - 16);
        Packet pkt(server_delivery, data);
        pkt.pack();
        return pkt.raw();
    }

}
