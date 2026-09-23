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
        /* Re-arm RX IMMEDIATELY after reading, before any software parsing:
         * the server's split reply arrives as two LoRa frames ~10-50 ms
         * apart, and every millisecond we spend parsing frame 1 (decrypt,
         * msgpack, ...) is time the radio is NOT listening for frame 2. */
        _radio->startReceive();

        if (_raw_rx) {
            float rssi = _radio->getRSSI();
            float snr  = _radio->getSNR();
            _raw_rx(buf + 1, len - 1, rssi, snr);
        } else {
            /* Diag: signal quality per frame — the bench↔tp4 LoRa link is the
             * thing that drops one half of a split reply. Log RSSI/SNR so a
             * marginal link is visible (RSSI ≈ -100 dBm is unusable). */
            const float rx_rssi = _radio->getRSSI();
            const float rx_snr  = _radio->getSNR();
            ESP_LOGI(TAG, "RX %u bytes on LoRa (RSSI=%.1f SNR=%.1f)",
                     (unsigned)len, (double)rx_rssi, (double)rx_snr);
            ESP_LOGV(TAG, "  hdr=0x%02x blen=%u", buf[0], (unsigned)(len - 1));
            if (len >= 9 && (len - 1) >= 12) {
                ESP_LOGI(TAG, "  hdr=0x%02x seq=%02x blen=%u body[0:12]=%02x%02x%02x%02x%02x%02x%02x%02x%02x%02x%02x%02x",
                         buf[0], buf[0] & 0xF0, (unsigned)(len - 1),
                         buf[1], buf[2], buf[3], buf[4], buf[5], buf[6], buf[7], buf[8],
                         buf[9], buf[10], buf[11], buf[12]);
            }
            /* RNode/urns split-frame reassembly.  Frames >254 B (the SX1262
             * FIFO cap) travel as two LoRa frames.  Two on-wire conventions
             * are in the wild:
             *  1) Vendored/urns (send_outgoing, dtu.py, Sprout DTU): both
             *     halves carry bit0=FLAG_SPLIT and the SAME seq nibble.
             *  2) The production Heltec RNode (tp4, web-flashed): the FIRST
             *     half carries FLAG_SPLIT + random seq + the RNS header; the
             *     SECOND half arrives ~400 ms later with a NON-split header
             *     (0x10/0x70 observed) and no seq relationship.
             * Consequence of (2) for a 467 B server reply: on-air frames are
             *   [254 B split][213 B non-split]; both halves are received and
             *   merged here.  (The Telegram-era claim that the second half is
             *   245 B / totals 499 B was from unrelated traffic whose payload
             *   contains the delivery hash — see the session log.)
             * Caveat found during PR2 bring-up: the tp4 RNode's TX of the
             *   split emits a 213 B non-split second half whose first byte is an
             *   inline header and whose true payload is only the following
             *   212 B (the RNS frame's final byte is lost), so a 467 B reply
             *   reassembles to a 1-byte-short ciphertext that fails RNS
             *   decryption (token HMAC).  Single-frame replies (<255 B)
             *   decode correctly.  Keeping the merge as-is: RNS validates
             *   the result and undecryptable merges are dropped rather than
             *   surfaced to lxmf delivery. */
            const uint8_t header = buf[0];
            const uint8_t* body  = buf + 1;
            const size_t   blen  = len - 1;
            constexpr uint8_t FLAG_SPLIT = 0x01;
            constexpr uint8_t SEQ_MASK   = 0xF0;
            /* RNode sends both reply frames ~410 ms apart (one frame
             * airtime), but a stale buffer must not survive into unrelated
             * traffic — mirrors the stale-drop in urns dtu.py without
             * killing the pending buffer on small interleaved frames. */
            constexpr uint32_t REASM_TIMEOUT_MS = 4000;
            /* Convention (2): second-half payloads are large (the RNS reply
             * remainder, e.g. 245 B).  Use real split frames / small mesh
             * traffic (51/84/167 B announces) are never completed this way. */
            constexpr size_t   FLAGLESS_MIN = 200;

            const uint32_t now_ms = (uint32_t)(esp_timer_get_time() / 1000);
            if (_reasm_armed && now_ms - _reasm_seen_ms > REASM_TIMEOUT_MS) {
                ESP_LOGW(TAG, "RX split reassembly timed out, discarding partial");
                _reasm_armed = false;
            }

            Bytes pkt;
            if (header & FLAG_SPLIT) {
                const uint8_t seq = header & SEQ_MASK;
                if (!_reasm_armed || _reasm_seq != seq) {
                    /* First split frame of a (new) packet — open the buffer.
                     * A previous pending buffer with a different seq is
                     * superseded, like dtu.py's seq-mismatch restart. */
                    if (_reasm_armed) {
                        ESP_LOGW(TAG, "RX split seq mismatch (%02x != %02x), restarting",
                                 _reasm_seq, seq);
                    }
                    _reasm_seq   = seq;
                    _reasm_armed = true;
                    _reasm_seen_ms = now_ms;
                    _reasm_buf.assign(body, blen);
                    ESP_LOGI(TAG, "RX split frame 1: %uB seq=%02x", (unsigned)blen, seq);
                } else {
                    /* Second split frame with matching seq — packet complete
                     * (convention 1). */
                    _reasm_buf.append(body, blen);
                    pkt.assign(_reasm_buf);
                    _reasm_buf.clear();
                    _reasm_armed = false;
                    ESP_LOGI(TAG, "RX split frame 2: %uB -> %uB total",
                             (unsigned)blen, (unsigned)pkt.size());
                }
            } else if (_reasm_armed && _reasm_buf.size() == 254 && blen >= FLAGLESS_MIN) {
                /* Convention (2): the production RNode's second half has no
                 * split flag and no seq relationship — a full-size pending
                 * first half plus a large unflagged frame is the reply
                 * remainder.  The RNS layer validates the merge. */
                _reasm_buf.append(body, blen);
                pkt.assign(_reasm_buf);
                _reasm_buf.clear();
                _reasm_armed = false;
                ESP_LOGI(TAG, "RX split frame 2 (unflagged): %uB -> %uB total",
                         (unsigned)blen, (unsigned)pkt.size());
            } else {
                /* Non-split frame — pass through while leaving any pending
                 * split buffer intact (the two reply frames can be separated
                 * by unrelated mesh traffic on this shared channel). */
                pkt.assign(body, blen);
            }

            if (!pkt.empty()) this->handle_incoming(pkt);

            /* The reply's second frame may already be in the radio FIFO
             * (frames arrive back-to-back; we re-armed RX above but the
             * DIO1 might still be latched if frame 2 landed mid-parse).
             * Drain it now — no main-loop latency. */
            for (int fast = 0; fast < 6 && g_packet_pending; fast++) {
                g_packet_pending = false;
                size_t len2 = _radio->getPacketLength();
                if (len2 == 0 || len2 > MAX_FRAME) { _radio->startReceive(); continue; }
                uint8_t buf2[MAX_FRAME];
                if (_radio->readData(buf2, len2) != RADIOLIB_ERR_NONE || len2 <= 1) {
                    _radio->startReceive();
                    continue;
                }
                _radio->startReceive();
                const uint8_t h2 = buf2[0];
                const uint8_t* b2 = buf2 + 1;
                const size_t n2 = len2 - 1;
                ESP_LOGI(TAG, "RX %u bytes on LoRa (back-to-back)", (unsigned)len2);
                Bytes pkt2;
                if (h2 & FLAG_SPLIT && _reasm_armed && (h2 & SEQ_MASK) == _reasm_seq) {
                    /* Second half of a split we already opened (conv. 1). */
                    _reasm_buf.append(b2, n2);
                    pkt2.assign(_reasm_buf);
                    _reasm_buf.clear();
                    _reasm_armed = false;
                    ESP_LOGI(TAG, "RX split frame 2 (back-to-back): %uB -> %uB total",
                             (unsigned)n2, (unsigned)pkt2.size());
                } else if (!(h2 & FLAG_SPLIT) && _reasm_armed &&
                           _reasm_buf.size() == 254 && n2 >= FLAGLESS_MIN) {
                    /* Production RNode second half, unflagged (conv. 2). */
                    _reasm_buf.append(b2, n2);
                    pkt2.assign(_reasm_buf);
                    _reasm_buf.clear();
                    _reasm_armed = false;
                    ESP_LOGI(TAG, "RX split frame 2 (back-to-back, unflagged): %uB -> %uB total",
                             (unsigned)n2, (unsigned)pkt2.size());
                } else if (h2 & FLAG_SPLIT) {
                    /* A different split stream — open/restart its buffer. */
                    if (_reasm_armed) {
                        ESP_LOGW(TAG, "RX split seq mismatch (%02x != %02x), restarting",
                                 _reasm_seq, h2 & SEQ_MASK);
                    }
                    _reasm_seq   = h2 & SEQ_MASK;
                    _reasm_armed = true;
                    _reasm_seen_ms = (uint32_t)(esp_timer_get_time() / 1000);
                    _reasm_buf.assign(b2, n2);
                    ESP_LOGI(TAG, "RX split frame 1: %uB seq=%02x", (unsigned)n2, h2 & SEQ_MASK);
                } else {
                    /* Small non-split frame — deliver standalone but LEAVE any
                     * pending split buffer intact (the reply halves can be
                     * separated by unrelated mesh traffic). */
                    pkt2.assign(b2, n2);
                }
                if (!pkt2.empty()) this->handle_incoming(pkt2);
            }
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
