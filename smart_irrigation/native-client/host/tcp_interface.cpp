// Host (POSIX) port of the Heltec V3 TcpInterface — same HDLC framing as
// Python RNS TCPClient/ServerInterface. esp_timer/esp_log swapped for
// std::chrono + fprintf.
#include "tcp_interface.h"
#include <sys/socket.h>
#include <netdb.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <cstdio>
#include <cstring>
#include <chrono>

using namespace RNS;
namespace {
    constexpr uint8_t HDLC_FLAG = 0x7E;
    constexpr uint8_t HDLC_ESC  = 0x7D;
    constexpr uint8_t HDLC_ESC_MASK = 0x20;
    uint64_t now_ms() {
        using namespace std::chrono;
        return duration_cast<milliseconds>(steady_clock::now().time_since_epoch()).count();
    }
    void logf(const char* f, ...) { va_list v; va_start(v,f); vfprintf(stderr, f, v); va_end(v); }
}
#include <cstdarg>
namespace HostTCP {

TcpInterface::TcpInterface(const char* host, uint16_t port) : InterfaceImpl("tcp"), _host(host), _port(port) {}
TcpInterface::~TcpInterface() { disconnect(); }
std::shared_ptr<TcpInterface> TcpInterface::create(const char* host, uint16_t port) {
    return std::shared_ptr<TcpInterface>(new TcpInterface(host, port));
}

bool TcpInterface::try_connect() {
    struct addrinfo hints = {}, *res = nullptr;
    hints.ai_family = AF_INET; hints.ai_socktype = SOCK_STREAM;
    char port_str[8]; snprintf(port_str, sizeof(port_str), "%u", _port);
    int rc = getaddrinfo(_host.c_str(), port_str, &hints, &res);
    if (rc != 0 || !res) { logf("[tcp] resolve failed %d\n", rc); return false; }
    _sock = socket(res->ai_family, res->ai_socktype, res->ai_protocol);
    if (_sock < 0) { freeaddrinfo(res); return false; }
    fcntl(_sock, F_SETFL, fcntl(_sock, F_GETFL) | O_NONBLOCK);
    rc = connect(_sock, res->ai_addr, res->ai_addrlen);
    freeaddrinfo(res);
    if (rc < 0 && errno != EINPROGRESS) { close(_sock); _sock = -1; return false; }
    fd_set wset; FD_ZERO(&wset); FD_SET(_sock, &wset);
    struct timeval tv = {5, 0};
    rc = select(_sock + 1, nullptr, &wset, nullptr, &tv);
    if (rc <= 0) { close(_sock); _sock = -1; return false; }
    int err = 0; socklen_t len = sizeof(err);
    getsockopt(_sock, SOL_SOCKET, SO_ERROR, &err, &len);
    if (err != 0) { close(_sock); _sock = -1; return false; }
    fcntl(_sock, F_SETFL, fcntl(_sock, F_GETFL) & ~O_NONBLOCK);
    tv = {0, 10000};
    setsockopt(_sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    _connected = true;
    logf("[tcp] connected to %s:%u\n", _host.c_str(), _port);
    return true;
}

void TcpInterface::disconnect() {
    if (_sock >= 0) { close(_sock); _sock = -1; }
    _connected = false;
}

bool TcpInterface::start() {
    if (try_connect()) { _online = true; return true; }
    logf("[tcp] initial connect failed, will retry\n");
    _online = true; return true;
}
void TcpInterface::stop() { disconnect(); _online = false; }

void TcpInterface::loop() {
    uint64_t now = now_ms();
    if (!_connected) {
        if (now < _reconnect_at) return;
        if (try_connect()) return;
        _reconnect_at = now + 5000;
        return;
    }
    uint8_t byte; int n;
    while ((n = recv(_sock, &byte, 1, 0)) == 1) {
        if (byte == HDLC_FLAG) {
            if (_in_frame && _rx_len > 0) {
                logf("[tcp] RX %u bytes\n", (unsigned)_rx_len);
                this->handle_incoming(Bytes(_rx_buf, _rx_len));
            }
            _in_frame = true; _escape = false; _rx_len = 0;
        } else if (_in_frame) {
            if (byte == HDLC_ESC) { _escape = true; }
            else {
                if (_escape) { byte ^= HDLC_ESC_MASK; _escape = false; }
                if (_rx_len < sizeof(_rx_buf)) _rx_buf[_rx_len++] = byte;
            }
        }
    }
    if (n == 0) { logf("[tcp] peer closed\n"); disconnect(); }
    else if (n < 0 && errno != EAGAIN && errno != EWOULDBLOCK) { logf("[tcp] recv err %d\n", errno); disconnect(); }
}

void TcpInterface::send_outgoing(const Bytes& data) {
    if (!_connected || _sock < 0) return;
    logf("[tcp] TX %u bytes\n", (unsigned)data.size());
    _txb += data.size();
    uint8_t buf[1400]; size_t o = 0;
    buf[o++] = HDLC_FLAG;
    for (size_t i = 0; i < data.size() && o + 2 < sizeof(buf); ++i) {
        uint8_t b = data.data()[i];
        if (b == HDLC_FLAG || b == HDLC_ESC) { buf[o++] = HDLC_ESC; buf[o++] = b ^ HDLC_ESC_MASK; }
        else buf[o++] = b;
    }
    buf[o++] = HDLC_FLAG;
    if (send(_sock, buf, o, 0) != (int)o) { logf("[tcp] send failed\n"); disconnect(); }
}
}
