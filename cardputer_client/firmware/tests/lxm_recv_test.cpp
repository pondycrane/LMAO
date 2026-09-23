// Host tests for firmware_common/lma_common/lxm_recv.{h,cpp} — the inbound
// LXM body parser + signature verifier (the receive mirror of lxmf_send).
//
// Constructs an OPPORTUNISTIC LXM body the way the Python server packs one —
// src_hash(16) + signature(64) + msgpack([ts, title, content, fields]) — and
// feeds it through parse_opportunistic_body / validate_signature.  Byte
// layout mirrors LXMF.LXMessage.pack() (see /home/pondycrane/LXMF).
#include <cassert>
#include <cstdio>
#include <string>

#include "rtreticulum/bytes.h"
#include "rtreticulum/cryptography/random.h"
#include "rtreticulum/identity.h"
#include "rtreticulum/msgpack.h"

#include "lxm_recv.h"

using RNS::Bytes;
using RNS::Identity;

namespace {

    // msgpack float64
    void pack_float64(Bytes& out, double v) {
        out.append((uint8_t)0xcb);
        uint64_t bits;
        static_assert(sizeof(bits) == sizeof(v));
        __builtin_memcpy(&bits, &v, 8);
        for (int i = 0; i < 8; i++) out.append((uint8_t)(bits >> (56 - 8 * i)));
    }

    // msgpack fixarray header for 4 elements
    void pack_array4(Bytes& out) { out.append((uint8_t)0x94); }

    // msgpack fixstr (len < 32)
    void pack_fixstr(Bytes& out, const char* s) {
        size_t n = strlen(s);
        assert(n < 32);
        out.append((uint8_t)(0xa0 | n));
        out.append((const uint8_t*)s, n);
    }

    // msgpack bin8 for the content bytes
    void pack_bin8(Bytes& out, const Bytes& b) {
        assert(b.size() < 256);
        out.append((uint8_t)0xc4);
        out.append((uint8_t)b.size());
        out.append(b);
    }

    // Build a server-style LXM body exactly as LXMF's pack would:
    //   src_hash + signature + msgpack([ts, title("p:Envelope"), content, {}])
    // and sign it with `signer` against dest_hash + src_hash + payload.
    Bytes build_signed_body(const Bytes& dest_hash, const Bytes& src_hash,
                            const Identity& signer, const Bytes& content_bytes) {
        Bytes payload;
        {
            pack_array4(payload);
            pack_float64(payload, 1721234567.5);        // timestamp
            pack_fixstr(payload, "p:Envelope");         // title
            pack_bin8(payload, content_bytes);          // content
            payload.append((uint8_t)0x80);              // fields = {}  (fixmap empty)
        }

        Bytes hashed_part;
        hashed_part << dest_hash << src_hash << payload;
        Bytes message_hash = Identity::full_hash(hashed_part);
        Bytes signed_part = hashed_part;
        signed_part << message_hash;
        Bytes signature = signer.sign(signed_part);

        Bytes body;
        body << src_hash << signature << payload;
        return body;
    }

    void test_parses_body_and_returns_content() {
        Identity id(true);   // signer
        Bytes dest_hash = RNS::Cryptography::random(16);
        Bytes src_hash = RNS::Cryptography::random(16);
        Bytes content((const uint8_t*)"\x0a\x05fake-protobuf-content", 25);

        Bytes body = build_signed_body(dest_hash, src_hash, id, content);
        lxm_recv::Decoded d = lxm_recv::parse_opportunistic_body(body);
        assert(d.valid);
        assert(d.source_hash == src_hash);
        assert(d.content == content);
        std::string title((const char*)d.title.data(), d.title.size());
        assert(title == "p:Envelope");
    }

    void test_signature_validates_against_signer() {
        Identity id(true);
        Bytes dest_hash = RNS::Cryptography::random(16);
        Bytes src_hash = RNS::Cryptography::random(16);
        Bytes content((const uint8_t*)"hello", 5);

        Bytes body = build_signed_body(dest_hash, src_hash, id, content);
        lxm_recv::Decoded d = lxm_recv::parse_opportunistic_body(body);
        assert(d.valid);
        // The learned server identity (public key only) must verify the sig.
        Bytes pub = id.get_public_key();
        Identity peer;
        peer.load_public_key(pub);
        assert(lxm_recv::validate_signature(d, dest_hash, peer));
    }

    void test_rejects_mismatched_signature() {
        Identity alice(true);
        Identity mallory(true);
        Bytes dest_hash = RNS::Cryptography::random(16);
        Bytes src_hash = RNS::Cryptography::random(16);
        Bytes content((const uint8_t*)"hello", 5);

        Bytes body = build_signed_body(dest_hash, src_hash, alice, content);
        lxm_recv::Decoded d = lxm_recv::parse_opportunistic_body(body);
        assert(d.valid);
        Bytes pub = mallory.get_public_key();
        Identity peer;
        peer.load_public_key(pub);
        assert(!lxm_recv::validate_signature(d, dest_hash, peer));
    }

    void test_rejects_short_garbage() {
        Bytes junk((const uint8_t*)"too short", 9);
        lxm_recv::Decoded d = lxm_recv::parse_opportunistic_body(junk);
        assert(!d.valid);
    }

}

int main() {
    test_parses_body_and_returns_content();
    test_signature_validates_against_signer();
    test_rejects_mismatched_signature();
    test_rejects_short_garbage();
    std::puts("lxm_recv host tests passed");
    return 0;
}
