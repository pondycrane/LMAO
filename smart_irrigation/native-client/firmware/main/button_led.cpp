#include "button_led.h"

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/gpio.h"
#include "driver/rmt.h"
#include "esp_log.h"
#include "board_pins.h"

// Atom Lite front controls — board-dependent GPIOs (see board_pins.h).
//   classic (ESP32): button G39 (input-only, external pull-up, HIGH released /
//     LOW pressed), single SK6812 LED G27.
//   Atom Lite S3 (ESP32-S3): built-in button G41, 4x WS2812 LEDs G35.
static const char* TAG = "btn-led";

#define BTN_GPIO         BOARD_BTN_GPIO
#define BTN_PRESSED      0
#define LED_GPIO         BOARD_LED_GPIO
#define LED_RMT_CHANNEL  RMT_CHANNEL_0
#define RMT_CLK_DIV      8          // APB 80 MHz / 8 = 0.1 us per tick

// Button gesture thresholds (debounce-resilient).  Arming is a deliberate hold
// so a stray tap can never arm the pump; a quick tap always disarms.
#define DEBOUNCE_MS       60u       // shorter presses are ignored (bounce)
#define TAP_MAX_MS       1000u      // <= this => disarm
#define LONG_PRESS_MS    2000u      // >= this => arm
#define POLL_MS          10u

// ─── SK6812 / WS2812 timing (0.1 us ticks): 1 = 0.9 us H + 0.3 us L,
//     0 = 0.3 us H + 0.9 us L, reset >= 80 us.  Pixel order is GRB.
#define T1H 9
#define T1L 3
#define T0H 3
#define T0L 9
#define RESET_TICKS 1000u           // 100 us reset

static volatile bool g_armed = false;
static volatile bool g_pending = false;

static void led_write_rgb(uint8_t r, uint8_t g, uint8_t b) {
    rmt_item32_t items[24 + 1];
    for (int i = 0; i < 24; ++i) {
        const int byte = i / 8;
        const int bit  = 7 - (i % 8);
        uint8_t  val;
        if (byte == 0)      val = (g >> bit) & 1u;   // GRB: green first
        else if (byte == 1) val = (r >> bit) & 1u;   // red
        else                val = (b >> bit) & 1u;   // blue
        items[i].level0 = 1;
        items[i].duration0 = val ? T1H : T0H;
        items[i].level1 = 0;
        items[i].duration1 = val ? T1L : T0L;
    }
    items[24].level0 = 0;
    items[24].duration0 = 0;
    items[24].level1 = 0;
    items[24].duration1 = RESET_TICKS;
    // Guard against an already-in-flight frame; blocks up to one frame anyway.
    rmt_write_items(LED_RMT_CHANNEL, items, 25, true);
    rmt_wait_tx_done(LED_RMT_CHANNEL, portMAX_DELAY);
}

static void led_show_armed(void) {
    led_write_rgb(g_armed ? 255 : 0, 0, 0);   // red = actuation armed, off = dry-run
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

    gpio_config_t out = {};
    out.pin_bit_mask = (1ULL << LED_GPIO);
    out.mode = GPIO_MODE_OUTPUT;
    out.pull_up_en = GPIO_PULLUP_DISABLE;
    out.pull_down_en = GPIO_PULLDOWN_DISABLE;
    out.intr_type = GPIO_INTR_DISABLE;
    gpio_config(&out);

    rmt_config_t cfg = RMT_DEFAULT_CONFIG_TX((gpio_num_t)LED_GPIO, LED_RMT_CHANNEL);
    cfg.clk_div = RMT_CLK_DIV;
    if (rmt_config(&cfg) != ESP_OK || rmt_driver_install(cfg.channel, 0, 0) != ESP_OK) {
        ESP_LOGW(TAG, "RMT init failed — LED unavailable (button still works)");
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
