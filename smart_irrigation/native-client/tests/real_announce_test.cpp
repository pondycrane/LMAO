// Issue #133 work item 1 — LIVE radio-path announce regression test.
//
// This feeds the EXACT server announce frame that was captured from the rig
// (the production server's RNS 1.3.5 `lxmf/delivery` announce, as received by
// the Sprout DTU) through the firmware's 1-byte RNode-header strip and into
// Transport::inbound. Before the ratchet fix, RTReticulum logged
// "invalid announce sig dest=dad35b80…" and never fired on_announce — the
// blocker that stopped the LXMF SensorReport being sent.
//
// The announce carries a 32-byte ratchet (packet context flag set, which RNS
// `Destination.announce` sets whenever the identity ratchets), so
// process_announce must include `ratchet` in both the field slicing and the
// signed_data, mirroring RNS.Identity.validate_announce.
#include <cstdio>
#include <cstring>
#include <memory>

#include "rtreticulum/identity.h"
#include "rtreticulum/destination.h"
#include "rtreticulum/transport.h"
#include "rtreticulum/interfaces/loopback.h"

using namespace RNS;

static std::string hexstr(const Bytes& b) {
    static const char* H = "0123456789abcdef";
    std::string s; s.reserve(b.size() * 2);
    for (size_t i = 0; i < b.size(); ++i) {
        s.push_back(H[b.data()[i] >> 4]);
        s.push_back(H[b.data()[i] & 0x0f]);
    }
    return s;
}

static Bytes unhex(const char* h) {
    Bytes out; size_t n = strlen(h) / 2;
    for (size_t i = 0; i < n; i++) {
        auto v = [](char c) -> uint8_t {
            if (c >= '0' && c <= '9') return (uint8_t)(c - '0');
            return (uint8_t)((c | 32) - 'a' + 10);
        };
        uint8_t b = (uint8_t)((v(h[2 * i]) << 4) | v(h[2 * i + 1]));
        out.append(Bytes(&b, 1));
    }
    return out;
}

int main() {
    int failures = 0;

    // Captured live on the rig: [RNode 1-byte header][RNS ANNOUNCE frame].
    const char* onair_hex =
        "102100DAD35B80164B25F7B1474BE86E443702001985AC0EF98F17D26671F2F9EA31C059"
        "3A90FEC1A30579FA68545A0CC0159420789018213F3115239D0ED1036CF3375EE75CD4103"
        "18EDA87FB9420CE7A4CA7886EC60BC318E2C0F0D908DEC6A96169006AAC122284867C15327"
        "265567760F8C3A601CF68414F74D9A341C35BB295CB10BF5E5830F24573FF71C2F2731537D"
        "6DE634BFAA529BE8305E424626C7216912871AB96EEDE37E2EA049FA8433AA004A2BB0D502"
        "8A79743577FCAEDC01A48E1B941A2700B93C40B6C6D616F2D736572766572C09100";
    Bytes onair = unhex(onair_hex);
    if (onair.size() < 20) { std::printf("FAIL: bad fixture\n"); return 1; }

    Bytes rframe(onair.data() + 1, onair.size() - 1);  // uart_at_interface RX strip
    if ((rframe.data()[0] & 0x03) != (uint8_t)Type::Packet::ANNOUNCE) {
        std::printf("FAIL: fixture not an ANNOUNCE frame (flags 0x%02x)\n", (int)rframe.data()[0]);
        failures++;
    }
    if (!(rframe.data()[0] & 0x20)) {
        std::printf("FAIL: fixture context flag not set (ratcheted announce expected)\n");
        failures++;
    }

    auto iface = Interfaces::LoopbackInterface::create("radio");
    int announced = 0;
    Bytes seen_dh, seen_id;
    Transport::on_announce([&](const Bytes& dh, const Identity& peer, const Bytes&) {
        announced++;
        seen_dh = dh;
        seen_id = peer.get_salt();
    });

    Transport::inbound(rframe, Interface(iface));

    Bytes want_dh = unhex("dad35b80164b25f7b1474be86e443702");
    if (announced != 1 || seen_dh != want_dh) {
        std::printf("FAIL: on_announce=%d dh=%s want=dad35b80164b25f7b1474be86e443702\n",
                    announced,
                    seen_dh.empty() ? "<none>" : hexstr(seen_dh).c_str());
        failures++;
    } else {
        std::printf("PASS: server announce validated -> on_announce, dh=dad35b80… id=%s\n",
                    hexstr(seen_id).c_str());
    }

    std::printf(failures == 0 ? "RESULT=PASS\n" : "RESULT=FAIL (%d)\n", failures);
    return failures == 0 ? 0 : 1;
}
