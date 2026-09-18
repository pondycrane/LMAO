// Host unit test for issue #135: RTReticulum on-demand path discovery.
//
// Acceptance — "a path request to a known local destination produces a valid
// PATH_RESPONSE and populates path_table":
//   1. A DATA packet addressed to the PLAIN "rnstransport.path.request"
//      destination (built exactly like path_find.cpp) carrying a local
//      destination's hash is answered with an ANNOUNCE whose context byte is
//      PATH_RESPONSE (0x0B) and whose destination is the requested one.
//   2. Feeding that PATH_RESPONSE back in (as the requester would) runs the
//      normal announce handling and populates the path table for the target.
//   3. Duplicate requests are dropped, and requests for unknown targets are
//      not answered.
#include <cstdio>
#include <memory>
#include <string>
#include <vector>

#include "rtreticulum/bytes.h"
#include "rtreticulum/cryptography/random.h"
#include "rtreticulum/destination.h"
#include "rtreticulum/identity.h"
#include "rtreticulum/interface.h"
#include "rtreticulum/packet.h"
#include "rtreticulum/transport.h"

using namespace RNS;

static int failures = 0;
#define CHECK(cond, label)                                          \
    do {                                                            \
        if (!(cond)) { std::printf("FAIL %s\n", label); failures++; } \
        else        { std::printf("ok   %s\n", label); }           \
    } while (0)

static std::string hexstr(const Bytes& b) {
    static const char* H = "0123456789abcdef";
    std::string s; s.reserve(b.size() * 2);
    for (size_t i = 0; i < b.size(); ++i) {
        s.push_back(H[b.data()[i] >> 4]);
        s.push_back(H[b.data()[i] & 0x0f]);
    }
    return s;
}

// Captures everything the transport broadcasts (in place of a real radio).
struct Recorder : InterfaceImpl {
    std::vector<Bytes> sent;
    Recorder() : InterfaceImpl("recorder") { _online = true; }
    void send_outgoing(const Bytes& data) override { sent.push_back(data); }
};

int main() {
    Transport::reset();
    auto rec = std::make_shared<Recorder>();
    Transport::register_interface(rec);
    Interface reciface(rec);

    // A local destination this node can answer path requests for.
    Identity identity;
    Destination local(identity, Type::Destination::IN, Type::Destination::SINGLE,
                      "pathreq", "test");
    Transport::register_destination(local);

    // The well-known PLAIN path-request destination hash.
    const Bytes& plain = Transport::path_request_hash();
    CHECK(hexstr(plain) == "6b9f66014d9853faab220fba47d02761",
          "path_request_hash matches the RNS reference");

    // Build a path request exactly like path_find.cpp: DATA to the PLAIN
    // control destination, payload = [target(16)][tag(16)].
    Identity no_keys(false);
    Destination reqdest(no_keys, Type::Destination::OUT, Type::Destination::PLAIN, plain);
    Bytes target = local.hash();
    Bytes tag = Cryptography::random(16);
    Bytes payload; payload.append(target); payload.append(tag);
    Packet request(reqdest, payload);   // DATA / CONTEXT_NONE / BROADCAST
    request.pack();
    CHECK(!request.raw().empty(), "path-request packet packs");

    // 1) Inject the request as if it had arrived on the radio interface.
    Transport::inbound(request.raw(), reciface);
    CHECK(rec->sent.size() == 1, "local dest answered with exactly one frame");

    if (!rec->sent.empty()) {
        const Bytes& reply = rec->sent[0];

        // Reply must be an ANNOUNCE (packet type bits = 0x01)...
        CHECK((reply.data()[0] & 0x03) == (uint8_t)Type::Packet::ANNOUNCE,
              "reply is an ANNOUNCE");
        // ... addressed to the requested local destination...
        CHECK(reply.size() > 18 &&
              Bytes(reply.data() + 2, 16) == target &&
              hexstr(Bytes(reply.data() + 2, 16)) == hexstr(target),
              "reply destination hash == requested target");
        // ... and stamped with the PATH_RESPONSE context byte (0x0B).
        Packet parsed(reply);
        if (parsed.unpack()) {
            CHECK((int)parsed.context() == (int)Type::Packet::PATH_RESPONSE,
                  "reply context is PATH_RESPONSE (0x0B)");
        } else {
            CHECK(false, "reply unpacks");
        }

        // 2) Feed the PATH_RESPONSE back in as the requester would; the normal
        //    announce handling must populate the path table for the target.
        Packet before(reply);
        CHECK(before.unpack(), "response parses before injection");
        rec->sent.clear();
        Transport::inbound(reply, reciface);
        CHECK(Transport::has_path(target), "path table populated for requested dest");
        const Transport::PathEntry* pe = Transport::lookup_path(target);
        CHECK(pe != nullptr && pe->next_hop == target, "next_hop is the announced target");
    }

    // 3) A duplicate request (same target + tag) is not answered again.
    rec->sent.clear();
    Transport::inbound(request.raw(), reciface);
    CHECK(rec->sent.empty(), "duplicate path request ignored");

    // 4) A request for an unknown destination is not answered.
    rec->sent.clear();
    Bytes unknown = Cryptography::random(16);
    Bytes payload2; payload2.append(unknown); payload2.append(Cryptography::random(16));
    Packet req2(reqdest, payload2);
    req2.pack();
    Transport::inbound(req2.raw(), reciface);
    CHECK(rec->sent.empty(), "unknown-target path request ignored");

    std::printf(failures == 0 ? "RESULT=PASS\n" : "RESULT=FAIL (%d)\n", failures);
    return failures == 0 ? 0 : 1;
}
