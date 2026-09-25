#include "lora_interface.h"

#include <RadioLib.h>
#include <string.h>

#include "driver/spi_master.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"

#include "cardputer_pins.h"
#include "radiolib_esp_idf_hal.h"
#include "rtreticulum/type.h"

using namespace RNS;

static const char* TAG = "lora";

namespace {
    /* Set by the SX1262 DIO1 ISR. The interface loop() drains it on the main
     * task. */
    volatile bool g_packet_pending = false;
    void IRAM_ATTR on_dio1() {
        g_packet_pending = true;
    }

    /* TX queue: send_outgoing (any task) enqueues, poll() (main task) drains.
     * This avoids multi-task SPI access which causes assert failures. */
    struct TxItem {
        uint8_t data[Type::Reticulum::MTU + 32];
        size_t  len;
    };
    QueueHandle_t g_tx_queue = nullptr;
}

namespace Cardputer {

LoraInterface::LoraInterface() : RNS::InterfaceImpl("cardputer_lora") {}

LoraInterface::~LoraInterface() {
    /* _radio / _hal are not deleted: their classes lack a virtual destructor
     * (-Werror=delete-non-virtual-dtor) and the interface is never torn down
     * in the sensor-node app — the device runs until power-off.  stop() is
     * the teardown path. */
}

std::shared_ptr<LoraInterface> LoraInterface::create() {
    return std::shared_ptr<LoraInterface>(new LoraInterface());
}

bool LoraInterface::start() {
    if (!g_tx_queue) g_tx_queue = xQueueCreate(8, sizeof(TxItem));
    _hal = new EspIdfHal(CARDPUTER_LORA_SCK, CARDPUTER_LORA_MISO, CARDPUTER_LORA_MOSI,
                         CARDPUTER_LORA_NSS, /*spi_host=*/CARDPUTER_LORA_SPI_HOST);
    _radio = new SX1262(new Module(_hal,
                                   CARDPUTER_LORA_NSS,
                                   CARDPUTER_LORA_DIO1,
                                   CARDPUTER_LORA_RST,
                                   CARDPUTER_LORA_BUSY));

    /* Cap LoRa-1262 has a 32 MHz TCXO fed from SX1262 DIO3 at 1.8V and uses
     * DIO2 as the RF TX/RX switch (RadioLib enables DIO2 automatically in
     * begin()). */
    int state = _radio->begin(CARDPUTER_LORA_FREQ_MHZ,
                              CARDPUTER_LORA_BANDWIDTH_KHZ,
                              CARDPUTER_LORA_SPREADING_FACTOR,
                              CARDPUTER_LORA_CODING_RATE,
                              CARDPUTER_LORA_SYNC_WORD,
                              CARDPUTER_LORA_TX_POWER_DBM,
                              CARDPUTER_LORA_PREAMBLE_LENGTH,
                              CARDPUTER_LORA_TCXO_VOLTAGE,
                              true);
    if (state != RADIOLIB_ERR_NONE) {
        ESP_LOGE(TAG, "SX1262 begin failed: %d", state);
        return false;
    }

    /* Upstream RNode firmware enables CRC on all LoRa packets. Without this a
     * real RNode will silently drop our frames (CRC absent → check fails). */
    _radio->setCRC(1);  /* SX126x: 0=off, 1=on. Matches upstream RNode. */

    _radio->setDio1Action(on_dio1);
    state = _radio->startReceive();
    if (state != RADIOLIB_ERR_NONE) {
        ESP_LOGE(TAG, "SX1262 startReceive failed: %d", state);
        return false;
    }
    _online = true;
    _radio_on = true;
    ESP_LOGI(TAG, "SX1262 listening on %.1f MHz SF%d BW%.0f",
             CARDPUTER_LORA_FREQ_MHZ,
             CARDPUTER_LORA_SPREADING_FACTOR,
             CARDPUTER_LORA_BANDWIDTH_KHZ);
    return true;
}

void LoraInterface::stop() {
    if (_radio) _radio->standby();
    _online = false;
    _radio_on = false;
}

void LoraInterface::poll() {
    /* RX before TX.  The SX126x shares one 256-byte FIFO between receive and
     * transmit, so a packet that finished receiving while a transmission was
     * already queued would be destroyed by that transmission — and the DIO1
     * flag, cleared when the frame goes out, is the only record it ever
     * arrived.  Draining RX first recovers those packets; it costs one DIO1
     * check per pass when nothing is pending. */
    if (g_packet_pending) poll_rx();

    /* Then drain the TX queue (filled from any task via send_outgoing). */
    TxItem item;
    while (g_tx_queue && xQueueReceive(g_tx_queue, &item, 0) == pdTRUE) {
        _radio->standby();
        int state = _radio->transmit(item.data, item.len);
        if (state == RADIOLIB_ERR_NONE) {
            ESP_LOGI(TAG, "TX %u bytes on LoRa", (unsigned)item.len);
        } else {
            ESP_LOGW(TAG, "transmit failed: %d", state);
        }
        /* The transmit clobbered the RX FIFO, so any surviving flag is stale. */
        g_packet_pending = false;
        _radio->startReceive();
    }
}

/* Drain one received frame (or a merged split pair) and hand it to Reticulum. */
void LoraInterface::poll_rx() {
    g_packet_pending = false;

    constexpr size_t MAX_FRAME = MAX_PACKET + 1;   /* RNode header + payload */
    size_t len = _radio->getPacketLength();
    if (len == 0) {
        /* A DIO1 with no payload — a CRC failure or an empty-payload IRQ.
         * Logged, not swallowed: a frame dropped here is indistinguishable
         * from one that never arrived, which is precisely the ambiguity that
         * made "the device hears everything except packets for itself" so hard
         * to read. */
        ESP_LOGW(TAG, "RX frame with no payload (crc/irq) — dropped");
        _radio->startReceive();
        return;
    }
    if (len > MAX_FRAME) {
        ESP_LOGW(TAG, "RX frame too large (%u B) — dropped", (unsigned)len);
        _radio->startReceive();
        return;
    }
    uint8_t buf[MAX_FRAME];
    int state = _radio->readData(buf, len);
    if (state != RADIOLIB_ERR_NONE || len < 2) {
        if (state != RADIOLIB_ERR_NONE) ESP_LOGW(TAG, "readData failed: %d", state);
        _radio->startReceive();
        return;
    }
    /* Header of every accepted frame: size, split flag and tag.  Together with
     * Reticulum's own `inbound ok ... dh=` line this pins down, per frame,
     * whether what the radio heard became a packet for this node. */
    ESP_LOGI(TAG, "RX frame %u B header=0x%02x split=%u tag=%02x",
             (unsigned)len, (unsigned)buf[0], (unsigned)(buf[0] & 0x01),
             (unsigned)(buf[0] & 0xF0));

    const uint32_t now_ms = (uint32_t)(esp_timer_get_time() / 1000);

    /* Reassemble by sequence tag (see lma_rnode_framing.h): a frame is only
     * discarded when its partner genuinely never arrives, never because
     * something else was transmitted in between — which is what silently ate
     * the server's chart packets on a shared channel. */
    const lma_attachment::RnodeFrame frame = lma_attachment::parse_rnode_frame(buf, len);
    const auto assembled = _asm.push(frame, now_ms);

    if (assembled.stale) {
        ESP_LOGW(TAG, "dropping stale split fragment (%u B, seq=%02x)",
                 (unsigned)assembled.stale_len, (unsigned)frame.seq);
    }
    if (assembled.dropped) {
        ESP_LOGW(TAG, "split assembly refused (%u B limit)", (unsigned)MAX_PACKET);
    }
    if (assembled.complete) {
        if (frame.split) {
            ESP_LOGI(TAG, "RX %u bytes on LoRa (2 frames, seq=%02x)",
                     (unsigned)assembled.data.size(), (unsigned)frame.seq);
        } else {
            ESP_LOGI(TAG, "RX %u bytes on LoRa", (unsigned)assembled.data.size());
        }
        deliver_packet((const uint8_t*)assembled.data.data(), assembled.data.size());
    }

    _radio->startReceive();
}

/* Hand a fully reassembled RNS packet to Reticulum (the RNode header is
 * stripped; Reticulum expects the bare frame). */
void LoraInterface::deliver_packet(const uint8_t* data, size_t len) {
    if (_raw_rx) {
        _raw_rx(data, len, _radio->getRSSI(), _radio->getSNR());
    } else {
        this->handle_incoming(Bytes(data, len));
    }
}

void LoraInterface::send_outgoing(const RNS::Bytes& data) {
    if (!_radio || !g_tx_queue) return;
    /* On-air frames carry a 1-byte RNode/urns LoRa interface header BEFORE
     * the RNS frame (random seq in the upper nibble; bit0 = FLAG_SPLIT) —
     * the server's RNode strips it on RX (exactly like urns dtu.py /
     * Sprout's uart_at_interface).  The SX1262 FIFO caps a single LoRa frame
     * at 255 bytes total, so payloads >254 B (a SensorReport octal chains to
     * ~275 B) are split into 2 frames using the RNode split protocol: both
     * frames carry the split-flagged header (seq|0x01) and the receiver
     * reassembles them by matching seq.  Mirrors Sprout's proven path. */
    constexpr size_t FRAME_PAYLOAD = 254;
    uint8_t seq = (uint8_t)(esp_random() & 0xF0);
    size_t off = 0;
    do {
        size_t chunk = std::min(FRAME_PAYLOAD, data.size() - off);
        bool split = data.size() > FRAME_PAYLOAD;
        TxItem item;
        item.data[0] = split ? (uint8_t)(seq | 0x01) : seq;   /* RNode header */
        memcpy(item.data + 1, data.data() + off, chunk);
        item.len = chunk + 1;  /* include header byte */
        _txb += chunk;
        if (xQueueSend(g_tx_queue, &item, pdMS_TO_TICKS(100)) != pdTRUE) {
            ESP_LOGW(TAG, "TX queue full, dropping packet");
            return;
        }
        off += chunk;
    } while (off < data.size());
}

void LoraInterface::send_raw(const uint8_t* data, size_t len) {
    if (!_radio || !_radio_on) return;
    uint8_t buf[Type::Reticulum::MTU + 32];
    buf[0] = (uint8_t)(esp_random() & 0xF0);
    if (len > sizeof(buf) - 1) len = sizeof(buf) - 1;
    memcpy(buf + 1, data, len);
    _txb += len;
    _radio->standby();
    _radio->transmit(buf, len + 1);
    g_packet_pending = false;
    _radio->startReceive();
}

}
