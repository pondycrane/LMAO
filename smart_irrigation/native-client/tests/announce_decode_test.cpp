// Regression test for issue #133 work item 1: the radio-path ANNOUNCE decode.
//
// A Sprout DTU receives on-air frames as [1-byte RNode/urns LoRa-interface
// header][RNS packet]. uart_at_interface.cpp strips that header and feeds the
// RNS packet to Transport::inbound. This test reproduces that exact radio-path
// framing locally (RTReticulum announce + RNode header wrapper) and asserts:
//   1. Packet::unpack() yields packet_type == ANNOUNCE (0x01) — NOT 0x00.
//   2. Transport::inbound() runs process_announce()/the registered on_announce
//      callback with the correct destination hash (the pre-framing-fix
//      symptom was "type 0x00, so on_announce never fires on the DTU path").
//   3. A plain DATA frame still decodes as DATA (sanity).
#include <cstdio>
#include <memory>

#include "rtreticulum/identity.h"
#include "rtreticulum/destination.h"
#include "rtreticulum/transport.h"
#include "rtreticulum/packet.h"
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

// Simulate uart_at_interface RX: strip the 1-byte RNode/urns LoRa header.
static Bytes strip_rnode_header(const Bytes& onair) {
    if (onair.size() < 2) return Bytes();
    return Bytes(onair.data() + 1, onair.size() - 1);
}

int main() {
    int failures = 0;

    auto iface = Interfaces::LoopbackInterface::create("radio");

    Identity identity;
    Destination dest(identity, Type::Destination::IN, Type::Destination::SINGLE,
                     "spike", "interop");
    Transport::register_destination(dest);

    // A real RTReticulum announce (byte-identical to what Python RNS emits:
    // first byte flags=0x01 => packet_type ANNOUNCE).
    Bytes rns_frame = dest.announce(Bytes(), /*send=*/false);
    if (rns_frame.empty() || (rns_frame.data()[0] & 0x03) != (uint8_t)Type::Packet::ANNOUNCE) {
        std::printf("FAIL: composed announce has unexpected flags byte 0x%02x\n",
                    rns_frame.empty() ? 0 : (int)rns_frame.data()[0]);
        return 1;
    }

    // 1) decode = [RNode header][announce] after the firmware's 1-byte strip.
    {
        uint8_t hdr = 0x31;  // one of the observed 81/31/51/B1 on-air header family
        Bytes onair; onair.append(Bytes(&hdr, 1)); onair.append(rns_frame);
        Packet pkt(strip_rnode_header(onair));
        if (!pkt.unpack()) {
            std::printf("FAIL: Packet::unpack() failed on radio-path announce\n");
            failures++;
        } else if (pkt.packet_type() != Type::Packet::ANNOUNCE) {
            std::printf("FAIL: radio-path announce decoded as type 0x%02x (want 0x01=ANNOUNCE)\n",
                        (int)pkt.packet_type());
            failures++;
        } else if (pkt.destination_hash() != dest.hash()) {
            std::printf("FAIL: radio-path announce dh=%s want %s\n",
                        hexstr(pkt.destination_hash()).c_str(),
                        hexstr(dest.hash()).c_str());
            failures++;
        } else {
            std::printf("PASS: Packet::unpack -> type=0x%02x (ANNOUNCE), dh ok\n",
                        (int)pkt.packet_type());
        }
    }

    // 2) full inbound path: same frame must reach on_announce (work item 1 blocker).
    {
        int announced = 0;
        Bytes seen_dh;
        Transport::on_announce([&](const Bytes& dh, const Identity&, const Bytes&) {
            announced++;
            seen_dh = dh;
        });

        uint8_t hdr = 0xB1;  // another family member
        Bytes onair; onair.append(Bytes(&hdr, 1)); onair.append(rns_frame);
        Transport::inbound(strip_rnode_header(onair), Interface(iface));

        if (announced != 1 || seen_dh != dest.hash()) {
            std::printf("FAIL: inbound announce not recognized (on_announce=%d, dh=%s want %s)\n",
                        announced,
                        seen_dh.empty() ? "<none>" : hexstr(seen_dh).c_str(),
                        hexstr(dest.hash()).c_str());
            failures++;
        } else {
            std::printf("PASS: Transport::inbound -> on_announce fired (%d), dh ok\n", announced);
        }
    }

    // 3) sanity: an outbound DATA packet still decodes as DATA.
    {
        Destination out(identity, Type::Destination::OUT, Type::Destination::SINGLE,
                        "spike", "interop");
        Packet data_pkt(out, Bytes("hello", 5), Type::Packet::DATA);
        data_pkt.pack();
        Packet again(data_pkt.raw());
        if (!again.unpack() || again.packet_type() != Type::Packet::DATA) {
            std::printf("FAIL: DATA sanity decode\n");
            failures++;
        } else {
            std::printf("PASS: DATA sanity decode -> type=0x%02x\n", (int)again.packet_type());
        }
    }

    std::printf(failures == 0 ? "RESULT=PASS\n" : "RESULT=FAIL (%d)\n", failures);
    return failures == 0 ? 0 : 1;
}
