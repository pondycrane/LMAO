#include "uart_at_interface.h"

#include <cstdio>
#include <cstring>
#include "esp_log.h"
#include "esp_random.h"
#include "driver/uart.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char* TAG = "uart_at";

using namespace RNS;

UartAtInterface::~UartAtInterface() { stop(); }

bool UartAtInterface::start() {
    uart_config_t cfg = {};
    cfg.baud_rate = DTU_BAUD;
    cfg.data_bits = UART_DATA_8_BITS;
    cfg.parity = UART_PARITY_DISABLE;
    cfg.stop_bits = UART_STOP_BITS_1;
    cfg.flow_ctrl = UART_HW_FLOWCTRL_DISABLE;
    cfg.source_clk = UART_SCLK_DEFAULT;
    uart_param_config(UART_NUM_2, &cfg);
    uart_driver_install(UART_NUM_2, 1024, 1024, 0, nullptr, 0);
    uart_set_pin(UART_NUM_2, PIN_UART_TX, PIN_UART_RX, -1, -1);
    ESP_LOGI(TAG, "UART2 up (tx=%u rx=%u) — configuring RAK3172 P2P mesh params",
             (unsigned)PIN_UART_TX, (unsigned)PIN_UART_RX);

    for (int i = 0; DTU_CONFIG_CMDS[i]; ++i) {
        uart_write(DTU_CONFIG_CMDS[i]);
        uart_write("\r\n", 2);
        uart_wait_tx_done(UART_NUM_2, 500);
        drain_ms(100);
        vTaskDelay(pdMS_TO_TICKS(30));
    }
    // RX off then on (avoid AT_BUSY_ERROR between commands).
    uart_write("AT+PRECV=0\r\n", 12);
    uart_wait_tx_done(UART_NUM_2, 500);
    drain_ms(100);
    uart_write("AT+PRECV=65535\r\n", 16);  // continuous rx
    uart_wait_tx_done(UART_NUM_2, 500);
    drain_ms(250);
    _running = true;
    _online = true;
    ESP_LOGI(TAG, "RAK3172 P2P on-mesh, listening");
    return true;
}

void UartAtInterface::stop() {
    if (_running) {
        uart_write("AT+PRECV=0\r\n", 12);
        uart_wait_tx_done(UART_NUM_2, 300);
        uart_driver_delete(UART_NUM_2);
        _running = false;
    }
    _online = false;
}

void UartAtInterface::send_outgoing(const Bytes& data) {
    if (!_online) return;
    ESP_LOGI(TAG, "TX %u bytes via AT+PSEND", (unsigned)data.size());
    _txb += data.size();

    // RUI4 P2P requires RX to be OFF while transmitting.
    uart_write("AT+PRECV=0\r\n", 12);
    uart_wait_tx_done(UART_NUM_2, 300);
    drain_ms(60);

    // On-air frames carry a 1-byte RNode/urns LoRa interface header BEFORE the
    // RNS frame (random seq in the upper nibble, no split flag for one frame) —
    // the server's RNode strips it on RX, so we must prepend it on TX.
    uint8_t hdr = (uint8_t)(esp_random() & 0xF0);
    RNS::Bytes onair;
    onair.append(RNS::Bytes(&hdr, 1));
    onair.append(data);
    std::string cmd = "AT+PSEND=" + to_hex(onair) + "\r\n";
    uart_write(cmd.c_str(), cmd.size());
    uart_wait_tx_done(UART_NUM_2, 500);
    drain_ms(300);  // swallow the late +EVT:TXP2P DONE / OK

    uart_write("AT+PRECV=65535\r\n", 16);  // re-arm rx
    uart_wait_tx_done(UART_NUM_2, 300);
    drain_ms(80);
}

void UartAtInterface::loop() {
    if (!_online) return;
    uint8_t buf[512];
    int n = uart_read_bytes(UART_NUM_2, buf, sizeof(buf), 0);
    if (n <= 0) return;
    for (int i = 0; i < n; ++i) _line += (char)buf[i];

    // Process complete '\n'-terminated lines.
    size_t nl;
    while ((nl = _line.find('\n')) != std::string::npos) {
        std::string line = _line.substr(0, nl);
        _line.erase(0, nl + 1);
        if (!line.empty() && line.back() == '\r') line.pop_back();
        if (line.find("+EVT:RXP2P") != std::string::npos) {
            _expect_hex = true;
            ESP_LOGI(TAG, "RX event: %s", line.c_str());
            // RUI4 appends the hex payload AFTER the last ':' on this same
            // line (e.g. ";+EVT:RXP2P:-18:13:010000DA...."), not on a
            // separate line. Extract it here; the next-line branch below is a
            // fallback for the two-line firmware format.
            size_t colon = line.rfind(':');
            if (colon != std::string::npos && colon + 1 < line.size()) {
                Bytes p = from_hex(line.substr(colon + 1));
                if (!p.empty()) {
                    _expect_hex = false;
                    // On-air frames on this mesh carry a 1-byte RNode/urns
                    // LoRa interface header (seq+split) BEFORE the RNS frame;
                    // the RNS core expects the frame without it (urns does the
                    // same in LoRaInterface). Splitting (>254B) not needed yet.
                    if (p.size() > 1) {
                        Bytes rframe(p.data() + 1, p.size() - 1);
                        ESP_LOGI(TAG, "RX %u bytes payload (inline)",
                                 (unsigned)rframe.size());
                        handle_incoming(rframe);
                    }
                }
            }
        } else if (_expect_hex && !line.empty()) {
            _expect_hex = false;
            Bytes payload = from_hex(line);
            if (!payload.empty()) {
                ESP_LOGI(TAG, "RX %u bytes payload", (unsigned)payload.size());
                handle_incoming(payload);
            }
        }
    }
}

void UartAtInterface::uart_write(const char* s) { uart_write(s, strlen(s)); }
void UartAtInterface::uart_write(const char* s, size_t n) {
    if (n) uart_write_bytes(UART_NUM_2, s, n);
}

void UartAtInterface::drain_ms(uint32_t ms) {
    uint8_t b[256];
    TickType_t start = xTaskGetTickCount();
    while ((TickType_t)(xTaskGetTickCount() - start) < pdMS_TO_TICKS(ms)) {
        int n = uart_read_bytes(UART_NUM_2, b, sizeof(b), 20);
        (void)n;  // discard
    }
}

std::string UartAtInterface::to_hex(const Bytes& b) {
    static const char* H = "0123456789abcdef";
    std::string s; s.reserve(b.size() * 2);
    for (size_t i = 0; i < b.size(); ++i) {
        s.push_back(H[b.data()[i] >> 4]);
        s.push_back(H[b.data()[i] & 0x0f]);
    }
    return s;
}

Bytes UartAtInterface::from_hex(const std::string& h) {
    Bytes out;
    if (h.size() % 2) return out;
    std::string raw;
    raw.reserve(h.size() / 2);
    for (size_t i = 0; i < h.size(); i += 2) {
        auto val = [&](char c) -> uint8_t {
            if (c >= '0' && c <= '9') return (uint8_t)(c - '0');
            if (c >= 'a' && c <= 'f') return (uint8_t)(c - 'a' + 10);
            if (c >= 'A' && c <= 'F') return (uint8_t)(c - 'A' + 10);
            return 0;
        };
        raw.push_back((char)((val(h[i]) << 4) | val(h[i + 1])));
    }
    return Bytes((const uint8_t*)raw.data(), raw.size());
}
