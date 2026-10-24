/*
 * RoboDog Phase 1 text-only RLCD chat UI (LMAO-owned).
 *
 * Deterministic display bring-up: esp-claw's own display_service / system_ui
 * are gated behind CONFIG_ESP_BOARD_DEV_DISPLAY_LCD_SUPPORT, which the
 * board-manager Kconfig silently drops for this custom-typed panel, so on a
 * normal boot nothing initialises the RLCD (zero flushes, zero RD_SHOT
 * frames).  This component instead:
 *   1. brings the custom RLCD panel up via the board manager
 *      (robodog_board_display_get, overlay setup_device.c),
 *   2. registers an LVGL display on it via esp_lv_adapter directly,
 *   3. renders the PR #278 text-only chat layout (status bar + transcript),
 *      and
 *   4. forces a synchronous full refresh after each text change so the
 *      panel flush -> RD_SHOT capture is deterministic (never the "no panel
 *      frame captured yet" flakiness of a purely event-driven flush).
 *
 * SPDX-License-Identifier: Apache-2.0
 */
#include "robodog_ui.h"

#include <stdio.h>
#include <string.h>

#include "esp_lv_adapter.h"
#include "esp_lv_adapter_display.h"
#include "esp_check.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "lvgl.h"

/* Declared in the overlay boards/waveshare.../setup_device.c (board device). */
esp_err_t robodog_board_display_get(esp_lcd_panel_handle_t *out_panel,
                                    esp_lcd_panel_io_handle_t *out_io);
/* Declared in robodog_phase0.c: force the RD_SHOT publisher to dump the
 * current frame immediately (bypasses the 4 s self-tick throttle). */
void robodog_rdshot_capture_now(void);

static const char *TAG = "robodog_ui";

#define UI_PANEL_W        400
#define UI_PANEL_H        300
#define UI_STATUS_H       32
#define UI_PAD            10

/* ASCII-only glyph set (parity with the bridge's display_text_ascii()): any
 * non-printable/complex codepoint is replaced so the ~14 px monochrome font
 * never mis-renders.  Multi-byte UTF-8 sequences are collapsed to '?'. */
#define UI_ASCII_REPLACE  '?'

typedef enum {
    UI_CMD_USER = 0,
    UI_CMD_ASSISTANT,
    UI_CMD_STATE,
} ui_cmd_type_t;

typedef struct {
    ui_cmd_type_t type;
    char text[480];
    char state[16];
} ui_cmd_t;

static QueueHandle_t s_ui_queue;
static TaskHandle_t s_ui_task;
static lv_display_t *s_disp;

static lv_obj_t *s_status_label;
static lv_obj_t *s_body_label;

static char s_status_text[96];
static char s_agent_state[16];
static char s_transcript[2048];      /* rolling conversation, newest last */

/* ── ASCII sanitisation ──────────────────────────────────────────────────── */

static void ui_ascii_copy(char *dst, size_t dst_size, const char *src)
{
    size_t d = 0;
    const char *s = src;
    while (s && *s && d + 1 < dst_size) {
        unsigned char c = (unsigned char)*s;
        if (c < 0x80 && (c >= 0x20 || c == '\n')) {
            dst[d++] = (char)c;
            s++;
        } else {
            /* Collapse each leading byte of a UTF-8 sequence to a single '?'. */
            size_t span = 1;
            if (c >= 0x80) {
                unsigned char lead = c;
                if ((lead & 0xE0) == 0xC0) span = 2;
                else if ((lead & 0xF0) == 0xE0) span = 3;
                else if ((lead & 0xF8) == 0xF0) span = 4;
            }
            dst[d++] = UI_ASCII_REPLACE;
            s += span;
        }
    }
    dst[d] = '\0';
}

/* ── Status bar ──────────────────────────────────────────────────────────── */

static void ui_refresh_status_locked(void)
{
    /* WiFi state read straight from the STA netif (no cap dependency). */
    esp_netif_t *sta = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
    const bool up = sta != NULL && esp_netif_is_netif_up(sta);

    const char *agent = s_agent_state[0] ? s_agent_state : "idle";
    snprintf(s_status_text, sizeof(s_status_text), "WIFI %s | %s",
             up ? "ON" : "OFF", agent);
    if (s_status_label != NULL) {
        lv_label_set_text(s_status_label, s_status_text);
    }
}

/* ── Transcript rolling buffer ───────────────────────────────────────────── */

static void ui_append_line(const char *prefix, const char *text)
{
    const size_t cap = sizeof(s_transcript) - 1; /* keep space for NUL */
    size_t len = strlen(s_transcript);

    /* Format "<prefix> <text>\n", ASCII-sanitized on the fly by prefixing. */
    char ascii[480];
    ui_ascii_copy(ascii, sizeof(ascii), text);
    char rendering[512];
    if (prefix && prefix[0]) {
        snprintf(rendering, sizeof(rendering), "%s %s\n", prefix, ascii);
    } else {
        snprintf(rendering, sizeof(rendering), "%s\n", ascii);
    }
    const size_t rlen = strlen(rendering);

    /* Drop the OLDEST messages until the newest fits: keeps the recent
     * exchange visible (matches the stale-caption intent) without needing
     * LVGL scroll management in Phase 1. */
    while (rlen >= cap || len + rlen > cap) {
        char *nl = strchr(s_transcript, '\n');
        if (nl == NULL) {
            s_transcript[0] = '\0';
            len = 0;
            break;
        }
        memmove(s_transcript, nl + 1, (len - (size_t)(nl - s_transcript) - 1) + 1);
        len = strlen(s_transcript);
        if (len == 0) {
            break;
        }
    }
    strlcat(s_transcript, rendering, sizeof(s_transcript));
}

/* ── LVGL screen construction ───────────────────────────────────────────── */

static esp_err_t ui_build_screen(void)
{
    lv_obj_t *scr = lv_obj_create(NULL);
    ESP_RETURN_ON_FALSE(scr != NULL, ESP_ERR_NO_MEM, TAG, "create screen failed");
    lv_obj_set_size(scr, UI_PANEL_W, UI_PANEL_H);
    lv_obj_set_style_pad_all(scr, 0, 0);
    lv_obj_set_style_border_width(scr, 0, 0);
    /* Reflective panel: white = paper.  A dark default theme would render the
     * whole screen black, so force the background to white (text is drawn
     * dark on top, exactly the PR #278 monochrome look). */
    lv_obj_set_style_bg_color(scr, lv_color_white(), 0);
    lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);
    lv_obj_clear_flag(scr, LV_OBJ_FLAG_SCROLLABLE);

    /* Status bar (top): WiFi + agent state. */
    s_status_label = lv_label_create(scr);
    ESP_RETURN_ON_FALSE(s_status_label != NULL, ESP_ERR_NO_MEM, TAG,
                        "create status label failed");
    lv_obj_set_pos(s_status_label, UI_PAD, 4);
    lv_obj_set_width(s_status_label, UI_PANEL_W - 2 * UI_PAD);
    lv_obj_set_style_text_align(s_status_label, LV_TEXT_ALIGN_LEFT, 0);
    lv_obj_set_style_text_font(s_status_label, &lv_font_montserrat_14, 0);
    ui_refresh_status_locked();

    /* Separator under the status bar. */
    lv_obj_t *rule = lv_obj_create(scr);
    ESP_RETURN_ON_FALSE(rule != NULL, ESP_ERR_NO_MEM, TAG, "create rule failed");
    lv_obj_set_pos(rule, 0, UI_STATUS_H - 2);
    lv_obj_set_size(rule, UI_PANEL_W, 1);
    lv_obj_set_style_bg_color(rule, lv_color_black(), 0);
    lv_obj_set_style_border_width(rule, 0, 0);
    lv_obj_clear_flag(rule, LV_OBJ_FLAG_SCROLLABLE);

    /* Conversation body: full-panel width, multiline wrap, left aligned. */
    s_body_label = lv_label_create(scr);
    ESP_RETURN_ON_FALSE(s_body_label != NULL, ESP_ERR_NO_MEM, TAG,
                        "create body label failed");
    lv_obj_set_pos(s_body_label, UI_PAD, UI_STATUS_H);
    lv_obj_set_width(s_body_label, UI_PANEL_W - 2 * UI_PAD);
    lv_obj_set_style_text_align(s_body_label, LV_TEXT_ALIGN_LEFT, 0);
    lv_obj_set_style_text_font(s_body_label, &lv_font_montserrat_14, 0);
    lv_obj_set_style_text_line_space(s_body_label, 2, 0);
    lv_label_set_long_mode(s_body_label, LV_LABEL_LONG_WRAP);
    lv_label_set_text(s_body_label, s_transcript);

    /* LVGL 9.5 public API: load the screen on the (already-default) display. */
    lv_screen_load(scr);
    return ESP_OK;
}

/* ── UI task ─────────────────────────────────────────────────────────────── */

static void ui_flush_locked(void)
{
    /* lv_label_set_text copies into the label's own buffer, so always refresh
     * the body to the latest transcript; LVGL only flushes a DIRTY region, so
     * on a fresh boot with a static/empty screen it would never flush (and the
     * RD_SHOT publisher would have no retained frame) - explicitly invalidate
     * the labels so even a no-op state change forces a redraw -> panel flush.
     * This makes ANY UI_CMD (including the very first boot render) produce a
     * deterministic RD_SHOT frame, not just text changes. */
    lv_label_set_text(s_body_label, s_transcript);
    lv_obj_invalidate(s_body_label);
    if (lv_label_get_text(s_status_label) != s_status_text) {
        lv_label_set_text(s_status_label, s_status_text);
    }
    lv_obj_invalidate(s_status_label);
    /* Synchronous full refresh: drives LVGL render -> panel flush ->
     * robodog_rdshot_publish (frame captured every time, never event-flaky),
     * then force the RD_SHOT dump so a capture right now reads the fresh
     * frame. */
    esp_lv_adapter_refresh_now(s_disp);
    robodog_rdshot_capture_now();
}

static void ui_process_cmd(const ui_cmd_t *cmd)
{
    if (esp_lv_adapter_lock(1000) != ESP_OK) {
        ESP_LOGW(TAG, "LVGL lock timeout, dropping ui cmd");
        return;
    }
    switch (cmd->type) {
    case UI_CMD_USER:
        ui_append_line(">", cmd->text);
        /* A fresh user turn starts: mark the agent as reasoning until the
         * reply finalizes (this is the "thinking" status-bar state). */
        strlcpy(s_agent_state, "thinking", sizeof(s_agent_state));
        break;
    case UI_CMD_ASSISTANT:
        ui_append_line("", cmd->text);
        strlcpy(s_agent_state, "idle", sizeof(s_agent_state));
        break;
    case UI_CMD_STATE:
        ui_ascii_copy(s_agent_state, sizeof(s_agent_state), cmd->state);
        break;
    }
    ui_refresh_status_locked();
    ui_flush_locked();
    esp_lv_adapter_unlock();
}

static void ui_task(void *arg)
{
    (void)arg;
    esp_err_t err;

    esp_lcd_panel_handle_t panel = NULL;
    esp_lcd_panel_io_handle_t io = NULL;
    err = robodog_board_display_get(&panel, &io);
    if (err != ESP_OK || panel == NULL) {
        ESP_LOGE(TAG, "RLCD panel bring-up failed: %s (no text UI)",
                 esp_err_to_name(err));
        vTaskDelete(NULL);
        return;
    }
    ESP_LOGI(TAG, "RLCD panel ready (%d x %d), driver LVGL adapter",
             UI_PANEL_W, UI_PANEL_H);

    esp_lv_adapter_config_t adapter_cfg = ESP_LV_ADAPTER_DEFAULT_CONFIG();
    adapter_cfg.task_stack_size = 8192;
    adapter_cfg.task_priority = 5;
    err = esp_lv_adapter_init(&adapter_cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "LVGL adapter init failed: %s", esp_err_to_name(err));
        vTaskDelete(NULL);
        return;
    }

    esp_lv_adapter_display_config_t disp_cfg =
        ESP_LV_ADAPTER_DISPLAY_SPI_WITH_PSRAM_DEFAULT_CONFIG(
            panel, io, UI_PANEL_W, UI_PANEL_H, ESP_LV_ADAPTER_ROTATE_0);
    if (esp_lv_adapter_lock(1000) != ESP_OK) {
        ESP_LOGE(TAG, "LVGL lock timeout during init");
        vTaskDelete(NULL);
        return;
    }
    s_disp = esp_lv_adapter_register_display(&disp_cfg);
    if (s_disp != NULL) {
        lv_display_set_default(s_disp);
    }
    if (s_disp == NULL || ui_build_screen() != ESP_OK) {
        esp_lv_adapter_unlock();
        ESP_LOGE(TAG, "LVGL display/screen setup failed");
        vTaskDelete(NULL);
        return;
    }
    esp_lv_adapter_unlock();
    esp_lv_adapter_start();

    ESP_LOGI(TAG, "text-only UI up");
    /* Seed the first panel render so a fresh-boot RD_SHOT capture always has
     * a frame: lv_screen_load defers the actual screen switch to the LVGL
     * adapter task's next tick, so an invalidate + synchronous refresh only
     * drives a real panel flush once the screen is active.  Retry a few times
     * so timing never leaves a fresh boot without a published frame (a static
     * screen otherwise never flushes -> the RD_SHOT "no panel frame" gap). */
    for (int i = 0; i < 5; i++) {
        vTaskDelay(pdMS_TO_TICKS(300));
        ui_process_cmd(&(ui_cmd_t){.type = UI_CMD_STATE, .state = "idle"});
    }

    for (;;) {
        ui_cmd_t cmd;
        if (xQueueReceive(s_ui_queue, &cmd, portMAX_DELAY) == pdTRUE) {
            ui_process_cmd(&cmd);
        }
    }
}

static void ui_enqueue(ui_cmd_type_t type, const char *text, const char *state)
{
    ui_cmd_t cmd = {
        .type = type,
        .text = {0},
        .state = {0},
    };
    if (text != NULL) {
        strlcpy(cmd.text, text, sizeof(cmd.text));
    }
    if (state != NULL) {
        strlcpy(cmd.state, state, sizeof(cmd.state));
    }
    if (s_ui_queue != NULL) {
        /* Non-blocking: a full queue means LVGL is backed up; drop the event
         * rather than stall the caller (the transcript is best-effort). */
        xQueueSend(s_ui_queue, &cmd, 0);
    }
}

/* ── public API ──────────────────────────────────────────────────────────── */

esp_err_t robodog_ui_start(void)
{
    if (s_ui_task != NULL) {
        return ESP_OK;
    }
    s_ui_queue = xQueueCreate(16, sizeof(ui_cmd_t));
    ESP_RETURN_ON_FALSE(s_ui_queue != NULL, ESP_ERR_NO_MEM, TAG,
                        "create UI queue failed");
    if (xTaskCreate(ui_task, "robodog_ui", 8192, NULL, 4, &s_ui_task) != pdPASS) {
        ESP_LOGE(TAG, "create UI task failed");
        vQueueDelete(s_ui_queue);
        s_ui_queue = NULL;
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

void robodog_ui_show_user(const char *text)
{
    ui_enqueue(UI_CMD_USER, text, NULL);
}

void robodog_ui_show_assistant(const char *text)
{
    ui_enqueue(UI_CMD_ASSISTANT, text, NULL);
}

void robodog_ui_set_agent_state(const char *state)
{
    ui_enqueue(UI_CMD_STATE, NULL, state);
}
