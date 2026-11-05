#pragma once
#include <cstdint>
#include <memory>
#include <string>

#include "rtreticulum/interface.h"

// Reticulum TCPClientInterface — wire compatible with Python RNS
// TCPClient/ServerInterface: SLIP-style framing (NOT KISS): flag 0x7E, escape
// 0x7D with XOR 0x20 (byte ^ 0x20).  No handshake.
//
// One implementation for both worlds: compiled on POSIX hosts (system
// sockets) and under ESP-IDF (lwIP's BSD-sockets API, esp_timer/esp_log), so
// the host interop harness and the board firmware cannot drift apart.
namespace lma_tcp {
    class TcpInterface : public RNS::InterfaceImpl {
    public:
        TcpInterface(const char* host, uint16_t port);
        ~TcpInterface() override;
        static std::shared_ptr<TcpInterface> create(const char* host, uint16_t port);

        bool start() override;
        void stop() override;
        void loop() override;
        void send_outgoing(const RNS::Bytes& data) override;
        std::string toString() const override { return "TcpInterface[" + _host + "]"; }

        // True once the socket is connected.  The interface connects lazily and
        // retries, so a caller must check this before handing it packets.
        bool connected() const { return _connected; }

    private:
        std::string _host;
        uint16_t _port;
        int _sock = -1;
        bool _connected = false;
        uint64_t _reconnect_at = 0;
        bool _in_frame = false;
        bool _escape = false;
        uint8_t _rx_buf[600];
        size_t _rx_len = 0;

        bool try_connect();
        void disconnect();
    };
}
