// WiFi STA connect + SNTP time sync for the RoboDog native client (ESP-IDF).
#include "wifi_helper.h"

#include <cstring>
#include <ctime>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_netif_sntp.h"
#include "esp_sntp.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"

static const char* TAG = "wifi";
static EventGroupHandle_t s_wifi_eg = nullptr;
static constexpr int BIT_GOT_IP = BIT0;
static constexpr int BIT_FAILED = BIT1;
static int s_retry = 0;

static void wifi_event_handler(void*, esp_event_base_t base, int32_t id, void*) {
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        if (s_retry < 10) { s_retry++; esp_wifi_connect(); }
        else if (s_wifi_eg) xEventGroupSetBits(s_wifi_eg, BIT_FAILED);
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        if (s_wifi_eg) xEventGroupSetBits(s_wifi_eg, BIT_GOT_IP);
    }
}

bool wifi_connect(const char* ssid, const char* pass, int timeout_s) {
    s_wifi_eg = xEventGroupCreate();
    ESP_ERROR_CHECK(esp_netif_init());
    esp_err_t err = esp_event_loop_create_default();
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) ESP_ERROR_CHECK(err);
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                                        &wifi_event_handler, nullptr, nullptr));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                                        &wifi_event_handler, nullptr, nullptr));

    wifi_config_t wcfg = {};
    strncpy((char*)wcfg.sta.ssid, ssid, sizeof(wcfg.sta.ssid) - 1);
    strncpy((char*)wcfg.sta.password, pass, sizeof(wcfg.sta.password) - 1);
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wcfg));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_LOGI(TAG, "connecting to SSID '%s' ...", ssid);

    EventBits_t bits = xEventGroupWaitBits(s_wifi_eg, BIT_GOT_IP | BIT_FAILED,
                                           pdTRUE, pdFALSE, pdMS_TO_TICKS(timeout_s * 1000));
    if (!(bits & BIT_GOT_IP)) {
        ESP_LOGW(TAG, "WiFi connect %s", (bits & BIT_FAILED) ? "failed" : "timed out");
        return false;
    }
    ESP_LOGI(TAG, "WiFi connected, got IP");

    // SNTP: block until the clock is real so unix timestamps are valid.
    esp_sntp_config_t sntp_cfg = ESP_NETIF_SNTP_DEFAULT_CONFIG("pool.ntp.org");
    esp_netif_sntp_init(&sntp_cfg);
    const int64_t deadline = esp_timer_get_time() + 10 * 1000 * 1000LL;
    while (time(NULL) <= 1700000000 && esp_timer_get_time() < deadline) {
        vTaskDelay(pdMS_TO_TICKS(250));
    }
    const bool synced = time(NULL) > 1700000000;
    ESP_LOGI(TAG, "SNTP %s (now=%lld)", synced ? "synced" : "timed out",
             (long long)time(NULL));
    return true;
}
