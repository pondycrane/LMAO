// Ratchet interoperability tests (reference RNS 0.7/0.8 support).
//
// The server (reference Python RNS) ratchets opportunistic downlink to a peer
// once that peer announces a ratchet with the FLAG_SET announce, and the
// peer's destination decrypts with its retained ratchet private (falling back
// to the base key).  These tests exercise the exact wire exchange:
//
//   1. DOWNLINK (the server -> device case): the device announces a ratchet
//      (FLAG_SET); the "server" encrypts a DATA packet against that ratchet;
//      the device decrypts it via its ratchet private.
//   2. UPLINK (device -> server): the server announces its ratchet; the device
//      remembers it and encrypts its report against it; the "server" decrypts.
//   3. LEGACY FALLBACK: a peer that never announces a ratchet still gets
//      base-key encryption and decryption untouched.
#include <cstdio>
#include <cstdlib>
#include <memory>
#include <string>

#include "rtreticulum/identity.h"
#include "rtreticulum/destination.h"
#include "rtreticulum/cryptography/hkdf.h"
#include "rtreticulum/transport.h"
#include "rtreticulum/packet.h"
#include "rtreticulum/interfaces/loopback.h"

using namespace RNS;

static int failures = 0;

static void check(bool ok, const char* label) {
    std::printf("%s %s\n", ok ? "ok  " : "FAIL", label);
    if (!ok) failures++;
}

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

    auto iface = Interfaces::LoopbackInterface::create("ratchet-test");

    /* ---- 1. DOWNLINK: server -> device, ratcheted ---- */
    {
        Identity device;                                    // the native client's identity
        Destination dev_in(device, Type::Destination::IN, Type::Destination::SINGLE,
                           "ratchet", "device");
        dev_in.create_ratchet();
        check(!dev_in.ratchets().empty(), "downlink: device owns a ratchet");
        Transport::register_destination(dev_in);

        Bytes delivered;
        dev_in.set_packet_callback([&](const Bytes& plaintext, const Packet&) {
            delivered = plaintext;
        });

        // Device announces its ratchet with FLAG_SET, over the radio path.
        Bytes announce = dev_in.announce(Bytes(), /*send=*/false);
        {
            Packet pkt(announce);
            check(pkt.unpack(), "downlink: device announce unpacks");
            check(pkt.context_flag() == Type::Packet::FLAG_SET,
                  "downlink: device announce carries FLAG_SET (ratchet present)");
            check(pkt.destination_hash() == dev_in.hash(),
                  "downlink: announce addressed to the device destination");
        }
        Transport::inbound(announce, Interface(iface));
        check(!Identity::get_ratchet(dev_in.hash()).empty(),
              "downlink: transport remembered the device's ratchet (FLAG_SET announce)");

        // "Server" builds an OUT destination for the device from its public key.
        Identity dev_pub(false);
        dev_pub.load_public_key(device.get_public_key());
        Destination dev_out(dev_pub, Type::Destination::OUT, Type::Destination::SINGLE,
                            "ratchet", "device");
        check(dev_out.hash() == dev_in.hash(), "downlink: OUT addr matches the IN destination");

        const std::string secret("chart-payload-bytes");
        {
            Packet data_pkt(dev_out, Bytes(secret.data(), secret.size()),
                            Type::Packet::DATA);
            data_pkt.pack();
            check(!data_pkt.ratchet_id().empty(),
                  "downlink: ratcheted packet carries a ratchet id");
            Transport::inbound(data_pkt.raw(), Interface(iface));
        }
        check(delivered.size() == secret.size() &&
                  std::string((const char*)delivered.data(), delivered.size()) == secret,
              "downlink: device decrypted the ratcheted chart payload");
    }

    /* ---- 2. UPLINK: device -> server, ratcheted ---- */
    {
        Identity server;                                    // the server's identity
        Destination srv_in(server, Type::Destination::IN, Type::Destination::SINGLE,
                           "ratchet", "server");
        srv_in.create_ratchet();
        Transport::register_destination(srv_in);

        Bytes delivered;
        srv_in.set_packet_callback([&](const Bytes& plaintext, const Packet&) {
            delivered = plaintext;
        });

        Bytes srv_announce = srv_in.announce(Bytes(), /*send=*/false);
        Transport::inbound(srv_announce, Interface(iface));
        check(!Identity::get_ratchet(srv_in.hash()).empty(),
              "uplink: device remembered the server's ratchet");

        // Device encrypts its report to the server against the server's ratchet.
        Identity srv_pub(false);
        srv_pub.load_public_key(server.get_public_key());
        Destination srv_out(srv_pub, Type::Destination::OUT, Type::Destination::SINGLE,
                            "ratchet", "server");
        check(srv_out.hash() == srv_in.hash(), "uplink: OUT addr matches the IN destination");

        const std::string report("SensorReport env=59B");
        Packet data_pkt(srv_out, Bytes(report.data(), report.size()), Type::Packet::DATA);
        data_pkt.pack();
        Transport::inbound(data_pkt.raw(), Interface(iface));
        check(delivered.size() == report.size() &&
                  std::string((const char*)delivered.data(), delivered.size()) == report,
              "uplink: server decrypted the ratcheted report");
    }

    /* ---- 3. LEGACY: a peer with no ratchet still works over the base key ---- */
    {
        Identity legacy;
        Destination leg_in(legacy, Type::Destination::IN, Type::Destination::SINGLE,
                           "ratchet", "legacy");
        Transport::register_destination(leg_in);

        Bytes delivered;
        leg_in.set_packet_callback([&](const Bytes& plaintext, const Packet&) {
            delivered = plaintext;
        });

        Bytes ann = leg_in.announce(Bytes(), /*send=*/false);
        {
            Packet pkt(ann);
            pkt.unpack();
            check(pkt.context_flag() == Type::Packet::FLAG_UNSET,
                  "legacy: no-ratchet announce carries FLAG_UNSET");
        }
        Transport::inbound(ann, Interface(iface));
        check(Identity::get_ratchet(leg_in.hash()).empty(),
              "legacy: no ratchet remembered for a FLAG_UNSET peer");

        Identity peer(false);
        peer.load_public_key(legacy.get_public_key());
        Destination leg_out(peer, Type::Destination::OUT, Type::Destination::SINGLE,
                            "ratchet", "legacy");
        const std::string payload("legacy-base-key");
        Packet data_pkt(leg_out, Bytes(payload.data(), payload.size()), Type::Packet::DATA);
        data_pkt.pack();
        check(data_pkt.ratchet_id().empty(), "legacy: no ratchet id for a base-key packet");
        Transport::inbound(data_pkt.raw(), Interface(iface));
        check(delivered.size() == payload.size() &&
                  std::string((const char*)delivered.data(), delivered.size()) == payload,
              "legacy: base-key packet still decrypts");
    }

    /* ---- 4. CROSS-IMPLEMENTATION: a reference-RNS (Python) ratcheted token
     * must decrypt in the native port with the same key material.  Vectors
     * produced by the deployed reference RNS (Server version, Identity +
     * _generate_ratchet + encrypt(plaintext, ratchet=...)); the reference also
     * reported decrypting its own output (REFSELF OK), so a pass here pins the
     * exact wire shape the server's ratcheted downlink uses. ---- */
    {
        auto unhex = [](const char* h) {
            Bytes out;
            std::string s(h);
            for (size_t i = 0; i < s.size(); i += 2) {
                uint8_t b = (uint8_t)std::strtoul(s.substr(i, 2).c_str(), nullptr, 16);
                out.append(Bytes(&b, 1));
            }
            return out;
        };

        // Deterministic reference-RNS (Python) vectors -- identity private,
        // fixed ephemeral key, self-verified in the reference (BASE_OK/RAT_OK).
        // The native must decrypt both exactly (the server's base-key AND
        // ratcheted downlink shapes).
        Identity dev(false);
        check(dev.load_private_key(unhex(
            "70230e2077a08bcd0cdec71b8a100bc0d89eed483f6b0d80d5ab10702ff3e645"
        "af8ea3c0b44feb36bc8c56e005b70f28bd8264be4725f7d0fe683909738ca76b")), "cross: loaded the reference identity's private key");
        check(dev.get_salt().toHex() == "3fafbcaa74e96e7f0538bd9e4a58e85a",
              "cross: identity salt matches the reference");
        Bytes ratchet_prv = unhex(
            "c000fb1488e1af539c47a01eef1fb994d734f0c7229348af5d63c0b3c69ea97e");
        check(Identity::ratchet_public_from_private(ratchet_prv).toHex() == "e0e86460e65dd48dccd036f227280bd8659deb4848aaa8f99ee64fa251fd7d19",
              "cross: ratchet public derivation matches the reference");

        {
            std::vector<Bytes> ratchets{ratchet_prv};
            Bytes plain = dev.decrypt(unhex(
                "8f40c5adb68f25624ae5b214ea767a6ec94d829d3d7b5e1ad1ba6f3e2138285f"
        "cdb178a1b79ea7c48e48b6107416f90a423500a391cd5f9b4db8cadd493a0d53"
        "936204bbeecbd4cc2eb724ab4b2d3305da87db08b5317f033927e68a9588968a"
        "575278e1b97115532c4c25fb77aaad24"), &ratchets);
            check(plain.size() == 21 && std::string((const char*)plain.data(), plain.size()) ==
                                           "HELLO-RATCHET-PAYLOAD",
                  "cross: native decrypts the reference ratcheted token");
        }

        {
            Bytes base_tok = unhex(
                "8f40c5adb68f25624ae5b214ea767a6ec94d829d3d7b5e1ad1ba6f3e2138285f"
                "3d05b9c8ab6b412962bf6ecb4eac3752430605b8c609227b155167e9d039a436"
                "c43cf0322e816e448a1ac98ee9e7276a368f2ff99e5682be03148897732b8a87"
                "3237354d669b8a1e4fcddc7009a52518");
        Bytes plain = dev.decrypt(base_tok);
        std::printf("CROSS base plain len=%zu content=%s\n", plain.size(),
                    plain.size() ? std::string((const char*)plain.data(), plain.size()).c_str() : "<empty>");
            check(plain.size() == 18 && std::string((const char*)plain.data(), plain.size()) ==
                                           "HELLO-BASE-PAYLOAD",
                  "cross: native decrypts the reference base-key token");
        }
    }

    std::printf(failures == 0 ? "RESULT=PASS\n" : "RESULT=FAIL (%d)\n", failures);
    return failures == 0 ? 0 : 1;
}
