// Sprout native client (ESP32-PICO-D4, ESP-IDF) — first radio milestone.
// Boots RNS with a UART-AT (RAK3172 DTU P2P) interface on the LMAO mesh,
// registers the "lmao/sprout" destination, and announces it every 30s so the
// production RNode/server can hear us. Issue #130.
//
// This is the native replacement for MicroPython urns (which does not fit the
// PICO-D4's 320 KB heap with LXMF). The RNS core is RTReticulum (MIT);
// the DTU-AT radio interface mirrors firmware/lib/dtu/dtu_at.py.
#include <cstdio>
#include <memory>
#include <string>

#include "esp_log.h"
#include "nvs_flash.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "rtreticulum/identity.h"
#include "rtreticulum/destination.h"
#include "rtreticulum/transport.h"
#include "rtreticulum/reticulum.h"
#include "uart_at_interface.h"

using namespace RNS;
static const char* TAG = "sprout";

static std::string hexstr(const Bytes& b) {
    static const char* H = "0123456789abcdef";
    std::string s; s.reserve(b.size() * 2);
    for (size_t i = 0; i < b.size(); ++i) {
        s.push_back(H[b.data()[i] >> 4]);
        s.push_back(H[b.data()[i] & 0x0f]);
    }
    return s;
}

extern "C" void app_main(void);

void app_main() {
    ESP_LOGI(TAG, "Sprout native client starting");

    esp_err_t nv = nvs_flash_init();
    if (nv == ESP_ERR_NVS_NO_FREE_PAGES || nv == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs_flash_init();
    }

    // DTU-AT (RAK3172 P2P) LoRa interface on UART2 (G22 TX / G19 RX).
    auto dt = std::make_shared<UartAtInterface>();
    Transport::register_interface(dt);

    // TODO: persist the Sprout identity so the node keeps the same identity the
    // MicroPython runtime had (61af0d5bdce5dd775c876f67d9702eb2) across boots —
    // load the private key from NVS instead of a fresh key each boot.
    Identity identity;
    ESP_LOGI(TAG, "identity hash: %s", hexstr(identity.get_salt()).c_str());

    Destination dest(identity, Type::Destination::IN, Type::Destination::SINGLE,
                     "lmao", "sprout");
    Transport::register_destination(dest);
    ESP_LOGI(TAG, "dest hash: %s", hexstr(dest.hash()).c_str());

    int announced = 0;
    Transport::on_announce([&](const Bytes& dh, const Identity& peer, const Bytes&) {
        announced++;
        ESP_LOGI(TAG, "<< announce from peer dest=%s id=%s (total=%d)",
                 hexstr(dh).c_str(), hexstr(peer.get_salt()).c_str(), announced);
    });

    if (!Reticulum::start(100, 8192, 5)) {
        ESP_LOGE(TAG, "Reticulum::start failed");
        return;
    }

    for (;;) {
        dest.announce(Bytes(), true);
        ESP_LOGI(TAG, "announced (peer announces so far: %d)", announced);
        vTaskDelay(pdMS_TO_TICKS(30000));
    }
}
