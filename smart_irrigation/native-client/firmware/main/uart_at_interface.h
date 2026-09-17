#pragma once

#include <cstdint>
#include <string>

#include "rtreticulum/interface.h"

namespace RNS {

    // G22 = UART2 TX -> RAK3172 RX ; G19 = UART2 RX <- RAK3172 TX
    constexpr uint8_t PIN_UART_TX = 22;
    constexpr uint8_t PIN_UART_RX = 19;
    constexpr uint32_t DTU_BAUD = 115200;

    // LMAO mesh radio params as RUI4 P2P AT values (must match server RNode).
    // 868 MHz / BW 125k / SF 7 / CR 4:5 / preamble 24 / syncword 0x1424 / TX 17
    static const char* const DTU_CONFIG_CMDS[] = {
        "AT+PFREQ=868000000", "AT+PSF=7", "AT+PBW=0", "AT+PCR=0",
        "AT+PPL=24", "AT+SYNCWORD=1424", "AT+PTP=17", nullptr,
    };

    // RNS InterfaceImpl that drives the M5Stack A152 DTU base (RAK3172) in
    // LoRa P2P mode over UART AT. Wire-format compatible with the urns
    // DtuInterface / Python Reticulum RNode framing at the RNS-core boundary
    // (the RNS core produces/consumes the same packet bytes as on the TCP/radio
    // interfaces — this class only handles AT+PSEND/+EVT:RXP2P hex transport).
    class UartAtInterface : public InterfaceImpl {
    public:
        UartAtInterface(const char* name = "dtu_at") : InterfaceImpl(name) {}
        ~UartAtInterface() override;

        bool start() override;
        void stop()  override;
        void loop()  override;                       // poll UART for +EVT:RXP2P
        void send_outgoing(const Bytes& data) override;
        std::string toString() const override { return std::string("UartAtInterface[DTU]"); }

    private:
        std::string _line;        // UART rx line buffer (AS-IS bytes)
        bool        _expect_hex = false;  // after +EVT:RXP2P header, next line is hex payload
        bool        _running = false;

        void uart_write(const char* s);
        void uart_write(const char* s, size_t n);
        void drain_ms(uint32_t ms);
        std::string read_response_ms(uint32_t ms);
        static std::string to_hex(const Bytes& b);
        static Bytes from_hex(const std::string& h);
    };

}
