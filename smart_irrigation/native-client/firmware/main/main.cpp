// Sprout native client (ESP32-PICO-D4, ESP-IDF).
//
// Step 3 (issue #130): boot RNS + UART-AT (RAK3172 DTU P2P) interface on the
// LMAO mesh, register "lmao/sprout", announce every 30s.
// Step 4: every 60s read the ENV III air temp + humidity (SHT30, G21/G25),
// build an LMAOEnvelope SensorReport (sensor_id 3=air temp, 2=humidity), wrap
// it in a send-only LXMF message to the server's lxmf/delivery destination
// (DEST_HASH), and transmit it on the DTU link. Also learns the server identity
// from its announce (single-hop RNode path).
#include <cstdio>
#include <memory>
#include <string>

#include "esp_log.h"
#include "esp_timer.h"
#include "nvs_flash.h"
#include "driver/gpio.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"

#include "rtreticulum/identity.h"
#include "rtreticulum/destination.h"
#include "rtreticulum/transport.h"
#include "rtreticulum/reticulum.h"
#include "uart_at_interface.h"
#include "sht30.h"
#include "lma_encoder.h"
#include "lxmf_send.h"

using namespace RNS;
static const char* TAG = "sprout";

// Server LXMF delivery destination hash (issue #127 / install_all DEST_HASH).
static const char* DEST_HASH_HEX = "dad35b80164b25f7b1474be86e443702";

static std::string hexstr(const Bytes& b) {
    static const char* H = "0123456789abcdef";
    std::string s; s.reserve(b.size() * 2);
    for (size_t i = 0; i < b.size(); ++i) {
        s.push_back(H[b.data()[i] >> 4]);
        s.push_back(H[b.data()[i] & 0x0f]);
    }
    return s;
}

static SemaphoreHandle_t s_ident_lock = nullptr;
static Identity s_server_identity;                 // learned from the server announce
static bool s_have_server = false;

static void on_announce_cb(const Bytes& dh, const Identity& peer, const Bytes&) {
    ESP_LOGI(TAG, "<< announce dest=%s id=%s", hexstr(dh).c_str(),
             hexstr(peer.get_salt()).c_str());
    if (hexstr(dh) == std::string(DEST_HASH_HEX)) {
        if (s_ident_lock) xSemaphoreTake(s_ident_lock, portMAX_DELAY);
        s_server_identity = peer;
        s_have_server = true;
        if (s_ident_lock) xSemaphoreGive(s_ident_lock);
        ESP_LOGI(TAG, "server delivery identity learned");
    }
}

static void send_sensor_report(const Identity& my_identity) {
    Sht30Reading r;
    if (!sht30_read(&r)) {
        ESP_LOGW(TAG, "SHT30 read failed (Port A contact? #124)");
        return;
    }
    uint64_t now_ms = (uint64_t)(esp_timer_get_time() / 1000);
    uint64_t unix_s = 946684800ULL + (uint64_t)(esp_timer_get_time() / 1000000ULL); // ~2000-epoch offset
    uint32_t seq = (uint32_t)(now_ms % 100000U);

    std::string rd_t = lma_encoder::encode_reading(3, r.temp_c, "C", now_ms);
    std::string rd_h = lma_encoder::encode_reading(2, r.hum_pct, "%", now_ms);
    std::string sreport = lma_encoder::encode_sensor_report(
        hexstr(my_identity.get_salt()), seq, 0.0f, {rd_t, rd_h});
    std::string envelope = lma_encoder::encode_envelope(sreport);
    ESP_LOGI(TAG, "SensorReport env=%uB temp=%.2f hum=%.2f",
             (unsigned)envelope.size(), r.temp_c, r.hum_pct);

    Destination my_delivery(my_identity, Type::Destination::OUT, Type::Destination::SINGLE,
                            "lxmf", "delivery");
    if (s_ident_lock) xSemaphoreTake(s_ident_lock, portMAX_DELAY);
    bool have = s_have_server;
    Identity srv = s_server_identity;
    if (s_ident_lock) xSemaphoreGive(s_ident_lock);
    if (!have) {
        ESP_LOGW(TAG, "server identity not learned yet — skipping LXMF send");
        return;
    }
    Destination server_delivery(srv, Type::Destination::OUT, Type::Destination::SINGLE,
                                "lxmf", "delivery");
    ESP_LOGI(TAG, "server delivery hash=%s", hexstr(server_delivery.hash()).c_str());

    Bytes body = lxmf_send::build_body(my_delivery, server_delivery, my_identity,
                                       Bytes(envelope), Bytes("p:Envelope"), unix_s);
    Bytes frame = lxmf_send::opportunistic_frame(server_delivery, body);
    if (frame.empty()) { ESP_LOGW(TAG, "no opportunistic frame"); return; }
    ESP_LOGI(TAG, "TX LXMF frame %uB (air temp + humidity) -> server", (unsigned)frame.size());
    Transport::broadcast(frame, nullptr);
}

extern "C" void app_main(void);

void app_main() {
    ESP_LOGI(TAG, "Sprout native client starting");

    // SAFETY (#119): drive the watering-unit pump enable LOW (OFF) as the very
    // FIRST action — never leave the pump control line floating.
    {
        gpio_config_t io = {};
        io.pin_bit_mask = (1ULL << GPIO_NUM_26);
        io.mode = GPIO_MODE_OUTPUT;
        gpio_config(&io);
        gpio_set_level(GPIO_NUM_26, 0);
        ESP_LOGI(TAG, "pump enable G26 driven LOW (OFF) as first action");
    }

    esp_err_t nv = nvs_flash_init();
    if (nv == ESP_ERR_NVS_NO_FREE_PAGES || nv == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs_flash_init();
    }

    s_ident_lock = xSemaphoreCreateMutex();

    auto dt = std::make_shared<UartAtInterface>();
    Transport::register_interface(dt);
    bool dt_up = dt->start();   // configures the RAK3172 DTU P2P radio + PRECV on (Reticulum only calls loop())
    if (dt_up) {
        ESP_LOGI(TAG, "DTU radio interface started");
    } else {
        ESP_LOGW(TAG, "DTU radio interface failed to start");
    }

    // TODO: persist identity (61af0d5b...) to NVS so the node keeps its identity
    // across boots (and across the MicroPython -> native migration).
    Identity identity;
    ESP_LOGI(TAG, "identity hash: %s", hexstr(identity.get_salt()).c_str());

    Destination dest(identity, Type::Destination::IN, Type::Destination::SINGLE,
                     "lmao", "sprout");
    Transport::register_destination(dest);
    ESP_LOGI(TAG, "dest hash: %s", hexstr(dest.hash()).c_str());

    Transport::on_announce(on_announce_cb);

    if (!Reticulum::start(100, 8192, 5)) {
        ESP_LOGE(TAG, "Reticulum::start failed");
        return;
    }

    uint64_t last_send_ms = 0;
    for (;;) {
        dest.announce(Bytes(), true);
        ESP_LOGI(TAG, "announced");
        uint64_t now_ms = (uint64_t)(esp_timer_get_time() / 1000);
        if (now_ms - last_send_ms >= 60000) {     // SensorReport ~every 60s
            last_send_ms = now_ms;
            send_sensor_report(identity);
        }
        vTaskDelay(pdMS_TO_TICKS(30000));
    }
}
