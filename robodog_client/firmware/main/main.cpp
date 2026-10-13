// RoboDog LVGL UI framework demo (Waveshare ESP32-S3 RLCD-4.2, ESP-IDF + LVGL 9).
//
// PR 1 — UI framework validation build: boots LVGL on the ST7305 reflective
// LCD and renders the declarative CalendarScreen (ui_xml/calendar_screen.xml)
// through subjects — a header band with a live clock, a status label, and six
// event rows fed by a static demo dataset. Rotates the demo events every 20 s
// so subject-driven re-rendering is visibly exercised on the panel.
//
// No networking in this build: the data source (the stable framed-TCP
// GetCalendar client the robodog_native board build already speaks, then the
// MicroPython/urns RNS-TCP runtime) lands in the follow-up PRs.
#include <cstdio>
#include <cstring>
#include <ctime>
#include <vector>

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "lvgl.h"

#include "calendar_model.h"
#include "calendar_ui.h"

using namespace calendar_ui;
#include "st7305_disp.h"

static const char* TAG = "robodog";

#define TICK_MS 250UL
#define DEMO_ROTATE_S 20UL

static const char* DEMO_TITLES[] = {
    "Swim class",         "Dentist visit",     "Piano lesson",
    "Family dinner",      "Grocery run",       "Park playdate",
    "School holiday",     "Swim class",        "Grandpa visit",
    "Martial arts",
};
static const uint64_t DEMO_BASE_MS = 1760265000000ULL;  // demo anchor time
static const uint64_t DEMO_STEP_MIN = 90;               // event spacing

static std::vector<calendar_model::UiEvent> demo_events(size_t count, uint64_t offset_min) {
    std::vector<calendar_model::UiEvent> out;
    for (size_t i = 0; i < count; ++i) {
        calendar_model::UiEvent ev;
        ev.title = DEMO_TITLES[(i + offset_min) % (sizeof(DEMO_TITLES) / sizeof(DEMO_TITLES[0]))];
        ev.start_ms = DEMO_BASE_MS + (offset_min + (uint64_t)i * DEMO_STEP_MIN) * 60000ULL;
        ev.end_ms = ev.start_ms + 3600000ULL;
        ev.uid = "demo-";
        ev.uid += std::to_string(i);
        out.push_back(ev);
    }
    return out;
}

static void apply_screen(uint64_t now_ms, const std::vector<calendar_model::UiEvent>& events) {
    set_header(
        calendar_model::header_text(now_ms).c_str());
    set_offline(false);
    calendar_ui::set_events(events.data(), events.size());
}

extern "C" void app_main(void);

void app_main() {
    ESP_LOGI(TAG, "RoboDog LVGL UI framework demo booting");

    if (!st7305_disp_init()) {
        ESP_LOGE(TAG, "ST7305 display init failed — halting");
        while (true) vTaskDelay(pdMS_TO_TICKS(1000));
    }
    if (!init()) {
        ESP_LOGE(TAG, "calendar_ui init failed — halting");
        while (true) vTaskDelay(pdMS_TO_TICKS(1000));
    }

    const uint64_t t0 = 946684800ULL * 1000ULL;  // host-side epoch anchor
    uint64_t tick0 = (uint64_t)(esp_timer_get_time() / 1000);

    uint64_t last_apply = 0;
    uint64_t rotate = 0;

    for (;;) {
        lv_task_handler();
        vTaskDelay(pdMS_TO_TICKS(TICK_MS));

        const uint64_t now = (uint64_t)(esp_timer_get_time() / 1000);
        if (last_apply == 0 || now - last_apply >= 5000) {
            last_apply = now;
            // Demo clock: uptime seconds on dev boards without SNTP.
            const uint64_t demo_now = t0 + (now - tick0);
            apply_screen(demo_now, demo_events(calendar_model::MAX_ROWS, rotate));
            ESP_LOGI(TAG, "screen updated (demo rotate=%llu)", (unsigned long long)rotate);
        }
        if (now - (uint64_t)(rotate * DEMO_ROTATE_S * 1000ULL) >= (uint64_t)DEMO_ROTATE_S * 1000ULL) {
            rotate += 1;
        }
    }
}
