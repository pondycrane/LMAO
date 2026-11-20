// Humidifier Atom Socket controller (M5Stack Atom Socket / Atom Lite, ESP32).
//
// A LAN sensor + actuator for a mains humidifier: it owns the relay, the
// socket's HLW8032 power meter and an ENV III (SHT30) on I2C, exposes a small
// HTTP API, and reports its air temperature + humidity to the LMAO server as a
// SensorReport (node_id = NVS device_name).  The decision logic lives in the
// host controller (lmao_socket_humidifier/controller.py), which reads the
// socket's own /status (direct) or the server's history for that node
// (fallback); this firmware only measures, executes + reports.
//
// Safety: every ON request carries a LEASE.  If the controller dies, loses
// WiFi, or is restarted without renewing, the relay opens by itself when the
// lease expires.  There is no unleased ON path.
//
//   GET  /status          -> JSON snapshot (relay, lease, V/A/W, RSSI,
//                            temp_c, hum_pct, sensor_ok, sensor_age_s)
//   POST /on?lease=SECS   -> close the relay for SECS (default HF_DEFAULT_LEASE_S)
//   POST /off             -> open the relay now
//   GET  /healthz         -> "ok"
//
// Commands require the NVS-provisioned token in X-Auth-Token (or ?token=).
// An unprovisioned socket fails CLOSED: status works, commands are refused.
//
// The Atom Lite's RGB LED (GPIO27) is the relay indicator: lit red while the
// relay is closed, dark while it is open.  It is driven from relay_write(),
// the single place the GPIO changes, so the light cannot disagree with the
// relay.
#include <cstdio>
#include <cstdlib>
#include <cstring>

#include "esp_event.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "mdns.h"
#include "nvs.h"
#include "nvs_flash.h"

#include "driver/gpio.h"
#include "driver/uart.h"

#include "config.h"
#include "hlw8032.h"
#include "led_rgb.h"
#include "net_report.h"
#include "relay_lease.h"
#include "sht30.h"
#include "wifi_helper.h"

static const char* TAG = "humidifier";

// NVS namespace + keys — flash.sh writes these at flash time.  No credentials
// or token are ever compiled in.
static const char* NVS_NS = "humidifier";
static const char* K_SSID = "wifi_ssid";
static const char* K_PASS = "wifi_pass";
static const char* K_HOST = "mdns_host";
static const char* K_TOKEN = "api_token";
static const char* K_LEASE = "lease_default";
static const char* K_SERVER_HOST = "server_host";
static const char* K_SERVER_PORT = "server_port";
static const char* K_NAME = "device_name";

struct Config {
    char ssid[33];
    char pass[65];
    char host[40];
    char token[65];
    uint32_t lease_default_s;
    char server_host[64];
    uint16_t server_port;
    char name[40];
};

static Config s_cfg;
static SemaphoreHandle_t s_lock = nullptr;
static humidifier::Lease s_lease;
static humidifier::LeaseLimits s_limits;
static hlw8032::Sample s_meter;
static uint32_t s_meter_last_ms = 0;
static bool s_meter_seen = false;
static uint32_t s_energy_pulses = 0;
static bool s_last_pulse_bit = false;
static uint32_t s_relay_writes = 0;
static bool s_relay_state = false;
static bool s_led_ready = false;
static uint32_t s_refusals = 0;

// Metering diagnostics.  A dead HLW8032 looks identical to "no load" in the
// readings (0 V/0 A), so the raw UART evidence is exposed: a wiring/parity
// fault must be distinguishable from an empty outlet.
static uint32_t s_uart_bytes = 0;
static uint32_t s_frames_ok = 0;
static uint8_t s_first_raw[hlw8032::kFrameLen] = {};
static size_t s_first_raw_len = 0;

// ── ENV III (SHT30) sample cache ────────────────────────────────────
// The controller reads /status directly, so the last good sample + its age
// are part of the API contract.  A failed read never overwrites the last good
// value: stale-but-real beats a fabricated one, and the age lets the caller
// reject it.
static float s_temp_c = 0.0f;
static float s_hum_pct = 0.0f;
static bool s_sensor_ok = false;
static uint32_t s_sensor_ms = 0;
static uint32_t s_sensor_reads = 0;
static uint32_t s_sensor_fails = 0;
static uint32_t s_sensor_last_reprobe_ms = 0;

static uint32_t now_ms() {
    return (uint32_t)(esp_timer_get_time() / 1000);
}

// Idempotent: a lease renewal must not rewrite the GPIO (and bump the write
// counter) when the relay is already closed.
static void relay_write(bool on) {
    if (on == s_relay_state) return;
    gpio_set_level(HF_RELAY_PIN, on ? 1 : 0);
    s_relay_state = on;
    s_relay_writes++;
    // Indicator follows the GPIO in the same call, so there is no window in
    // which the light and the relay disagree.  A missing LED never blocks
    // control: s_led_ready is false and set_all() is a no-op.
    if (s_led_ready) lma_led::set_all(on ? lma_led::kRed : lma_led::kOff);
}

static void nvs_load() {
    snprintf(s_cfg.host, sizeof(s_cfg.host), "%s", HF_MDNS_HOST_DEFAULT);
    snprintf(s_cfg.server_host, sizeof(s_cfg.server_host), "%s",
             HF_SERVER_HOST_DEFAULT);
    s_cfg.server_port = (uint16_t)HF_SERVER_PORT_DEFAULT;
    snprintf(s_cfg.name, sizeof(s_cfg.name), "%s", HF_DEVICE_NAME_DEFAULT);
    s_cfg.lease_default_s = HF_DEFAULT_LEASE_S;

    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) != ESP_OK) {
        ESP_LOGW(TAG, "no NVS namespace '%s' — socket is UNPROVISIONED "
                      "(relay commands refused)", NVS_NS);
        return;
    }
    size_t sz = sizeof(s_cfg.ssid);
    (void)nvs_get_str(h, K_SSID, s_cfg.ssid, &sz);
    sz = sizeof(s_cfg.pass);
    (void)nvs_get_str(h, K_PASS, s_cfg.pass, &sz);
    sz = sizeof(s_cfg.host);
    (void)nvs_get_str(h, K_HOST, s_cfg.host, &sz);
    sz = sizeof(s_cfg.token);
    (void)nvs_get_str(h, K_TOKEN, s_cfg.token, &sz);
    sz = sizeof(s_cfg.server_host);
    (void)nvs_get_str(h, K_SERVER_HOST, s_cfg.server_host, &sz);
    uint16_t port = 0;
    if (nvs_get_u16(h, K_SERVER_PORT, &port) == ESP_OK && port != 0) {
        s_cfg.server_port = port;
    }
    sz = sizeof(s_cfg.name);
    (void)nvs_get_str(h, K_NAME, s_cfg.name, &sz);
    uint32_t lease = 0;
    if (nvs_get_u32(h, K_LEASE, &lease) == ESP_OK && lease != 0) {
        s_cfg.lease_default_s = lease;
    }
    nvs_close(h);
    ESP_LOGI(TAG, "config: ssid='%s' mdns='%s' token=%s lease_default=%us "
                  "server=%s:%u device='%s'",
             s_cfg.ssid, s_cfg.host,
             s_cfg.token[0] ? "set" : "MISSING",
             (unsigned)s_cfg.lease_default_s, s_cfg.server_host,
             (unsigned)s_cfg.server_port, s_cfg.name);
}

// ── HLW8032 metering ─────────────────────────────────────────────────
static void meter_init() {
    uart_config_t cfg = {};
    cfg.baud_rate = HF_METER_BAUD;
    cfg.data_bits = UART_DATA_8_BITS;
    cfg.parity = UART_PARITY_EVEN;   // HLW8032 transmits 8E1
    cfg.stop_bits = UART_STOP_BITS_1;
    cfg.flow_ctrl = UART_HW_FLOWCTRL_DISABLE;
    cfg.source_clk = UART_SCLK_DEFAULT;
    ESP_ERROR_CHECK(uart_driver_install(HF_METER_UART, 1024, 0, 0, nullptr, 0));
    ESP_ERROR_CHECK(uart_param_config(HF_METER_UART, &cfg));
    ESP_ERROR_CHECK(uart_set_pin(HF_METER_UART, UART_PIN_NO_CHANGE,
                                 HF_METER_RX_PIN, UART_PIN_NO_CHANGE,
                                 UART_PIN_NO_CHANGE));
}

static void meter_poll(hlw8032::FrameAssembler* asm_) {
    uint8_t chunk[64];
    int n = uart_read_bytes(HF_METER_UART, chunk, sizeof(chunk),
                            pdMS_TO_TICKS(20));
    if (n > 0) {
        s_uart_bytes += (uint32_t)n;
        if (s_first_raw_len == 0) {
            s_first_raw_len = (size_t)n < sizeof(s_first_raw) ? (size_t)n
                                                              : sizeof(s_first_raw);
            memcpy(s_first_raw, chunk, s_first_raw_len);
            char hex[3 * hlw8032::kFrameLen + 1] = {};
            for (size_t i = 0; i < s_first_raw_len; i++) {
                snprintf(hex + 3 * i, 4, "%02x ", s_first_raw[i]);
            }
            ESP_LOGI(TAG, "metering: first %u raw bytes [%s]",
                     (unsigned)s_first_raw_len, hex);
        }
    }

    uint8_t frame[hlw8032::kFrameLen];
    for (int i = 0; i < n; i++) {
        if (!asm_->feed(chunk[i], frame)) continue;
        hlw8032::Sample s;
        if (!hlw8032::parse_frame(frame, sizeof(frame), &s)) continue;
        const bool bit = (frame[20] & 0x80) != 0;
        if (bit && !s_last_pulse_bit) s_energy_pulses++;
        s_last_pulse_bit = bit;
        xSemaphoreTake(s_lock, portMAX_DELAY);
        s_meter = s;
        s_meter.energy_pulses = s_energy_pulses;
        s_meter_last_ms = now_ms();
        s_meter_seen = true;
        xSemaphoreGive(s_lock);
        s_frames_ok++;
    }
}

// ── ENV III (SHT30) ──────────────────────────────────────────────────
// Probe order: the Atom Lite's Grove port first (G26 SDA / G32 SCL, and the
// swapped polarity), then the 9-pin header's dedicated I2C (G25 SDA / G21
// SCL).  The socket's own 4-pin port has no I2C, so a unit plugged there will
// simply not answer; the log names every pair tried.
static void sensor_init() {
    static const int pairs[HF_ENV_PAIR_COUNT][2] = {
        {HF_ENV_SDA_G26, HF_ENV_SCL_G32},
        {HF_ENV_SCL_G32, HF_ENV_SDA_G26},
        {HF_ENV_SDA_G25, HF_ENV_SCL_G21},
        {HF_ENV_SCL_G21, HF_ENV_SDA_G25},
    };
    const int idx = lma_sensor::sht30_init(pairs, HF_ENV_PAIR_COUNT);
    if (idx < 0) {
        ESP_LOGE(TAG, "no ENV III detected — the room has no humidity input "
                      "from this socket (check the Grove/header wiring)");
        return;
    }
    lma_sensor::Reading r{};
    if (lma_sensor::sht30_read(&r)) {
        s_temp_c = r.temp_c;
        s_hum_pct = r.hum_pct;
        s_sensor_ok = true;
        s_sensor_ms = now_ms();
        s_sensor_reads++;
    }
}

static void sensor_poll(uint32_t t) {
    if (!lma_sensor::sht30_ok()) {
        // Rate-limit the re-probe: a Grove plug that lost contact can come
        // back, but deinstalling/reinstalling the driver every tick is noise.
        if (t - s_sensor_last_reprobe_ms < 300000u) return;
        s_sensor_last_reprobe_ms = t;
        ESP_LOGW(TAG, "ENV III unavailable — re-probing I2C pairs");
        if (!lma_sensor::sht30_reprobe()) return;
    }
    lma_sensor::Reading r{};
    if (lma_sensor::sht30_read(&r)) {
        s_temp_c = r.temp_c;
        s_hum_pct = r.hum_pct;
        s_sensor_ok = true;
        s_sensor_ms = t;
        s_sensor_reads++;
        ESP_LOGI(TAG, "ENV III: %.1f C / %.0f %%RH", r.temp_c, r.hum_pct);
    } else {
        s_sensor_fails++;
        ESP_LOGW(TAG, "ENV III read failed (reads=%u fails=%u) — keeping the "
                      "last good sample", (unsigned)s_sensor_reads,
                 (unsigned)s_sensor_fails);
    }
}

// ── HTTP API ─────────────────────────────────────────────────────────
static bool token_ok(httpd_req_t* req) {
    if (s_cfg.token[0] == '\0') return false;  // fail closed
    char hdr[96];
    if (httpd_req_get_hdr_value_str(req, "X-Auth-Token", hdr, sizeof(hdr)) ==
        ESP_OK) {
        return strcmp(hdr, s_cfg.token) == 0;
    }
    char query[128];
    char tok[65];
    if (httpd_req_get_url_query_str(req, query, sizeof(query)) == ESP_OK &&
        httpd_query_key_value(query, "token", tok, sizeof(tok)) == ESP_OK) {
        return strcmp(tok, s_cfg.token) == 0;
    }
    return false;
}

static esp_err_t refuse(httpd_req_t* req, const char* why) {
    s_refusals++;
    httpd_resp_set_status(req, "403 Forbidden");
    httpd_resp_set_type(req, "application/json");
    char body[96];
    snprintf(body, sizeof(body), "{\"error\":\"%s\"}", why);
    return httpd_resp_sendstr(req, body);
}

static esp_err_t status_get(httpd_req_t* req) {
    xSemaphoreTake(s_lock, portMAX_DELAY);
    const humidifier::Lease lease = s_lease;
    hlw8032::Sample meter = s_meter;
    const uint32_t meter_last = s_meter_last_ms;
    const bool meter_seen = s_meter_seen;
    const float temp_c = s_temp_c;
    const float hum_pct = s_hum_pct;
    const bool sensor_ok = s_sensor_ok;
    const uint32_t sensor_ms = s_sensor_ms;
    const uint32_t sensor_reads = s_sensor_reads;
    const uint32_t sensor_fails = s_sensor_fails;
    xSemaphoreGive(s_lock);

    const uint32_t t = now_ms();
    meter.meter_fresh = meter_seen && (t - meter_last) < HF_METER_STALE_MS;
    // -1 = no sample ever; otherwise the age the caller's stale_s check uses.
    const long sensor_age_s =
        sensor_ms == 0 ? -1L : (long)((t - sensor_ms) / 1000u);

    // Raw UART evidence is part of the contract: "0 V" must be explainable as
    // either an empty outlet (frames flowing) or a dead meter link (no bytes).
    char first_raw[3 * hlw8032::kFrameLen + 1] = {};
    for (size_t i = 0; i < s_first_raw_len; i++) {
        snprintf(first_raw + 3 * i, 4, "%02x ", s_first_raw[i]);
    }

    char body[1280];
    int n = snprintf(
        body, sizeof(body),
        "{\"relay\":%s,\"lease_s\":%u,\"on_ms_total\":%u,\"relay_writes\":%u,"
        "\"refusals\":%u,\"meter_fresh\":%s,\"voltage_v\":%.2f,"
        "\"current_a\":%.3f,\"power_w\":%.2f,\"pf_raw\":%u,"
        "\"energy_pulses\":%u,\"uart_bytes\":%u,\"frames_ok\":%u,"
        "\"meter_rx_pin\":%d,"
        "\"temp_c\":%.2f,\"hum_pct\":%.1f,\"sensor_ok\":%s,"
        "\"sensor_age_s\":%ld,\"sensor_reads\":%u,\"sensor_fails\":%u,"
        "\"sensor_sda\":%d,\"sensor_scl\":%d,"
        "\"server_known\":%s,\"reports_sent\":%u,\"reports_failed\":%u,"
        "\"device_name\":\"%s\",\"server_host\":\"%s\",\"server_port\":%u,"
        "\"led\":\"%s\","
        "\"first_raw\":\"%s\",\"uptime_s\":%u,\"mdns_host\":\"%s\"}",
        lease.on ? "true" : "false",
        (unsigned)humidifier::lease_remaining_s(lease, t),
        (unsigned)lease.on_ms_total, (unsigned)s_relay_writes,
        (unsigned)s_refusals, meter.meter_fresh ? "true" : "false",
        meter.voltage, meter.current_a, meter.power_w,
        (unsigned)meter.pf_raw, (unsigned)meter.energy_pulses,
        (unsigned)s_uart_bytes, (unsigned)s_frames_ok,
        (int)HF_METER_RX_PIN,
        temp_c, hum_pct, sensor_ok ? "true" : "false",
        sensor_age_s, (unsigned)sensor_reads, (unsigned)sensor_fails,
        lma_sensor::sht30_sda(), lma_sensor::sht30_scl(),
        net_report::server_known() ? "true" : "false",
        (unsigned)net_report::reports_sent(),
        (unsigned)net_report::reports_failed(),
        s_cfg.name, s_cfg.server_host, (unsigned)s_cfg.server_port,
        s_relay_state ? "red" : "off", first_raw,
        (unsigned)(t / 1000), s_cfg.host);
    (void)n;
    httpd_resp_set_type(req, "application/json");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    return httpd_resp_sendstr(req, body);
}

static esp_err_t on_post(httpd_req_t* req) {
    if (!token_ok(req)) return refuse(req, "bad or missing token");

    uint32_t lease_s = 0;
    char query[128];
    char val[16];
    if (httpd_req_get_url_query_str(req, query, sizeof(query)) == ESP_OK &&
        httpd_query_key_value(query, "lease", val, sizeof(val)) == ESP_OK) {
        lease_s = (uint32_t)strtoul(val, nullptr, 10);
    }

    xSemaphoreTake(s_lock, portMAX_DELAY);
    const uint32_t granted =
        humidifier::lease_arm(&s_lease, lease_s, now_ms(), s_limits);
    relay_write(true);
    xSemaphoreGive(s_lock);
    ESP_LOGI(TAG, "relay ON (lease %us)", (unsigned)granted);

    char body[64];
    snprintf(body, sizeof(body), "{\"relay\":true,\"lease_s\":%u}",
             (unsigned)granted);
    httpd_resp_set_type(req, "application/json");
    return httpd_resp_sendstr(req, body);
}

static esp_err_t off_post(httpd_req_t* req) {
    if (!token_ok(req)) return refuse(req, "bad or missing token");
    xSemaphoreTake(s_lock, portMAX_DELAY);
    humidifier::lease_clear(&s_lease, now_ms());
    relay_write(false);
    xSemaphoreGive(s_lock);
    ESP_LOGI(TAG, "relay OFF (controller request)");
    httpd_resp_set_type(req, "application/json");
    return httpd_resp_sendstr(req, "{\"relay\":false,\"lease_s\":0}");
}

static esp_err_t health_get(httpd_req_t* req) {
    httpd_resp_set_type(req, "text/plain");
    return httpd_resp_sendstr(req, "ok");
}

static void http_start() {
    httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
    cfg.server_port = HF_HTTP_PORT;
    cfg.lru_purge_enable = true;
    cfg.max_open_sockets = 4;

    httpd_handle_t server = nullptr;
    ESP_ERROR_CHECK(httpd_start(&server, &cfg));

    const httpd_uri_t routes[] = {
        {.uri = "/status", .method = HTTP_GET, .handler = status_get},
        {.uri = "/on", .method = HTTP_POST, .handler = on_post},
        {.uri = "/off", .method = HTTP_POST, .handler = off_post},
        {.uri = "/healthz", .method = HTTP_GET, .handler = health_get},
    };
    for (const auto& r : routes) ESP_ERROR_CHECK(httpd_register_uri_handler(server, &r));
    ESP_LOGI(TAG, "HTTP API on :%d (GET /status, POST /on?lease=N, POST /off)",
             HF_HTTP_PORT);
}

static void mdns_start() {
    if (mdns_init() != ESP_OK) {
        ESP_LOGW(TAG, "mDNS init failed (HTTP still reachable by IP)");
        return;
    }
    mdns_hostname_set(s_cfg.host);
    mdns_instance_name_set("LMAO humidifier socket");
    mdns_service_add(nullptr, "_http", "_tcp", HF_HTTP_PORT, nullptr, 0);
    ESP_LOGI(TAG, "mDNS: http://%s.local/", s_cfg.host);
}

extern "C" void app_main(void);

void app_main(void) {
    ESP_LOGI(TAG, "Humidifier Atom Socket starting");

    esp_err_t nv = nvs_flash_init();
    if (nv == ESP_ERR_NVS_NO_FREE_PAGES || nv == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs_flash_init();
    }

    s_lock = xSemaphoreCreateMutex();
    nvs_load();
    s_limits.default_s = s_cfg.lease_default_s;
    s_limits.max_s = HF_MAX_LEASE_S;

    // Relay first, explicitly OPEN: a booting socket must never energize the
    // appliance before the controller asks.
    gpio_config_t io = {};
    io.pin_bit_mask = 1ULL << HF_RELAY_PIN;
    io.mode = GPIO_MODE_OUTPUT;
    io.pull_up_en = GPIO_PULLUP_DISABLE;
    io.pull_down_en = GPIO_PULLDOWN_DISABLE;
    io.intr_type = GPIO_INTR_DISABLE;
    ESP_ERROR_CHECK(gpio_config(&io));
    gpio_set_level(HF_RELAY_PIN, 0);
    s_relay_state = false;
    ESP_LOGI(TAG, "relay GPIO%d OPEN at boot", (int)HF_RELAY_PIN);

    // Indicator: dark at boot (relay open), red while closed.
    s_led_ready = lma_led::init((int)HF_LED_PIN, HF_LED_PIXELS, HF_LED_RMT_CHANNEL);
    if (s_led_ready) lma_led::set_all(lma_led::kOff);

    meter_init();
    sensor_init();

    const bool wifi_ok = lma_wifi::connect(s_cfg.ssid, s_cfg.pass,
                                           HF_WIFI_CONNECT_TIMEOUT_S);
    ESP_LOGI(TAG, "wifi %s", wifi_ok ? "up" : "FAILED (will keep retrying)");

    http_start();
    mdns_start();

    // The room's sensor AND actuator: report the ENV III sample to the server
    // so ENV_HISTORY[device_name] keeps a series (the controller falls back to
    // it).  A failed RNS start leaves HTTP relay control intact.
    net_report::init({s_cfg.server_host, s_cfg.server_port, s_cfg.name});
    if (wifi_ok && !net_report::start()) {
        ESP_LOGW(TAG, "reporting disabled — /status still carries the sample");
    }

    hlw8032::FrameAssembler assembler;
    uint32_t last_tick = now_ms();
    uint32_t last_sensor_ms = now_ms();
    for (;;) {
        meter_poll(&assembler);

        const uint32_t t = now_ms();
        if (t - last_tick < HF_TICK_MS) {
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }
        last_tick = t;

        if (t - last_sensor_ms >= HF_SENSOR_INTERVAL_S * 1000u) {
            last_sensor_ms = t;
            sensor_poll(t);
        }
        net_report::tick(t, s_sensor_ok, s_temp_c, s_hum_pct);

        // Deadman: an expired lease opens the relay, whatever the controller
        // is doing.
        xSemaphoreTake(s_lock, portMAX_DELAY);
        if (humidifier::lease_expired(s_lease, t)) {
            humidifier::lease_clear(&s_lease, t);
            relay_write(false);
            ESP_LOGW(TAG, "lease expired — relay OPEN (fail-safe)");
        }
        xSemaphoreGive(s_lock);
    }
}
