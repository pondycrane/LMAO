// RTReticulum <-> Python RNS interop spike (host).
// RTReticulum node connects to a Python RNS TCPServerInterface; both sides
// announce a "spike/interop" destination; each should see the other's announce.
#include <cstdio>
#include <cstdlib>
#include <memory>
#include <thread>
#include <chrono>
#include <string>
#include "rtreticulum/identity.h"
#include "rtreticulum/destination.h"
#include "rtreticulum/transport.h"
#include "rtreticulum/reticulum.h"
#include "tcp_interface.h"

using namespace RNS;

static std::string hexstr(const Bytes& b) {
    static const char* H = "0123456789abcdef";
    std::string s; s.reserve(b.size() * 2);
    for (size_t i = 0; i < b.size(); ++i) { s.push_back(H[b.data()[i] >> 4]); s.push_back(H[b.data()[i] & 0x0f]); }
    return s;
}

int main(int argc, char** argv) {
    const char* host = argc > 1 ? argv[1] : "127.0.0.1";
    uint16_t port = argc > 2 ? (uint16_t)atoi(argv[2]) : 42424;
    int run_secs = argc > 3 ? atoi(argv[3]) : 30;
    printf("[interop] RTReticulum host interop -> tcp %s:%u for %ds\n", host, port, run_secs);

    auto tcp = HostTCP::TcpInterface::create(host, port);
    Transport::register_interface(tcp);

    Identity identity;
    printf("[interop] identity hash: %s\n", hexstr(identity.get_salt()).c_str());

    Destination dest(identity, Type::Destination::IN, Type::Destination::SINGLE, "spike", "interop");
    Transport::register_destination(dest);
    printf("[interop] dest hash: %s\n", hexstr(dest.hash()).c_str());

    int peer_ann = 0;
    Transport::on_announce([&](const Bytes& dh, const Identity& peer, const Bytes&) {
        peer_ann++;
        printf("[interop] << ANNOUNCE from peer dest=%s id=%s (total %d)\n",
               hexstr(dh).c_str(), hexstr(peer.hash()).c_str(), peer_ann);
    });

    if (!Reticulum::start(100, 8192, 5)) { printf("[interop] Reticulum::start FAILED\n"); return 1; }

    for (int i = 0; i < run_secs; ++i) {
        std::this_thread::sleep_for(std::chrono::seconds(1));
        if (i % 5 == 0) {
            printf("[interop] announce #%d (peer announces seen so far: %d)\n", i / 5 + 1, peer_ann);
            dest.announce(Bytes(), true);
        }
    }
    Reticulum::stop();
    printf("[interop] done. peer announces seen: %d\n", peer_ann);
    printf("%s\n", peer_ann > 0 ? "INTEROP=OK (saw Python RNS announce)" : "INTEROP=NONE (no peer announce seen)");
    return peer_ann > 0 ? 0 : 2;
}
