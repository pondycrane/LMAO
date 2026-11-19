#include "button_led.h"

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "board_pins.h"
#include "led_rgb.h"

// Atom Lite front controls — board-dependent GPIOs (see board_pins.h).
//   classic (ESP32): button G39 (input-only, external pull-up, HIGH released /
//     LOW pressed), single SK6812 LED G27.
//   Atom Lite S3 (ESP32-S3): built-in button G41, 4x WS2812 LEDs G35.
static const char* TAG = "btn-led";

#define BTN_GPIO         BOARD_BTN_GPIO
#define BTN_PRESSED      0
#define LED_RMT_CHANNEL  0   // RMT channel owned by the LED (see lma_led)

// Button gesture thresholds (debounce-resilient).  Arming is a deliberate hold
// so a stray tap can never arm the pump; a quick tap always disarms.
#define DEBOUNCE_MS       60u       // shorter presses are ignored (bounce)
#define TAP_MAX_MS       1000u      // <= this => disarm
#define LONG_PRESS_MS    2000u      // >= this => arm
#define POLL_MS          10u

static volatile bool g_armed = false;
static volatile bool g_pending = false;

static void led_show_armed(void) {
    // red = actuation armed, dark = dry-run
    lma_led::set_all(g_armed ? lma_led::kRed : lma_led::kOff);
}

static void set_armed(bool armed) {
    if (g_armed == armed) return;
    g_armed = armed;
    g_pending = true;
    led_show_armed();
    ESP_LOGI(TAG, "actuation %s (button)", armed ? "ARMED" : "dry run");
}

static void button_task(void* arg) {
    (void)arg;
    bool last = true;               // released (external pull-up => HIGH)
    uint32_t press_start_ms = 0;
    for (;;) {
        const bool pressed = (gpio_get_level((gpio_num_t)BTN_GPIO) == BTN_PRESSED);
        if (pressed && !last) {
            press_start_ms = (uint32_t)(xTaskGetTickCount() * portTICK_PERIOD_MS);
        } else if (!pressed && last) {
            const uint32_t dur =
                (uint32_t)(xTaskGetTickCount() * portTICK_PERIOD_MS) - press_start_ms;
            if (dur >= LONG_PRESS_MS) {
                set_armed(true);
            } else if (dur >= DEBOUNCE_MS && dur <= TAP_MAX_MS) {
                set_armed(false);
            }
            // presses between TAP_MAX_MS and LONG_PRESS_MS are ignored.
        }
        last = pressed;
        vTaskDelay(pdMS_TO_TICKS(POLL_MS));
    }
}

void button_led_init(void) {
    // LED pin out, button in (pull-up config is accepted; the board provides
    // the external pull-up that GPIO39 actually needs).
    gpio_config_t in = {};
    in.pin_bit_mask = (1ULL << BTN_GPIO);
    in.mode = GPIO_MODE_INPUT;
    in.pull_up_en = GPIO_PULLUP_ENABLE;
    in.pull_down_en = GPIO_PULLDOWN_DISABLE;
    in.intr_type = GPIO_INTR_DISABLE;
    gpio_config(&in);

    // Shared SK6812 writer (firmware_common/lma_led); the button still works
    // if the RMT channel cannot be installed.
    if (!lma_led::init(BOARD_LED_GPIO, BOARD_LED_COUNT, LED_RMT_CHANNEL)) {
        ESP_LOGW(TAG, "LED unavailable (button still works)");
    }

    // Boot ALWAYS dry-run: armed default is already false, LED off.
    g_armed = false;
    g_pending = false;
    led_show_armed();

    if (xTaskCreate(button_task, "btn_led", 3072, NULL, 5, NULL) != pdPASS) {
        ESP_LOGE(TAG, "button task create failed");
    }
}

bool button_led_armed(void) { return g_armed; }

bool button_led_take_pending_change(void) {
    const bool p = g_pending;
    g_pending = false;
    return p;
}
