// RoboDog family-calendar client — LVGL 9 UI + LMAO gRPC calendar sync.
//
// Entry point: inits the ST7305/LVGL display and the declarative calendar
// screen, then hands off to robodog_core (its own task: WiFi + gRPC
// GetCalendar over :50051, incremental NVS cache, pic-tile decode) and pumps
// lv_task_handler here. Rendering flows through the XML+subjects screen.
#include <cstdio>

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "lvgl.h"

#include "calendar_ui.h"
#include "st7305_disp.h"

static const char* TAG = "robodog";

extern "C" void app_main(void);
extern "C" void robodog_core_start_c(void);

void app_main() {
    ESP_LOGI(TAG, "RoboDog LVGL family-calendar client booting");

    if (!st7305_disp_init()) {
        ESP_LOGE(TAG, "ST7305 display init failed — halting");
        while (true) vTaskDelay(pdMS_TO_TICKS(1000));
    }


    if (!calendar_ui::init()) {
        ESP_LOGE(TAG, "calendar_ui init failed — halting");
        while (true) vTaskDelay(pdMS_TO_TICKS(1000));
    }

    // Calendar sync runs on its own task (WiFi + gRPC + NVS cache); it pokes
    // the LVGL subjects via set_events_data(). The LVGL pump stays here.
    robodog_core_start_c();

    for (;;) {
        lv_task_handler();
        vTaskDelay(pdMS_TO_TICKS(10));
    }
}
