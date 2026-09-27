#include "lora_interface.h"

#include <RadioLib.h>
#include <string.h>

#include "driver/spi_master.h"
#include "esp_log.h"
#include "esp_random.h"
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
    /* Drain TX queue first (from any task via send_outgoing). */
    TxItem item;
    while (g_tx_queue && xQueueReceive(g_tx_queue, &item, 0) == pdTRUE) {
        _radio->standby();
        int state = _radio->transmit(item.data, item.len);
        if (state == RADIOLIB_ERR_NONE) {
            ESP_LOGI(TAG, "TX %u bytes on LoRa", (unsigned)item.len);
        } else {
            ESP_LOGW(TAG, "transmit failed: %d", state);
        }
        g_packet_pending = false;
        _radio->startReceive();
    }

    /* Then check for RX. */
    if (!g_packet_pending) return;
    g_packet_pending = false;

    size_t len = _radio->getPacketLength();
    constexpr size_t MAX_FRAME = Type::Reticulum::MTU + 32;
    if (len == 0 || len > MAX_FRAME) {
        _radio->startReceive();
        return;
    }
    uint8_t buf[MAX_FRAME];
    int state = _radio->readData(buf, len);
    if (state == RADIOLIB_ERR_NONE && len > 1) {
        ESP_LOGI(TAG, "RX %u bytes on LoRa", (unsigned)len);
        if (_raw_rx) {
            float rssi = _radio->getRSSI();
            float snr  = _radio->getSNR();
            _raw_rx(buf + 1, len - 1, rssi, snr);
        } else {
            this->handle_incoming(Bytes(buf + 1, len - 1));
        }
    } else {
        ESP_LOGW(TAG, "readData failed: %d", state);
    }
    _radio->startReceive();
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
