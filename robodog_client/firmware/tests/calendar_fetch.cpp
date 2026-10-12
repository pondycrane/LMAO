// Host POSIX interop smoke binary for the RoboDog native calendar fetch:
// connects to a real RNS TCP server, learns the server's lxmf/delivery
// identity from its announce, sends a CalendarRequest, and prints the events
// from the CalendarBundle reply.
//
// Usage: calendar_fetch <server_host> <server_port> [identity_hex_64B]
// Exit:  0 when a bundle arrived within 60 s, 2 on timeout.
// Mirrors smart_irrigation/native-client/host/interop.cpp.
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <chrono>
#include <string>
#include <thread>

#include "rtreticulum/identity.h"
#include "rtreticulum/destination.h"
#include "rtreticulum/transport.h"
#include "rtreticulum/reticulum.h"
#include "tcp_interface.h"
#include "calendar_net.h"
#include "lma_calendar.h"

using namespace RNS;

static std::string hexstr(const Bytes& b) {
    static const char* H = "0123456789abcdef";
    std::string s; s.reserve(b.size() * 2);
    for (size_t i = 0; i < b.size(); ++i) { s.push_back(H[b.data()[i] >> 4]); s.push_back(H[b.data()[i] & 0x0f]); }
    return s;
}

static bool unhex(const char* hex, Bytes* out) {
    size_t n = strlen(hex);
    if (n % 2 != 0) return false;
    out->clear();
    for (size_t i = 0; i < n; i += 2) {
        auto nib = [](char c) -> int {
            if (c >= '0' && c <= '9') return c - '0';
            if (c >= 'a' && c <= 'f') return c - 'a' + 10;
            if (c >= 'A' && c <= 'F') return c - 'A' + 10;
            return -1;
        };
        int hi = nib(hex[i]), lo = nib(hex[i + 1]);
        if (hi < 0 || lo < 0) return false;
        out->append((uint8_t)((hi << 4) | lo));
    }
    return true;
}

static volatile bool s_done = false;
static Identity s_server_identity;
static bool s_have_server = false;

int main(int argc, char** argv) {
    if (argc < 3) {
        fprintf(stderr, "usage: %s <server_host> <server_port> [identity_hex_64B]\n", argv[0]);
        return 64;
    }
    const char* host = argv[1];
    uint16_t port = (uint16_t)atoi(argv[2]);
    printf("[fetch] RoboDog calendar fetch -> tcp %s:%u\n", host, port);

    Identity identity(true);
    if (argc > 3) {
        Bytes prv;
        if (!unhex(argv[3], &prv) || !identity.load_private_key(prv)) {
            fprintf(stderr, "[fetch] bad identity hex\n");
            return 64;
        }
    }
    printf("[fetch] identity hash: %s\n", hexstr(identity.get_salt()).c_str());

    auto tcp = RnsTcp::TcpInterface::create(host, port);
    tcp->start();
    Transport::register_interface(tcp);

    Destination delivery = calendar_net::init(identity, [](uint32_t seq, const lma_calendar::CalendarBundle& b) {
        printf("[fetch] << CalendarBundle seq=%u format=%u calendar=%s watermark=%llu events=%u\n",
               (unsigned)seq, (unsigned)b.format, b.calendar_id.c_str(),
               (unsigned long long)b.watermark_ms, (unsigned)b.events.size());
        for (const auto& ev : b.events) {
            printf("[fetch]   event uid=%s title=\"%s\" start=%llu end=%llu pic=%s%s\n",
                   ev.uid.c_str(), ev.title.c_str(),
                   (unsigned long long)ev.start_ms, (unsigned long long)ev.end_ms,
                   ev.pic.empty() ? "(none)" : ev.pic.c_str(),
                   ev.deleted ? " [deleted]" : "");
        }
        s_done = true;
    });
    printf("[fetch] delivery dest hash: %s\n", hexstr(delivery.hash()).c_str());

    Transport::on_announce([](const Bytes& dh, const Identity& peer, const Bytes&) {
        printf("[fetch] << ANNOUNCE dest=%s id=%s\n", hexstr(dh).c_str(),
               hexstr(peer.get_salt()).c_str());
        // Accept any announce as a candidate server delivery destination and
        // remember its identity (printed for the operator).
        if (!s_have_server) {
            s_server_identity = peer;
            s_have_server = true;
            printf("[fetch] treating as server identity\n");
        }
    });

    if (!Reticulum::start(100, 8192, 5)) { printf("[fetch] Reticulum::start FAILED\n"); return 1; }

    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(60);
    auto last_announce = std::chrono::steady_clock::now() - std::chrono::seconds(60);
    bool sent = false;
    while (!s_done && std::chrono::steady_clock::now() < deadline) {
        tcp->loop();
        const auto now = std::chrono::steady_clock::now();
        if (now - last_announce >= std::chrono::seconds(10)) {
            last_announce = now;
            delivery.announce(Bytes(), true);
        }
        if (!sent && s_have_server) {
            sent = true;
            printf("[fetch] sending CalendarRequest seq=1 since_ms=0\n");
            calendar_net::send_request(identity, s_server_identity, /*seq=*/1, /*since_ms=*/0,
                                       "family", (uint64_t)time(NULL));
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
    Reticulum::stop();

    if (s_done) { printf("[fetch] CALENDAR_FETCH=OK\n"); return 0; }
    printf("[fetch] CALENDAR_FETCH=TIMEOUT\n");
    return 2;
}
