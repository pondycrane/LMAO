/*
 * robodog.c -- the RoboDog application loop.
 *
 * Per cycle:
 *   1. join the family WiFi (rd_net)
 *   2. framed-TCP calendar sync with the LMAO server (:4247, rd_net)
 *   3. merge the delta into the NVS-persisted cache (rd_store)
 *   4. render the 400x300 landscape panel (rd_render)
 *   5. hold the frame (no light-sleep: the reflective ST7305 loses its image
 *      in sleep), redrawing every 30 s so the clock stays live, then repeat
 *
 * The pieces this file used to contain now live in their own units:
 *   rd_store.c   NVS config + cache (schema-versioned)
 *   rd_net.c     WiFi + framed-TCP transport (separate connect/read budgets)
 *   rd_render.c  ST7305 render + RD_SHOT capture
 *   rd_layout.c  panel geometry fit arithmetic (host-tested)
 *   rd_clip.c / rd_datetime.c / rd_weather.c  pure formatting (host-tested)
 *
 * A task watchdog is armed here: the hold loop feeds it every chunk, so a
 * wedged sync (blocked recv, stuck DNS) reboots the board instead of leaving
 * a frozen panel until someone power-cycles it.
 */
#include <string.h>

#include "esp_err.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_task_wdt.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs.h"
#include "nvs_flash.h"

#include "config.h"
#include "lma_proto.h"
#include "rd_net.h"
#include "rd_render.h"
#include "rd_store.h"

static const char *TAG = "robodog";

/* Runtime state owned by the loop (the units take it as arguments). */
static rd_config_t s_cfg;
static rd_cache_t s_cache;
static char s_wm_kid[32];   /* kid the persisted cache/watermark belong to */

/* Conditions from the last reply.  RAM only: stale weather is worse than
 * none, so it is never persisted and never re-rendered from a boot cache. */
static lma_weather_t s_current_wx;

/* Persisted owner of the cache: a kid-assignment change must never render the
 * previous kid's rows in the pre-sync boot frame. */
static void wm_kid_load(void)
{
    s_wm_kid[0] = '\0';
    nvs_handle_t h;
    if (nvs_open(RD_NVS_NAMESPACE, NVS_READONLY, &h) == ESP_OK) {
        size_t sz = sizeof(s_wm_kid);
        (void)nvs_get_str(h, RD_NVS_KEY_WM_KID, s_wm_kid, &sz);
        nvs_close(h);
    }
}

static void wm_kid_save(void)
{
    nvs_handle_t h;
    if (nvs_open(RD_NVS_NAMESPACE, NVS_READWRITE, &h) != ESP_OK) {
        return;
    }
    nvs_set_str(h, RD_NVS_KEY_WM_KID, s_cfg.kid_name);
    nvs_commit(h);
    nvs_close(h);
}

static void nvs_boot(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);
}

static void hold_until_next_sync(rd_state_t state)
{
    rd_net_disconnect();
    /* Reflective ST7305 loses its image when the ESP32 light-sleeps the
     * display power ("screen went blank"), so stay active and let the RLCD
     * keep the last rendered frame visible.  Redraw every 30 s so the header
     * clock stays live (it advances via esp_timer). */
    ESP_LOGI(TAG, "holding screen %u s (no light-sleep)",
             (unsigned)RD_SYNC_INTERVAL_SECONDS);
    uint32_t left_ms = (uint32_t)RD_SYNC_INTERVAL_SECONDS * 1000u;
    while (left_ms > 0) {
        uint32_t chunk = left_ms > 30000u ? 30000u : left_ms;
        vTaskDelay(pdMS_TO_TICKS(chunk));
        left_ms -= chunk;
        esp_task_wdt_reset();
        rd_render_screen(state, &s_cfg, &s_current_wx, &s_cache);
    }
}

/* The decoded reply is ~13 KB (16 events at their text caps + the 176x176
 * tile), far beyond the 8 KB main-task stack: it lives in static storage, like
 * the transport's frame buffer. */
static lma_cal_bundle_t s_bundle;

static bool sync_once(rd_state_t *state)
{
    lma_cal_request_t req;
    lma_proto_req_init(&req);
    snprintf(req.calendar_id, sizeof(req.calendar_id), "%s", RD_CALENDAR_ID);
    req.since_ms = s_cache.watermark_ms;   /* 0 -> full fetch */
    if (s_cfg.kid_name[0] != '\0') {
        /* kid panel: the server filters to this kid's events */
        snprintf(req.participant, sizeof(req.participant), "%s",
                 s_cfg.kid_name);
    }
    const bool is_full = (req.since_ms == 0);

    if (!rd_net_sync(&s_cfg, &req, &s_bundle)) {
        *state = RD_STATE_STALE;
        return false;
    }
    rd_render_set_clock(s_bundle.watermark_ms);   /* server epoch-ms clock */
    if (is_full) {
        /* A full fetch is the server's complete truth: the server sends live
         * events only, so drop cached events it no longer contains. */
        s_cache.count = 0;
    }
    rd_cache_apply(&s_cache, &s_bundle);
    s_current_wx = s_bundle.current;
    *state = RD_STATE_FRESH;
    return true;
}

void robodog_app_main(void)
{
    nvs_boot();
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());

    rd_config_load(&s_cfg);
    wm_kid_load();
    rd_cache_load(&s_cache);

    /* Delta-safety: the persisted cache/watermark are owned by the kid the
     * board was last synced under.  On a kid-assignment change (different kid,
     * or set<->family) the previous kid's rows must NEVER render pre-sync. */
    if (strcmp(s_cfg.kid_name, s_wm_kid) != 0) {
        ESP_LOGW(TAG, "kid assignment change: '%s' -> '%s': cleared cache, "
                      "full refetch", s_wm_kid, s_cfg.kid_name);
        rd_cache_clear(&s_cache);
        wm_kid_save();
    }

    /* Full fetch on every boot: the NVS cache stores no picture (the 24 KB
     * partition cannot hold tiles), so each boot re-serves the headline
     * drawing into RAM.  The calendar is small; this is one reply per boot. */
    s_cache.watermark_ms = 0;

    if (!rd_net_init()) {
        ESP_LOGE(TAG, "wifi init failed");
    }
    if (!rd_render_init()) {
        ESP_LOGE(TAG, "display init failed");
    }

    /* Task watchdog: the main task is the only long-running one, and a wedged
     * network call must reboot the board rather than freeze the panel. */
    const esp_task_wdt_config_t wdt_cfg = {
        .timeout_ms = RD_WDT_TIMEOUT_S * 1000u,
        .idle_core_mask = 0,
        .trigger_panic = true,
    };
    /* IDF initialises the TWDT at startup by default, so reconfigure first and
     * only init when it is genuinely absent (calling init on an initialised
     * watchdog just logs an error and leaves the default timeout in place). */
    if (esp_task_wdt_reconfigure(&wdt_cfg) != ESP_OK) {
        esp_task_wdt_init(&wdt_cfg);
    }
    esp_task_wdt_add(NULL);

    rd_render_screen(RD_STATE_OFFLINE, &s_cfg, &s_current_wx, &s_cache);
    ESP_LOGI(TAG, "booted: %u cached events, watermark=%llu, server=%s "
                  "(main stack high-water %u B free)",
             (unsigned)s_cache.count,
             (unsigned long long)s_cache.watermark_ms, s_cfg.server_host,
             (unsigned)uxTaskGetStackHighWaterMark(NULL));

    for (;;) {
        rd_state_t state = RD_STATE_OFFLINE;
        esp_task_wdt_reset();
        if (rd_net_connect(&s_cfg)) {
            esp_task_wdt_reset();
            sync_once(&state);
        }
        rd_render_screen(state, &s_cfg, &s_current_wx, &s_cache);
        ESP_LOGI(TAG, "cycle done (state=%d), main stack high-water %u B free",
                 (int)state, (unsigned)uxTaskGetStackHighWaterMark(NULL));
        hold_until_next_sync(state);
    }
}
