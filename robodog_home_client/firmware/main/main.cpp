// RoboDog home client (Waveshare ESP32-S3 RLCD-4.2, ESP-IDF).
//
// Reports air temperature + humidity from the board's SHTC3 to the LMAO
// server over the Reticulum TCP interface, and renders its own history as a
// chart on the ST7305 reflective panel.
//
// Per cycle (RH_INTERVAL_SECONDS):
//   1. read SHTC3 -> LMAOEnvelope{SensorReport} (sensor_id 3 temp, 2 humidity)
//   2. send it opportunistically to the server's lxmf/delivery destination
//   3. request the air-series history (HistoryRequest: AIR_TEMP + AIR_HUMIDITY)
//   4. the server replies LMAOEnvelope{ChartBundle}; render it as a chart
//
// Identity: pinned at build time (RH_NODE_IDENTITY_HEX, install_all-style
// no-drift scheme) and persisted in NVS, so the delivery hash never drifts.
// WiFi/server settings and the device name come from NVS (written by
// flash.sh at flash time) — no credentials are compiled into the source.
#include <cstdio>
#include <cstring>
#include <ctime>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_task_wdt.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "nvs.h"
#include "nvs_flash.h"

#include "rtreticulum/destination.h"
#include "rtreticulum/identity.h"
#include "rtreticulum/reticulum.h"
#include "rtreticulum/transport.h"

#include "config.h"
#include "env_net.h"
#include "lma_chart.h"
#include "lma_identity.h"
#include "shtc3.h"
#include "tcp_interface.h"
#include "ui.h"
#include "wifi_helper.h"

using namespace RNS;
using lma_identity::hexstr;

static const char* TAG = "robodog_home";

// NVS keys (namespace "robodog_home") — flash.sh writes these at flash time.
static const char* NVS_NS = "robodog_home";
static const char* K_SSID = "wifi_ssid";
static const char* K_PASS = "wifi_pass";
static const char* K_HOST = "server_host";
static const char* K_PORT = "server_port";
static const char* K_NAME = "device_name";

#ifndef RH_DEST_HASH_HEX
#define RH_DEST_HASH_HEX ""
#endif
#ifndef RH_NODE_IDENTITY_HEX
#define RH_NODE_IDENTITY_HEX ""
#endif

struct rh_config_t {
    char ssid[33];
    char pass[65];
    char host[64];
    uint16_t port;
    char name[40];
};

static rh_config_t s_cfg;
static SemaphoreHandle_t s_lock = nullptr;
static Identity s_server_identity;
static bool s_have_server = false;
static bool s_reply_pending = false;
static lma_chart::ChartBundle s_reply;
static lma_chart::ChartBundle s_shown;   // last successfully received series
static bool s_have_shown = false;
static uint32_t s_seq = 0;
static uint32_t s_chart_seq = 0;

static void nvs_load(void) {
    snprintf(s_cfg.ssid, sizeof(s_cfg.ssid), "%s", RH_WIFI_SSID_DEFAULT);
    snprintf(s_cfg.pass, sizeof(s_cfg.pass), "%s", RH_WIFI_PASS_DEFAULT);
    snprintf(s_cfg.host, sizeof(s_cfg.host), "%s", RH_SERVER_HOST_DEFAULT);
    s_cfg.port = (uint16_t)RH_SERVER_PORT_DEFAULT;
    snprintf(s_cfg.name, sizeof(s_cfg.name), "%s", RH_DEVICE_NAME_DEFAULT);

    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) != ESP_OK) {
        ESP_LOGW(TAG, "no NVS namespace '%s' — using build defaults", NVS_NS);
        return;
    }
    size_t sz = sizeof(s_cfg.ssid);
    (void)nvs_get_str(h, K_SSID, s_cfg.ssid, &sz);
    sz = sizeof(s_cfg.pass);
    (void)nvs_get_str(h, K_PASS, s_cfg.pass, &sz);
    sz = sizeof(s_cfg.host);
    (void)nvs_get_str(h, K_HOST, s_cfg.host, &sz);
    uint16_t port = 0;
    if (nvs_get_u16(h, K_PORT, &port) == ESP_OK && port != 0) s_cfg.port = port;
    sz = sizeof(s_cfg.name);
    (void)nvs_get_str(h, K_NAME, s_cfg.name, &sz);
    nvs_close(h);
    ESP_LOGI(TAG, "config: ssid='%s' server=%s:%u device='%s'",
             s_cfg.ssid, s_cfg.host, (unsigned)s_cfg.port, s_cfg.name);
}

static void on_announce_cb(const Bytes& dh, const Identity& peer, const Bytes&) {
    const std::string hash = hexstr(dh);
    if (RH_DEST_HASH_HEX[0] == '\0' || hash != std::string(RH_DEST_HASH_HEX)) {
        return;
    }
    if (s_lock) xSemaphoreTake(s_lock, portMAX_DELAY);
    s_server_identity = peer;
    s_have_server = true;
    if (s_lock) xSemaphoreGive(s_lock);
    ESP_LOGI(TAG, "server delivery identity learned (%s)", hash.c_str());
}

extern "C" void app_main(void);

void app_main(void) {
    ESP_LOGI(TAG, "RoboDog home client starting");

    esp_err_t nv = nvs_flash_init();
    if (nv == ESP_ERR_NVS_NO_FREE_PAGES || nv == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs_flash_init();
    }

    s_lock = xSemaphoreCreateMutex();
    nvs_load();

    const Identity identity = lma_identity::load_or_create(NVS_NS, RH_NODE_IDENTITY_HEX);
    ESP_LOGI(TAG, "identity hash: %s", hexstr(identity.get_salt()).c_str());
    const std::string delivery_hash = lma_identity::delivery_hash(identity);
    ESP_LOGI(TAG, "my lxmf/delivery hash: %s (add to server ALLOWED_CLIENTS)",
             delivery_hash.c_str());

    if (!ui::init()) {
        ESP_LOGE(TAG, "display init failed, halting");
        for (;;) vTaskDelay(pdMS_TO_TICKS(1000));
    }
    ui::render(s_cfg.name, nullptr, false, false, "booting");

    if (!shtc3_init()) {
        ESP_LOGE(TAG, "SHTC3 init failed — no readings will be reported");
    }

    const bool wifi_ok = wifi_connect(s_cfg.ssid, s_cfg.pass,
                                      RH_WIFI_CONNECT_TIMEOUT_S);
    ESP_LOGI(TAG, "wifi %s", wifi_ok ? "up" : "FAILED (tcp layer retries)");
    ui::render(s_cfg.name, nullptr, false, false, wifi_ok ? "wifi up" : "no wifi");

    auto tcp = lma_tcp::TcpInterface::create(s_cfg.host, s_cfg.port);
    tcp->start();
    Transport::register_interface(tcp);

    // Node destination: makes the board addressable/visible on the mesh.
    Destination node_dest(identity, Type::Destination::IN,
                          Type::Destination::SINGLE, "lmao", "robodog_home");
    Transport::register_destination(node_dest);

    // IN lxmf/delivery destination: the server's ChartBundle replies land here.
    Destination delivery = env_net::init_delivery(
        identity, [](const lma_chart::ChartBundle& b) {
            if (s_lock) xSemaphoreTake(s_lock, portMAX_DELAY);
            s_reply = b;
            s_reply_pending = true;
            if (s_lock) xSemaphoreGive(s_lock);
        });

    // The announce carries the LXMF peer data (display name = device name), so
    // the server's contact book and the host home client see the device name.
    const Bytes app_data = env_net::announce_app_data(
        s_cfg.name[0] ? s_cfg.name : delivery_hash);
    Transport::on_announce(on_announce_cb);

    if (!Reticulum::start(100, 8192, 5)) {
        ESP_LOGE(TAG, "Reticulum::start failed");
        return;
    }

    const esp_task_wdt_config_t wdt_cfg = {
        .timeout_ms = RH_WDT_TIMEOUT_S * 1000u,
        .idle_core_mask = 0,
        .trigger_panic = true,
    };
    if (esp_task_wdt_reconfigure(&wdt_cfg) != ESP_OK) {
        esp_task_wdt_init(&wdt_cfg);
    }
    esp_task_wdt_add(NULL);

    uint32_t last_announce_ms = 0;
    uint32_t last_report_ms = 0;
    uint64_t reply_deadline_ms = 0;
    bool awaiting_reply = false;
    bool s_waiting_shown = false;
    char status[32] = "waiting for server";

    for (;;) {
        const uint32_t now_ms = (uint32_t)(esp_timer_get_time() / 1000);
        esp_task_wdt_reset();

        if (last_announce_ms == 0 ||
            now_ms - last_announce_ms >= RH_ANNOUNCE_INTERVAL_MS) {
            last_announce_ms = now_ms;
            node_dest.announce(app_data, true);
            delivery.announce(app_data, true);
        }

        // Apply a stashed reply (rendering happens on this task only).
        bool have_reply = false;
        lma_chart::ChartBundle reply;
        if (s_lock) xSemaphoreTake(s_lock, portMAX_DELAY);
        if (s_reply_pending) {
            reply = s_reply;
            s_reply_pending = false;
            have_reply = true;
        }
        const bool have_server = s_have_server;
        const Identity server_identity = s_server_identity;
        if (s_lock) xSemaphoreGive(s_lock);

        if (have_reply) {
            awaiting_reply = false;
            s_shown = reply;
            s_have_shown = true;
            snprintf(status, sizeof(status), "updated");
            ui::render(s_cfg.name, &s_shown, true, false, status);
        }

        if (awaiting_reply && now_ms > reply_deadline_ms) {
            awaiting_reply = false;
            snprintf(status, sizeof(status), "no reply");
            ui::render(s_cfg.name, s_have_shown ? &s_shown : nullptr,
                       s_have_shown, true, status);
        }

        // Periodic sample + history request.
        if (last_report_ms == 0 ||
            now_ms - last_report_ms >= RH_INTERVAL_SECONDS * 1000u) {
            float temp_c = 0.0f, rh = 0.0f;
            const bool sensor_ok = shtc3_read(&temp_c, &rh);
            if (!sensor_ok) {
                ESP_LOGW(TAG, "SHTC3 read failed — skipping report");
            }

            if (have_server) {
                last_report_ms = now_ms;
                time_t now_s = time(nullptr);
                const uint64_t ts_ms =
                    now_s > 1700000000 ? (uint64_t)now_s * 1000ULL : 0;
                if (sensor_ok) {
                    s_seq++;
                    env_net::send_sensor_report(identity, server_identity,
                                                s_cfg.name, s_seq, temp_c, rh,
                                                ts_ms);
                }
                s_chart_seq++;
                env_net::send_history_request(identity, server_identity,
                                              s_chart_seq, RH_HISTORY_MAX, 0);
                awaiting_reply = true;
                reply_deadline_ms = now_ms + 30000u;
            } else {
                // Server not discovered yet (boot raced its announce): retry in
                // 5 s instead of burning a whole interval, and only redraw the
                // hint when it changes.
                last_report_ms = now_ms - RH_INTERVAL_SECONDS * 1000u + 5000u;
                if (!s_waiting_shown) {
                    s_waiting_shown = true;
                    ESP_LOGW(TAG, "server identity not learned yet — will retry");
                    snprintf(status, sizeof(status), "waiting for server");
                    ui::render(s_cfg.name, s_have_shown ? &s_shown : nullptr,
                               s_have_shown, false, status);
                }
            }
        }

        vTaskDelay(pdMS_TO_TICKS(RH_TICK_MS));
    }
}
