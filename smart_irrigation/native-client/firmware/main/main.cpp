// Sprout native client (ESP32-PICO-D4, ESP-IDF).
//
// Step 3 (issue #130): boot RNS + UART-AT (RAK3172 DTU P2P) interface on the
// LMAO mesh, register "lmao/sprout", announce every 30s.
// Step 4/5: every SEND_INTERVAL_MS (5 min) read the ENV III air temp +
// humidity (SHT30, G21/G25) AND the soil-moisture probe (ADC1_CH4/GPIO32),
// build ONE LMAOEnvelope SensorReport bundling all three readings
// (sensor_id 3=air temp °C, 2=humidity %, 4=soil moisture %), wrap it in a
// send-only LXMF message to the server's lxmf/delivery destination
// (DEST_HASH), and transmit it on the DTU link. Also learns the server identity
// from its announce (single-hop RNode path).
#include <cstdio>
#include <memory>
#include <string>

#include "esp_log.h"
#include "esp_timer.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "driver/gpio.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"

#include "rtreticulum/identity.h"
#include "rtreticulum/destination.h"
#include "rtreticulum/transport.h"
#include "rtreticulum/reticulum.h"
#include "uart_at_interface.h"
#include "path_find.h"
#include "sht30.h"
#include "moisture.h"
#include "lma_encoder.h"
#include "lxmf_send.h"

using namespace RNS;
static const char* TAG = "sprout";

// Server LXMF delivery destination hash (issue #127 / install_all DEST_HASH).
static const char* DEST_HASH_HEX = "dad35b80164b25f7b1474be86e443702";

// Sensor bundle cadence.  5 min per the Phase 0 algorithm evaluation: the
// irrigation decision runs on a ~5 min cadence and the ML dataset batches
// 6 x 5-min samples; 60 s pushes just added LoRa/DuckDB load with no
// control benefit.  The first report is sent immediately after boot.
#define SEND_INTERVAL_MS 300000UL

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
    uint64_t now_ms = (uint64_t)(esp_timer_get_time() / 1000);
    uint64_t unix_s = 946684800ULL + (uint64_t)(esp_timer_get_time() / 1000000ULL); // ~2000-epoch offset
    uint32_t seq = (uint32_t)(now_ms % 100000U);

    // Bundle every reading we can take into ONE SensorReport.  A Port A
    // SHT30 contact failure (#124) no longer drops the whole report — the
    // soil-moisture reading (the control input) still gets through.
    std::vector<std::string> readings;

    Sht30Reading r;
    if (sht30_read(&r)) {
        readings.push_back(lma_encoder::encode_reading(3, r.temp_c, "C", now_ms));
        readings.push_back(lma_encoder::encode_reading(2, r.hum_pct, "%", now_ms));
        ESP_LOGI(TAG, "SHT30 ok: temp=%.2f C hum=%.2f %%", r.temp_c, r.hum_pct);
    } else {
        ESP_LOGW(TAG, "SHT30 read failed (Port A contact? #124) — bundle continues without air T/H");
    }

    float moist_pct = 0.0f;
    if (moisture_read_percent(&moist_pct)) {
        readings.push_back(lma_encoder::encode_reading(4, moist_pct, "%", now_ms));
        ESP_LOGI(TAG, "moisture=%.1f%%", moist_pct);
    } else {
        ESP_LOGW(TAG, "moisture read failed");
    }

    if (readings.empty()) {
        ESP_LOGW(TAG, "no sensor readings available — skipping SensorReport");
        return;
    }

    std::string sreport = lma_encoder::encode_sensor_report(
        hexstr(my_identity.get_salt()), seq, 0.0f, readings);
    std::string envelope = lma_encoder::encode_envelope(sreport);
    ESP_LOGI(TAG, "SensorReport env=%uB readings=%u (temp/hum/moisture bundle)",
             (unsigned)envelope.size(), (unsigned)readings.size());

    Destination my_delivery(my_identity, Type::Destination::OUT, Type::Destination::SINGLE,
                            "lxmf", "delivery");
    if (s_ident_lock) xSemaphoreTake(s_ident_lock, portMAX_DELAY);
    bool have = s_have_server;
    Identity srv = s_server_identity;
    if (s_ident_lock) xSemaphoreGive(s_ident_lock);
    if (!have) {
        // TODO(#130): native RNS path-request hits an RTReticulum PLAIN-dest
        // abort (upstream). For now we discover the server via its periodic
        // announce (server announces every ~60s).
        ESP_LOGW(TAG, "server identity not learned yet — skipping LXMF send (awaiting server announce)");
        return;
    }
    Destination server_delivery(srv, Type::Destination::OUT, Type::Destination::SINGLE,
                                "lxmf", "delivery");
    ESP_LOGI(TAG, "server delivery hash=%s", hexstr(server_delivery.hash()).c_str());

    Bytes body = lxmf_send::build_body(my_delivery, server_delivery, my_identity,
                                       Bytes(envelope), Bytes("p:Envelope"), unix_s);
    Bytes frame = lxmf_send::opportunistic_frame(server_delivery, body);
    if (frame.empty()) { ESP_LOGW(TAG, "no opportunistic frame"); return; }
    ESP_LOGI(TAG, "TX LXMF frame %uB (temp/hum/moisture bundle) -> server",
             (unsigned)frame.size());
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

    // Persistent identity (NVS): keep the same RNS identity across boots so the
    // node is a stable mesh peer and can be whitelisted on the server (#130).
    // TODO: seed with the old MicroPython key (61af0d5b...) for full continuity.
    Identity identity;
    {
        nvs_handle_t nv = 0;
        if (nvs_open("sprout", NVS_READWRITE, &nv) == ESP_OK) {
            uint8_t key[64];
            size_t len = sizeof(key);
            if (nvs_get_blob(nv, "identity64", key, &len) == ESP_OK && len == 64) {
                Identity loaded(false);
                loaded.load_private_key(Bytes(key, 64));
                identity = loaded;
                ESP_LOGI(TAG, "loaded persisted identity from NVS");
            } else {
                identity = Identity(true);
                Bytes pk = identity.get_private_key();
                nvs_set_blob(nv, "identity64", pk.data(), pk.size());
                nvs_commit(nv);
                ESP_LOGI(TAG, "generated and persisted identity to NVS");
            }
            nvs_close(nv);
        } else {
            identity = Identity(true);
        }
    }
    ESP_LOGI(TAG, "identity hash: %s", hexstr(identity.get_salt()).c_str());
    {
        // The server whitelists the sender's lxmf/delivery hash (what it logs
        // as "From:") — print ours now so it can be added to the server list.
        Destination dm(identity, Type::Destination::OUT, Type::Destination::SINGLE,
                       "lxmf", "delivery");
        ESP_LOGI(TAG, "my lxmf/delivery hash: %s", hexstr(dm.hash()).c_str());
    }

    Destination dest(identity, Type::Destination::IN, Type::Destination::SINGLE,
                     "lmao", "sprout");
    Transport::register_destination(dest);
    ESP_LOGI(TAG, "dest hash: %s", hexstr(dest.hash()).c_str());

    // LXMF delivery destination (IN) — announced so the server can recall the
    // Sprout identity keyed by THIS delivery hash (f5f05952…). Without it the
    // server's LXMF get_source() resolves <unknown> and the allow-list gate
    // drops our SensorReport (the Cardputer works because its delivery hash is
    // announced; ours was only ever heard under lmao/sprout).
    Destination delivery(identity, Type::Destination::IN, Type::Destination::SINGLE,
                         "lxmf", "delivery");
    Transport::register_destination(delivery);
    ESP_LOGI(TAG, "delivery dest hash: %s", hexstr(delivery.hash()).c_str());

    Transport::on_announce(on_announce_cb);

    if (!Reticulum::start(100, 8192, 5)) {
        ESP_LOGE(TAG, "Reticulum::start failed");
        return;
    }

    uint64_t last_send_ms = 0;
    for (;;) {
        dest.announce(Bytes(), true);
        delivery.announce(Bytes(), true);
        ESP_LOGI(TAG, "announced");
        uint64_t now_ms = (uint64_t)(esp_timer_get_time() / 1000);
        // First report immediately after boot, then every SEND_INTERVAL_MS
        // (5 min) — the Phase 0 evaluation decision cadence.
        if (last_send_ms == 0 || now_ms - last_send_ms >= SEND_INTERVAL_MS) {
            last_send_ms = now_ms;
            send_sensor_report(identity);
        }
        vTaskDelay(pdMS_TO_TICKS(30000));
    }
}
