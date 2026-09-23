#pragma once

#include <functional>
#include <memory>

#include "rtreticulum/interface.h"

class EspIdfHal;
class SX1262;

namespace Cardputer {

    /* RTReticulum LoRa interface backed by RadioLib's SX1262 driver for the
     * Cardputer ADV's onboard Cap LoRa-1262 module.  RX is triggered by the
     * DIO1 interrupt; the ISR sets a flag that poll() (main task only) drains.
     * On-air framing matches RNode: a 1-byte RNode header on every frame. */
    class LoraInterface : public RNS::InterfaceImpl {
    public:
        /* Incoming frames bypass handle_incoming() to this callback (raw
         * RSSI/SNR path, used for diagnostics). Kept for parity with the
         * Heltec reference; the sensor node leaves it unset. */
        using RawRxCallback = std::function<void(const uint8_t* data, size_t len,
                                                 float rssi_dbm, float snr_db)>;

        LoraInterface();
        ~LoraInterface() override;

        static std::shared_ptr<LoraInterface> create();

        bool start() override;
        void stop()  override;
        /* loop() is intentionally a no-op — the Reticulum task calls it, but
         * all SPI access must happen on the main task. Call poll() from the
         * main loop instead (single-threaded SPI). */
        void loop()  override {}
        void poll();
        void send_outgoing(const RNS::Bytes& data) override;
        std::string toString() const override { return "LoraInterface[cardputer]"; }

        void set_raw_rx_callback(RawRxCallback cb) { _raw_rx = std::move(cb); }
        void send_raw(const uint8_t* data, size_t len);

        bool radio_online() const { return _radio_on; }

    private:
        EspIdfHal*  _hal     = nullptr;
        SX1262*     _radio   = nullptr;
        bool        _radio_on = false;
        RawRxCallback _raw_rx;

        /* RNode/urns split-frame reassembly state (see poll()). */
        uint8_t  _reasm_seq  = 0;
        bool     _reasm_armed = false;
        uint32_t _reasm_seen_ms = 0;
        RNS::Bytes _reasm_buf;   // size capped by REASM_TIMEOUT below
    };

}
