/*
 * robodog.c -- native ESP-IDF firmware for the Waveshare ESP32-S3 RLCD-4.2
 * (ST7305 400x300 reflective LCD) family-calendar display, "RoboDog".
 *
 * Flow per cycle:
 *   1. connect STA WiFi ("robotdog")
 *   2. best-effort SNTP (header date only)
 *   3. framed-TCP calendar sync with the LMAO server (lma_core/calendar_tcp_api.py)
 *   4. merge the delta into the NVS-persisted event cache (union by uid,
 *      tombstones remove), advance the watermark
 *   5. render 1-bit rows on the ST7305 via the vendored u8g2_st7305 driver
 *   6. stop WiFi and light-sleep for SYNC_INTERVAL_SECONDS, then repeat
 *
 * Wire format (both directions), payloads are raw protobuf (see lma_proto.c):
 *   [u32 BE length][payload bytes]
 *   request  = CalendarRequest  (empty payload == server defaults "family"/0)
 *   response = CalendarBundle
 */
#include <string.h>
#include <stdio.h>
#include <time.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "esp_log.h"
#include "esp_err.h"
#include "esp_event.h"
#include "esp_wifi.h"
#include "esp_netif.h"
#include "esp_sntp.h"
#include "esp_sleep.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "lwip/sockets.h"
#include "lwip/netdb.h"

#include "config.h"
#include "lma_proto.h"
#include "u8g2_st7305.h"

static const char *TAG = "robodog";

/* ------------------------------------------------------------------ state */

#define RD_WIFI_GOT_IP ((EventBits_t)1u << 0)
static EventGroupHandle_t s_wifi_evt;
static bool s_wifi_started = false;

static char s_wifi_ssid[33];
static char s_wifi_pass[65];
static char s_server_host[65];
static uint64_t s_watermark = 0;

static lma_cal_event_t s_cache[LMA_PB_EVENT_MAX];
static size_t s_cache_count = 0;

/* Frame/bundle buffers are static: a bundle can be ~8 KiB, main-task stacks
 * are far smaller. */
static uint8_t s_frame[LMA_PB_MAX_FRAME];
static lma_cal_bundle_t s_bundle;

#define NVS_NS "robodog"
#define NVS_KEY_WM "wm"
#define NVS_KEY_EC "ec"

static char s_cachekey[12];

static const char *cache_key(size_t i)
{
    snprintf(s_cachekey, sizeof(s_cachekey), "e%u", (unsigned)i);
    return s_cachekey;
}

/* --------------------------------------------------------------- config/NVS */

static void config_load(void)
{
    nvs_handle_t h;
    const char *defs[3];
    char *dsts[3];
    size_t caps[3];
    const char *keys[3] = {"wifi_ssid", "wifi_pass", "server_host"};
    defs[0] = RD_WIFI_SSID_DEFAULT;
    defs[1] = RD_WIFI_PASS_DEFAULT;
    defs[2] = RD_SERVER_HOST_DEFAULT;
    dsts[0] = s_wifi_ssid;
    dsts[1] = s_wifi_pass;
    dsts[2] = s_server_host;
    caps[0] = sizeof(s_wifi_ssid);
    caps[1] = sizeof(s_wifi_pass);
    caps[2] = sizeof(s_server_host);

    for (int i = 0; i < 3; i++) {
        snprintf(dsts[i], caps[i], "%s", defs[i]);
    }
    if (nvs_open(NVS_NS, NVS_READONLY, &h) == ESP_OK) {
        for (int i = 0; i < 3; i++) {
            size_t sz = caps[i];
            if (nvs_get_str(h, keys[i], dsts[i], &sz) != ESP_OK) {
                snprintf(dsts[i], caps[i], "%s", defs[i]);
            }
        }
        nvs_close(h);
    }
}

static void nvs_open_rw(nvs_handle_t *h)
{
    *h = 0;
    if (nvs_open(NVS_NS, NVS_READWRITE, h) != ESP_OK) {
        *h = 0;
    }
}

static void watermark_load(void)
{
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) == ESP_OK) {
        nvs_get_u64(h, NVS_KEY_WM, &s_watermark); /* NOT_FOUND -> 0 is fine */
        nvs_close(h);
    }
}

static void watermark_save(void)
{
    nvs_handle_t h;
    nvs_open_rw(&h);
    if (h != 0) {
        nvs_set_u64(h, NVS_KEY_WM, s_watermark);
        nvs_commit(h);
        nvs_close(h);
    }
}

static void cache_load(void)
{
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) != ESP_OK) {
        return;
    }
    uint8_t cnt = 0;
    if (nvs_get_u8(h, NVS_KEY_EC, &cnt) == ESP_OK) {
        for (size_t i = 0; i < cnt && i < LMA_PB_EVENT_MAX; i++) {
            size_t sz = sizeof(s_cache[i]);
            if (nvs_get_blob(h, cache_key(i), &s_cache[i], &sz) != ESP_OK) {
                continue;
            }
            s_cache_count++;
        }
    }
    nvs_close(h);
}

static void cache_persist(void)
{
    nvs_handle_t h;
    nvs_open_rw(&h);
    if (h == 0) {
        return;
    }
    nvs_set_u8(h, NVS_KEY_EC, (uint8_t)s_cache_count);
    for (size_t i = 0; i < s_cache_count; i++) {
        nvs_set_blob(h, cache_key(i), &s_cache[i], sizeof(s_cache[i]));
    }
    for (size_t i = s_cache_count; i < LMA_PB_EVENT_MAX; i++) {
        nvs_erase_key(h, cache_key(i)); /* may be NOT_FOUND -> ignored */
    }
    nvs_commit(h);
    nvs_close(h);
}

/* -------------------------------------------------------------- cache merge */

static int cache_find_by_uid(const char *uid)
{
    for (size_t i = 0; i < s_cache_count; i++) {
        if (strcmp(s_cache[i].uid, uid) == 0) {
            return (int)i;
        }
    }
    return -1;
}

static void cache_remove(size_t idx)
{
    if (idx + 1 < s_cache_count) {
        memmove(&s_cache[idx], &s_cache[idx + 1],
                (s_cache_count - idx - 1) * sizeof(s_cache[0]));
    }
    s_cache_count--;
}

/* Rising start_ms so the calendar renders in chronological order. */
static void cache_sort_by_start(void)
{
    for (size_t i = 1; i < s_cache_count; i++) {
        lma_cal_event_t v = s_cache[i];
        size_t j = i;
        while (j > 0 && s_cache[j - 1].start_ms > v.start_ms) {
            s_cache[j] = s_cache[j - 1];
            j--;
        }
        s_cache[j] = v;
    }
}

static void cache_apply(const lma_cal_bundle_t *bundle)
{
    if (bundle->format != 0 && bundle->format != 1) {
        ESP_LOGW(TAG, "bundle format=%u (expected 1)", (unsigned)bundle->format);
    }
    size_t applied = 0;
    for (size_t i = 0; i < bundle->event_count; i++) {
        const lma_cal_event_t *ev = &bundle->events[i];
        int idx = cache_find_by_uid(ev->uid);
        if (ev->deleted) {
            if (idx >= 0) {
                cache_remove((size_t)idx);
                applied++;
            }
        } else if (idx >= 0) {
            s_cache[idx] = *ev;
            applied++;
        } else if (s_cache_count < LMA_PB_EVENT_MAX) {
            s_cache[s_cache_count++] = *ev;
            applied++;
        } else {
            ESP_LOGW(TAG, "cache full, dropping event uid=%s", ev->uid);
        }
    }
    if (bundle->watermark_ms > s_watermark) {
        s_watermark = bundle->watermark_ms;
    }
    cache_sort_by_start();
    cache_persist();
    watermark_save();
    ESP_LOGI(TAG, "applied %u delta events, cache=%u, watermark=%llu",
             (unsigned)applied, (unsigned)s_cache_count,
             (unsigned long long)s_watermark);
}

/* ------------------------------------------------------------------- WiFi */

static void wifi_event_handler(void *arg, esp_event_base_t base, int32_t id,
                               void *data)
{
    (void)arg;
    (void)data;
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        if (s_wifi_started) {
            ESP_LOGW(TAG, "wifi disconnected, retrying");
            esp_wifi_connect();
        }
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ESP_LOGI(TAG, "connected, got ip");
        xEventGroupSetBits(s_wifi_evt, RD_WIFI_GOT_IP);
    }
}

static void wifi_init(void)
{
    s_wifi_evt = xEventGroupCreate();

    ESP_ERROR_CHECK(esp_netif_create_default_wifi_sta());

    wifi_init_config_t wic = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&wic));

    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event_handler, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event_handler, NULL, NULL));

    wifi_config_t wc = {0};
    strncpy((char *)wc.sta.ssid, s_wifi_ssid, sizeof(wc.sta.ssid) - 1);
    strncpy((char *)wc.sta.password, s_wifi_pass, sizeof(wc.sta.password) - 1);
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));
}

static bool wifi_connect(void)
{
    if (!s_wifi_started) {
        ESP_ERROR_CHECK(esp_wifi_start());
        s_wifi_started = true;
    }
    xEventGroupClearBits(s_wifi_evt, RD_WIFI_GOT_IP);
    bool ok = (xEventGroupWaitBits(s_wifi_evt, RD_WIFI_GOT_IP, pdFALSE, pdFALSE,
                                   pdMS_TO_TICKS(RD_WIFI_CONNECT_TIMEOUT_S * 1000)) &
               RD_WIFI_GOT_IP) != 0;
    if (!ok) {
        ESP_LOGW(TAG, "wifi connect timeout after %u s",
                 (unsigned)RD_WIFI_CONNECT_TIMEOUT_S);
        s_wifi_started = false;
        esp_wifi_stop();
    }
    return ok;
}

/* -------------------------------------------------------------------- SNTP */

static void sntp_sync(void)
{
#if RD_SNTP_SERVER[0] != '\0'
    if (esp_sntp_get_sync_status() == SNTP_SYNC_STATUS_COMPLETED) {
        return;
    }
    esp_sntp_setoperatingmode(SNTP_OPMODE_POLL);
    esp_sntp_setservername(0, RD_SNTP_SERVER);
    esp_sntp_init();
    uint32_t waited = 0;
    while (waited < RD_SNTP_WAIT_MAX_MS &&
           esp_sntp_get_sync_status() != SNTP_SYNC_STATUS_COMPLETED) {
        vTaskDelay(pdMS_TO_TICKS(100));
        waited += 100;
    }
    if (esp_sntp_get_sync_status() == SNTP_SYNC_STATUS_COMPLETED) {
        ESP_LOGI(TAG, "time synced via %s", RD_SNTP_SERVER);
    }
#endif
}

/* ----------------------------------------------------------- TCP calendar */

static bool send_all(int fd, const uint8_t *p, size_t n)
{
    while (n > 0) {
        ssize_t w = send(fd, p, n, 0);
        if (w <= 0) {
            return false;
        }
        p += w;
        n -= (size_t)w;
    }
    return true;
}

static bool recv_exact(int fd, uint8_t *p, size_t n)
{
    while (n > 0) {
        ssize_t r = recv(fd, p, n, 0);
        if (r <= 0) { /* timeout (SO_RCVTIMEO) or peer closed */
            return false;
        }
        p += r;
        n -= (size_t)r;
    }
    return true;
}

static bool tcp_sync(void)
{
    char port[8];
    snprintf(port, sizeof(port), "%u", RD_CAL_TCP_PORT);

    struct addrinfo hints;
    memset(&hints, 0, sizeof(hints));
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_STREAM;
    struct addrinfo *res = NULL;
    if (getaddrinfo(s_server_host, port, &hints, &res) != 0 || res == NULL) {
        ESP_LOGE(TAG, "cannot resolve server host '%s'", s_server_host);
        return false;
    }

    int fd = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (fd < 0) {
        freeaddrinfo(res);
        return false;
    }

    struct timeval tv;
    tv.tv_sec = RD_TCP_CONNECT_TIMEOUT_MS / 1000;
    tv.tv_usec = (RD_TCP_CONNECT_TIMEOUT_MS % 1000) * 1000;
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

    bool ok = false;
    if (connect(fd, res->ai_addr, res->ai_addrlen) != 0) {
        ESP_LOGW(TAG, "tcp connect to %s:%s failed", s_server_host, port);
        goto out;
    }

    lma_cal_request_t req;
    lma_proto_req_init(&req);
    snprintf(req.calendar_id, sizeof(req.calendar_id), "%s", RD_CALENDAR_ID);
    req.since_ms = s_watermark; /* 0 -> full fetch on first boot */
    /* max_events left 0 -> server default page cap */

    int plen = lma_proto_req_encode(&req, s_frame + 4, sizeof(s_frame) - 4);
    if (plen < 0) {
        ESP_LOGE(TAG, "request does not fit frame buffer");
        goto out;
    }

    uint32_t n = (uint32_t)plen;
    s_frame[0] = (uint8_t)(n >> 24);
    s_frame[1] = (uint8_t)(n >> 16);
    s_frame[2] = (uint8_t)(n >> 8);
    s_frame[3] = (uint8_t)n;

    if (!send_all(fd, s_frame, (size_t)4 + (size_t)plen)) {
        ESP_LOGW(TAG, "send request failed");
        goto out;
    }

    uint8_t hdr[4];
    if (!recv_exact(fd, hdr, 4)) {
        ESP_LOGW(TAG, "no reply frame header (timeout %u ms)",
                 (unsigned)RD_TCP_FRAME_TIMEOUT_MS);
        goto out;
    }
    n = ((uint32_t)hdr[0] << 24) | ((uint32_t)hdr[1] << 16) |
        ((uint32_t)hdr[2] << 8) | (uint32_t)hdr[3];
    if (n > sizeof(s_frame)) {
        ESP_LOGW(TAG, "reply frame too large (%u bytes)", (unsigned)n);
        goto out;
    }
    if (!recv_exact(fd, s_frame, n)) {
        ESP_LOGW(TAG, "short reply payload");
        goto out;
    }
    if (!lma_proto_bundle_decode(s_frame, n, &s_bundle)) {
        ESP_LOGE(TAG, "corrupt CalendarBundle payload (%u bytes)", (unsigned)n);
        goto out;
    }
    cache_apply(&s_bundle);
    ok = true;

out:
    close(fd);
    freeaddrinfo(res);
    return ok;
}

/* ------------------------------------------------------- ST7305 rendering */

static u8g2_st7305_t s_display;

static void display_init(void)
{
    u8g2_st7305_config_t cfg = u8g2_st7305_default_config();
    cfg.mosi_io = RD_MOSI_PIN;
    cfg.sclk_io = RD_SCK_PIN;
    cfg.dc_io = RD_DC_PIN;
    cfg.cs_io = RD_CS_PIN;
    cfg.reset_io = RD_RST_PIN;
    cfg.rotation = U8G2_R1;
    cfg.tile_buf_height = U8G2_ST7305_TILE_BUF_FULL;
    cfg.prefer_psram = true;
    ESP_ERROR_CHECK(u8g2_st7305_init(&s_display, &cfg));
}

static void draw_centered(u8g2_t *u8g2, int baseline_y, const char *s)
{
    int w = u8g2_GetStrWidth(u8g2, s);
    int x = (u8g2_GetDisplayWidth(u8g2) - w) / 2;
    if (x < 0) {
        x = 0;
    }
    u8g2_DrawStr(u8g2, x, baseline_y, s);
}

static void draw_right(u8g2_t *u8g2, int right_x, int baseline_y, const char *s)
{
    u8g2_DrawStr(u8g2, right_x - u8g2_GetStrWidth(u8g2, s), baseline_y, s);
}

/* 5x8 monospace-ish estimate for the clip length. */
#define RD_TITLE_CHAR_PX 5

static void draw_clipped_title(u8g2_t *u8g2, int x, int base, const char *s,
                               int max_px)
{
    size_t n = strlen(s);
    size_t maxch = (size_t)(max_px / RD_TITLE_CHAR_PX);
    if (maxch >= n) {
        u8g2_DrawStr(u8g2, x, base, s);
        return;
    }
    if (maxch < 2) {
        return;
    }
    char tmp[LMA_PB_EVENT_TITLE_MAX + 1];
    if (maxch > sizeof(tmp) - 1) {
        maxch = sizeof(tmp) - 1;
    }
    memcpy(tmp, s, maxch);
    tmp[maxch] = '\0';
    /* do not split a UTF-8 sequence: back off trailing continuation bytes */
    while (maxch > 0 && ((uint8_t)tmp[maxch - 1] & 0xC0) == 0x80) {
        maxch--;
    }
    tmp[maxch] = '\0';
    u8g2_DrawStr(u8g2, x, base, tmp);
}

/* Epoch ms (UTC) -> HH:MM, pure arithmetic (no RTC dependency). */
static void epoch_ms_to_hhmm(uint64_t epoch_ms, char out[6])
{
    uint64_t tod = (epoch_ms / 1000) % 86400;
    unsigned hh = (unsigned)(tod / 3600);
    unsigned mm = (unsigned)((tod % 3600) / 60);
    snprintf(out, 6, "%02u:%02u", hh, mm);
}

static void header_date_str(char *out, size_t cap)
{
    out[0] = '\0';
#if RD_SNTP_SERVER[0] != '\0'
    if (esp_sntp_get_sync_status() != SNTP_SYNC_STATUS_COMPLETED) {
        return;
    }
    time_t now = time(NULL);
    struct tm tmv;
    gmtime_r(&now, &tmv);
    if (strftime(out, cap, "%Y-%m-%d", &tmv) == 0) {
        out[0] = '\0';
    }
#endif
}

/* "pic" is just an icon-pack key; draw a 16x16 placeholder box + glyph. */
static void draw_pic_placeholder(u8g2_t *u8g2, int x, int y, int sz)
{
    u8g2_DrawFrame(u8g2, x, y, sz, sz);
    u8g2_DrawCircle(u8g2, x + sz / 2, y + sz / 2, sz / 4);
    u8g2_DrawLine(u8g2, x, y, x + sz - 1, y + sz - 1);
    u8g2_DrawLine(u8g2, x + sz - 1, y, x, y + sz - 1);
}

typedef enum {
    RD_STATE_FRESH,   /* synced just now */
    RD_STATE_STALE,   /* connected but sync failed, showing cache */
    RD_STATE_OFFLINE, /* no wifi, showing cache (or an offline note) */
} rd_state_t;

static void render_screen(rd_state_t state)
{
    u8g2_t *u8g2 = u8g2_st7305_get_u8g2(&s_display);
    int W = u8g2_GetDisplayWidth(u8g2);
    int H = u8g2_GetDisplayHeight(u8g2);

    u8g2_ClearBuffer(u8g2);
    u8g2_SetDrawColor(u8g2, 1);

    /* header: "family calendar" + (optional) SNTP date on the right */
    u8g2_SetFont(u8g2, u8g2_font_10x20_tf);
    char date[16];
    header_date_str(date, sizeof(date));
    if (date[0] != '\0') {
        draw_right(u8g2, W - 6, 24, date);
    }
    draw_centered(u8g2, 24, "family calendar");
    u8g2_DrawHLine(u8g2, 4, 34, W - 8);

    if (s_cache_count == 0) {
        const char *msg = (state == RD_STATE_OFFLINE) ? "no wifi  :  offline"
                                                      : "no calendar events";
        u8g2_SetFont(u8g2, u8g2_font_10x20_tf);
        draw_centered(u8g2, (H / 2) + 7, msg);
        u8g2_SendBuffer(u8g2);
        return;
    }

    u8g2_SetFont(u8g2, u8g2_font_5x8_tf);

    /* small status hint under the rule when data is not fresh */
    if (state == RD_STATE_STALE) {
        draw_right(u8g2, W - 6, 41, "stale");
    } else if (state == RD_STATE_OFFLINE) {
        draw_right(u8g2, W - 6, 41, "no wifi");
    }

    const int row_top = 50;
    const int row_stride = 22;
    const int box_x = 6;
    const int box_sz = 16;
    const int time_w = 5 * 5 + 4;    /* "HH:MM" at 5px/char + margin */
    const int title_x = box_x + box_sz + 6;

    int y = row_top;
    size_t visible = 0;
    for (size_t i = 0; i < s_cache_count; i++) {
        if (y + row_stride > H) {
            break; /* rest of the day is off-screen; split is fine for v1 */
        }
        const lma_cal_event_t *ev = &s_cache[i];

        if (ev->pic[0] != '\0') {
            draw_pic_placeholder(u8g2, box_x, y, box_sz);
        }

        char hhmm[6];
        epoch_ms_to_hhmm(ev->start_ms, hhmm);
        draw_right(u8g2, W - 6, y + 12, hhmm);

        int avail = (W - 6) - title_x - time_w;
        draw_clipped_title(u8g2, title_x, y + 12, ev->title, avail);

        y += row_stride;
        visible++;
    }

    u8g2_SendBuffer(u8g2);
    ESP_LOGI(TAG, "rendered %u rows (cache=%u)", (unsigned)visible,
             (unsigned)s_cache_count);
}

/* ------------------------------------------------------------------ sleep */

static void sleep_until_next_sync(void)
{
    if (s_wifi_started) {
        s_wifi_started = false; /* stop the disconnect-retry handler first */
        esp_wifi_disconnect();
        esp_wifi_stop();
    }
    esp_sleep_enable_timer_wakeup((uint64_t)RD_SYNC_INTERVAL_SECONDS * 1000000ULL);
    ESP_LOGI(TAG, "light sleep for %u s", (unsigned)RD_SYNC_INTERVAL_SECONDS);
    esp_light_sleep_start();
}

/* ------------------------------------------------------------------ entry */

void robodog_app_main(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());

    config_load();
    watermark_load();
    cache_load();

    wifi_init();
    display_init();

    render_screen(RD_STATE_OFFLINE);
    ESP_LOGI(TAG, "booted: %u cached events, watermark=%llu, server=%s",
             (unsigned)s_cache_count, (unsigned long long)s_watermark,
             s_server_host);

    for (;;) {
        rd_state_t state;
        bool wifi_ok = wifi_connect();
        if (wifi_ok) {
            sntp_sync();
            state = tcp_sync() ? RD_STATE_FRESH : RD_STATE_STALE;
        } else {
            state = RD_STATE_OFFLINE;
        }
        render_screen(state);
        sleep_until_next_sync();
    }
}
