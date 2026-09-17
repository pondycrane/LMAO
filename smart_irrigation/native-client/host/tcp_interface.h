#pragma once
#include <memory>
#include <string>
#include <cstdint>
#include "rtreticulum/interface.h"

namespace HostTCP {
    // Reticulum TCPClientInterface (host, POSIX) — wire compatible with
    // Python RNS TCP interfaces (HDLC framing, no handshake).
    class TcpInterface : public RNS::InterfaceImpl {
    public:
        TcpInterface(const char* host, uint16_t port);
        ~TcpInterface() override;
        static std::shared_ptr<TcpInterface> create(const char* host, uint16_t port);
        bool start() override;
        void stop()  override;
        void loop()  override;
        void send_outgoing(const RNS::Bytes& data) override;
        std::string toString() const override { return "TcpInterface[" + _host + "]"; }
    private:
        std::string _host; uint16_t _port;
        int _sock = -1; bool _connected = false; uint64_t _reconnect_at = 0;
        bool _in_frame = false; bool _escape = false;
        uint8_t _rx_buf[600]; size_t _rx_len = 0;
        bool try_connect(); void disconnect();
    };
}
