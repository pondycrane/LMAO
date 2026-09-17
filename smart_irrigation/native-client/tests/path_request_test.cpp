// Regression test for issue #133 work item 2: RTReticulum's PLAIN-destination
// path. Sprout builds a path request to the plain "rnstransport.path.request"
// destination with a KEYLESS Identity so it can learn the server path on
// demand (rather than only via the server's periodic announce).
//
// Pre-fix both Destination ctors threw "PLAIN destination cannot hold an
// identity" for Identity(false) because operator bool() is true as soon as the
// identity object exists (even with no key material). The fix adds
// Identity::has_key() and uses it in the Destination ctors + name/hash.
//
// Checks:
//   1. Both ctors accept keyless Identity for PLAIN (no throw).
//   2. The name-taking ctor yields the RNS-wire-compatible PLAIN hash for
//      rnstransport.path.request (constant reference, matches Python RNS).
//   3. path_find.cpp's sequence builds + packs a DATA path-request packet.
#include <cstdio>

#include "rtreticulum/bytes.h"
#include "rtreticulum/cryptography/random.h"
#include "rtreticulum/destination.h"
#include "rtreticulum/identity.h"
#include "rtreticulum/packet.h"

using namespace RNS;

static const char* REFERENCE_PLAIN_HASH =
    "6b9f66014d9853faab220fba47d02761";  // trunc(sha256(trunc(sha256("rnstransport.path.request"))))

static std::string hexstr(const Bytes& b) {
    static const char* H = "0123456789abcdef";
    std::string s; s.reserve(b.size() * 2);
    for (size_t i = 0; i < b.size(); ++i) {
        s.push_back(H[b.data()[i] >> 4]);
        s.push_back(H[b.data()[i] & 0x0f]);
    }
    return s;
}

int main() {
    int failures = 0;

    // Independent reference computation (the timeless RNS way).
    {
        Bytes name_bytes("rnstransport.path.request");
        Bytes name_hash = Identity::full_hash(name_bytes)
                              .left(Type::Identity::NAME_HASH_LENGTH / 8);
        Bytes dh = Identity::truncated_hash(name_hash);
        std::string h = hexstr(dh);
        if (h != REFERENCE_PLAIN_HASH) {
            std::printf("FAIL: reference plain hash %s want %s\n",
                        h.c_str(), REFERENCE_PLAIN_HASH);
            failures++;
        }
    }

    Identity no_keys(false);
    if (no_keys.has_key()) {
        std::printf("FAIL: Identity(false) reported has_key()=true\n");
        failures++;
    }

    // 1) precomputed-hash ctor (what path_find.cpp uses).
    try {
        Bytes name_hash = Identity::full_hash(Bytes("rnstransport.path.request"))
                              .left(Type::Identity::NAME_HASH_LENGTH / 8);
        Bytes dh = Identity::truncated_hash(name_hash);
        Destination pl(no_keys, Type::Destination::OUT, Type::Destination::PLAIN, dh);
        if (pl.type() != Type::Destination::PLAIN || pl.direction() != Type::Destination::OUT) {
            std::printf("FAIL: precomputed PLAIN dest wrong type/dir\n");
            failures++;
        } else {
            std::printf("PASS: precomputed-hash ctor accepts keyless identity\n");
        }
    } catch (const std::exception& e) {
        std::printf("FAIL: precomputed-hash ctor threw: %s\n", e.what());
        failures++;
    }

    // 2) name-taking ctor — hash must match the RNS reference.
    try {
        Destination pl(no_keys, Type::Destination::OUT, Type::Destination::PLAIN,
                       "rnstransport", "path.request");
        std::string h = hexstr(pl.hash());
        if (h != REFERENCE_PLAIN_HASH) {
            std::printf("FAIL: name-taking PLAIN hash %s want %s\n",
                        h.c_str(), REFERENCE_PLAIN_HASH);
            failures++;
        } else {
            std::printf("PASS: name-taking ctor hash %s == RNS reference\n", h.c_str());
        }
    } catch (const std::exception& e) {
        std::printf("FAIL: name-taking ctor threw: %s\n", e.what());
        failures++;
    }

    // 3) path_find()'s exact sequence: build plain dest, pack a DATA
    //    path-request packet ([16B target][16B tag]) and verify it packs.
    try {
        Bytes dh = Identity::truncated_hash(
            Identity::full_hash(Bytes("rnstransport.path.request"))
                .left(Type::Identity::NAME_HASH_LENGTH / 8));
        Destination pl(no_keys, Type::Destination::OUT, Type::Destination::PLAIN, dh);

        uint8_t tgt[16] = {0};  tgt[0] = 0xAB;
        Bytes dst(tgt, 16);
        Bytes tag = Cryptography::random(16);
        Bytes payload; payload.append(dst); payload.append(tag);

        Packet pkt(pl, payload);  // DATA / CONTEXT_NONE / BROADCAST
        pkt.pack();
        if (pkt.raw().empty()) {
            std::printf("FAIL: path-request packet empty after pack\n");
            failures++;
        } else {
            std::printf("PASS: path-request packet packs (%uB)\n", (unsigned)pkt.raw().size());
        }
    } catch (const std::exception& e) {
        std::printf("FAIL: path-request sequence threw: %s\n", e.what());
        failures++;
    }

    std::printf(failures == 0 ? "RESULT=PASS\n" : "RESULT=FAIL (%d)\n", failures);
    return failures == 0 ? 0 : 1;
}
