#pragma once

#include "lma_rnode_framing.h"

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
        /* RNode/urns on-air framing: every LoRa frame carries a 1-byte header
         * (sequence nibble + FLAG_SPLIT).  Packets up to 254 B ride one frame;
         * larger ones are split across two frames sharing the sequence nibble
         * and MUST be merged before they reach Reticulum — a fragment handed to
         * RNS on its own fails to unpack, which is what silently killed every
         * larger server reply (chart payloads, ACKs with data) on this board.
         * Merging happens in poll() on the main task, so the state needs no
         * lock (same reason the SPI access is single-task). */
        static const size_t   FRAME_PAYLOAD    = 254;                 /* per LoRa frame */
        static const size_t   MAX_PACKET       = 2 * FRAME_PAYLOAD;   /* split ceiling */
        /* Partner frame timeout.  Frames of a pair are sent back-to-back, so
         * this only has to cover radio scheduling jitter — not seconds.  Short
         * matters: a stale fragment holding a tag delays the next use of it. */
        static const uint32_t REASM_TIMEOUT_MS = 500;

        void deliver_packet(const uint8_t* data, size_t len);
        /* Drain one received frame (or a merged split pair) into Reticulum. */
        void poll_rx();

        /* Split reassembly keyed by the frame sequence tag: fragments from
         * different senders (or a sender that flags unconditionally) no longer
         * destroy each other.  Pure logic, host-tested as
         * //firmware_common:lma_rnode_framing_test. */
        lma_attachment::RnodeSplitAssembler _asm{MAX_PACKET, REASM_TIMEOUT_MS};

        EspIdfHal*  _hal     = nullptr;
        SX1262*     _radio   = nullptr;
        bool        _radio_on = false;
        RawRxCallback _raw_rx;
    };

}
