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
 *   6. stop WiFi and hold the RLCD frame visible (no light-sleep; the
 *      reflective ST7305 loses its image in sleep) for SYNC_INTERVAL_SECONDS, then repeat
 *
 * Wire format (both directions), payloads are raw protobuf (see lma_proto.c):
 *   [u32 BE length][payload bytes]
 *   request  = CalendarRequest  (empty payload == server defaults "family"/0)
 *   response = CalendarBundle
 */
#include <string.h>
#include <stddef.h>
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
#include "esp_sleep.h"
#include "esp_timer.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "lwip/sockets.h"
#include "lwip/netdb.h"

#include "config.h"
#include "lma_proto.h"
#include "u8g2_st7305.h"

/* Host-clean pure logic (date/meta formatting + measured clip length and
 * word wrap): the firmware wraps their width/string callbacks; the selftest
 * proves the identical code on the host `cc` gate. */
#include "rd_clip.h"
#include "rd_datetime.h"

static const char *TAG = "robodog";

/* rd_clip's internal prefix buffer must hold any server-served title. */
_Static_assert(LMA_PB_EVENT_TITLE_MAX <= RD_CLIP_TITLE_MAX,
               "rd_clip prefix buffer must cover the event title cap");

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
static uint8_t s_rx[LMA_PB_MAX_FRAME];   /* one received HTTP/2 frame payload */
static uint8_t s_h2[512];                 /* HPACK header block (fixed RPC) */
static lma_cal_bundle_t s_bundle;

/* Defined below; tcp_sync sets the display clock from the server watermark. */
static void clock_set(uint64_t utc_ms);

#define NVS_NS "robodog"
#define NVS_KEY_WM "wm"
#define NVS_KEY_EC "ec"

/* NVS persists only the non-image prefix of each event: the 24 KB NVS
 * partition can't hold per-event 2592 B pictures, and the panel re-fetches
 * the headline image on every (boot) full sync anyway. */
#define NVS_EV_PREFIX offsetof(lma_cal_event_t, pic_data)

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
            size_t sz = NVS_EV_PREFIX;
            if (nvs_get_blob(h, cache_key(i), &s_cache[i], &sz) != ESP_OK) {
                continue; /* stale full-size blob (older FW) -> re-fetched */
            }
            s_cache[i].pic_data_len = 0; /* images live in RAM only */
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
        nvs_set_blob(h, cache_key(i), &s_cache[i], NVS_EV_PREFIX);
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

/* WiFi disconnect reason codes we care about (esp_wifi_types.h). */
static const char *wifi_reason_str(int r)
{
    switch (r) {
    case 201: return "NO_AP_FOUND (SSID not visible: 2.4GHz? range? hidden?)";
    case 202: return "AUTH_FAIL (wrong password / >63-char passphrase)";
    case 203: return "ASSOC_FAIL";
    case 15:  return "4WAY_HANDSHAKE_TIMEOUT (wrong password / length)";
    case 204: return "AUTH_EXPIRE";
    default:  return "other";
    }
}

static void wifi_event_handler(void *arg, esp_event_base_t base, int32_t id,
                               void *data)
{
    (void)arg;
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        ESP_LOGI(TAG, "wifi STA started, connecting to '%s' (pass len %u)",
                 s_wifi_ssid, (unsigned)strlen(s_wifi_pass));
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_CONNECTED) {
        ESP_LOGI(TAG, "wifi associated with AP");
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        wifi_event_sta_disconnected_t *d = (wifi_event_sta_disconnected_t *)data;
        ESP_LOGW(TAG, "wifi disconnected reason=%d (%s)",
                 d ? d->reason : -1, wifi_reason_str(d ? d->reason : -1));
        if (s_wifi_started) {
            ESP_LOGW(TAG, "reconnecting");
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

    wifi_init_config_t wic = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&wic));
    /* create_default_wifi_sta() AFTER esp_wifi_init(); it returns esp_netif_t*
       (NULL on failure), not esp_err_t, so it must NOT go through
       ESP_ERROR_CHECK — check for NULL instead. */
    esp_netif_t *sta = esp_netif_create_default_wifi_sta();
    if (sta == NULL) {
        ESP_LOGE(TAG, "create_default_wifi_sta failed");
        abort();
    }

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
#if RD_SNTP_ENABLED
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

/* ------------------------------------------------- gRPC calendar sync */
/* Minimal HTTP/2 + gRPC unary client for the single GetCalendar RPC.
 * Wire format verified live against the LMAO server:
 *   connection preface -> SETTINGS -> ACK -> HEADERS(/lma.LMAO/GetCalendar,
 *   literal-without-indexing HPACK) -> DATA(1-byte flag + 4-byte BE msg len +
 *   CalendarRequest) -> read frames; DATA carries the gRPC-framed reply.
 * Response HEADERS/HPACK are intentionally NOT decoded: grpc-status != 0
 * yields no valid CalendarBundle (decode fails), which we treat as a failed
 * sync and retry next poll. */
#define H2_PREFACE   "PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"
#define H2_PREFACE_LEN 24u

static bool h2_send_frame(int fd, uint8_t type, uint8_t flags, uint32_t stream,
                          const uint8_t *payload, size_t plen)
{
    uint8_t h[9];
    h[0] = (uint8_t)((plen >> 16) & 0xff);
    h[1] = (uint8_t)((plen >> 8) & 0xff);
    h[2] = (uint8_t)(plen & 0xff);
    h[3] = type;
    h[4] = flags;
    h[5] = (uint8_t)(stream >> 24);
    h[6] = (uint8_t)(stream >> 16);
    h[7] = (uint8_t)(stream >> 8);
    h[8] = (uint8_t)(stream & 0xff);
    return send_all(fd, h, 9) && send_all(fd, payload, plen);
}

static bool h2_recv_frame(int fd, uint8_t *type_out, uint8_t *flags_out,
                          uint32_t *stream_out, size_t cap, size_t *plen_out)
{
    uint8_t h[9];
    if (!recv_exact(fd, h, 9)) {
        return false;
    }
    size_t ln = ((size_t)h[0] << 16) | ((size_t)h[1] << 8) | (size_t)h[2];
    *type_out = h[3];
    *flags_out = h[4];
    *stream_out = ((uint32_t)h[5] << 24) | ((uint32_t)h[6] << 16) |
                  ((uint32_t)h[7] << 8) | (uint32_t)h[8];
    if (ln > cap) {
        ESP_LOGW(TAG, "h2 frame too large (%u > %u)", (unsigned)ln, (unsigned)cap);
        return false;
    }
    if (!recv_exact(fd, s_rx, ln)) {
        return false;
    }
    *plen_out = ln;
    return true;
}

/* HPACK literal-without-indexing header: 0x00, len(name), name, len(value),
 * value.  Valid HPACK, no Huffman, no dynamic table — trivially encodable. */
static size_t hpack_lit_header(uint8_t *o, const char *name, const char *value)
{
    size_t nl = strlen(name);
    size_t vl = strlen(value);
    size_t p = 0;
    o[p++] = 0x00;
    o[p++] = (uint8_t)nl;
    memcpy(o + p, name, nl);
    p += nl;
    o[p++] = (uint8_t)vl;
    memcpy(o + p, value, vl);
    p += vl;
    return p;
}

static bool tcp_sync(void)
{
    char port[8];
    snprintf(port, sizeof(port), "%u", RD_CAL_GRPC_PORT);

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

    /* HPACK header block for POST /lma.LMAO/GetCalendar */
    char authority[96];
    snprintf(authority, sizeof(authority), "%s:%s", s_server_host, port);
    size_t hp = 0;
    hp += hpack_lit_header(s_h2 + hp, ":method", "POST");
    hp += hpack_lit_header(s_h2 + hp, ":scheme", "http");
    hp += hpack_lit_header(s_h2 + hp, ":path", "/lma.LMAO/GetCalendar");
    hp += hpack_lit_header(s_h2 + hp, ":authority", authority);
    hp += hpack_lit_header(s_h2 + hp, "content-type", "application/grpc");
    hp += hpack_lit_header(s_h2 + hp, "te", "trailers");

    /* Serialize CalendarRequest */
    lma_cal_request_t req;
    lma_proto_req_init(&req);
    snprintf(req.calendar_id, sizeof(req.calendar_id), "%s", RD_CALENDAR_ID);
    req.since_ms = s_watermark;          /* 0 -> full fetch on first boot */
    const bool is_full = (req.since_ms == 0);
    int plen = lma_proto_req_encode(&req, s_frame, sizeof(s_frame));
    if (plen < 0) {
        ESP_LOGE(TAG, "request does not fit frame buffer");
        goto out;
    }

    /* HTTP/2 connection preface + our empty SETTINGS */
    if (!send_all(fd, (const uint8_t *)H2_PREFACE, H2_PREFACE_LEN) ||
        !h2_send_frame(fd, 0x4, 0x0, 0, NULL, 0)) {
        ESP_LOGW(TAG, "h2 handshake write failed");
        goto out;
    }

    bool opened = false;
    bool done = false;
    size_t resp = 0;   /* accumulated DATA payloads (gRPC-framed reply) */
    uint8_t ftype, fflags;
    uint32_t fstream;
    size_t flen;

    while (!done) {
        if (!h2_recv_frame(fd, &ftype, &fflags, &fstream,
                           sizeof(s_rx), &flen)) {
            ESP_LOGW(TAG, "h2 frame read failed (timeout %u ms)",
                     (unsigned)RD_TCP_FRAME_TIMEOUT_MS);
            goto out;
        }
        if (ftype == 0x4 && !(fflags & 0x1)) {          /* server SETTINGS */
            /* ACK (one-shot on the first server SETTINGS pass) */
            if (!opened) {
                if (!h2_send_frame(fd, 0x4, 0x1, 0, NULL, 0) ||
                    !h2_send_frame(fd, 0x1, 0x4, 1, s_h2, hp)) {
                    ESP_LOGW(TAG, "h2 request write failed");
                    goto out;
                }
                /* gRPC message: 1B flag(0) + 4B BE length + CalendarRequest */
                s_rx[0] = 0;
                s_rx[1] = (uint8_t)((signed)plen >> 24);
                s_rx[2] = (uint8_t)((signed)plen >> 16);
                s_rx[3] = (uint8_t)((signed)plen >> 8);
                s_rx[4] = (uint8_t)(signed)plen;
                memcpy(s_rx + 5, s_frame, (size_t)plen);
                if (!h2_send_frame(fd, 0x0, 0x1, 1, s_rx, (size_t)5 + (size_t)plen)) {
                    ESP_LOGW(TAG, "h2 request DATA failed");
                    goto out;
                }
                opened = true;
            } else {
                /* late SETTINGS from a server restart on the same conn */
                if (!h2_send_frame(fd, 0x4, 0x1, 0, NULL, 0)) {
                    goto out;
                }
            }
        } else if (ftype == 0x0) {                       /* DATA */
            if (resp + flen <= sizeof(s_frame)) {
                memcpy(s_frame + resp, s_rx, flen);
                resp += flen;
            } else {
                ESP_LOGW(TAG, "reply too large (%u bytes)", (unsigned)(resp + flen));
                goto out;
            }
        }
        /* END_STREAM only exists on HEADERS/DATA frames — bit 0 on a SETTINGS
         * frame means ACK and must NOT terminate the response read. */
        if ((ftype == 0x1 || ftype == 0x0) && (fflags & 0x1)) {
            done = true;
        }
    }

    /* Accumulated DATA = gRPC message framing: [0][u32 len][CalendarBundle] */
    if (resp >= 5u) {
        uint32_t mlen = ((uint32_t)s_frame[1] << 24) | ((uint32_t)s_frame[2] << 16) |
                        ((uint32_t)s_frame[3] << 8) | (uint32_t)s_frame[4];
        if (s_frame[0] == 0u && (size_t)5 + (size_t)mlen <= resp &&
            lma_proto_bundle_decode(s_frame + 5, mlen, &s_bundle)) {
            clock_set(s_bundle.watermark_ms);  /* server epoch-ms as clock base */
            if (is_full) {
                /* A full fetch (since_ms=0) is the server's complete truth:
                 * the server sends live events only, so drop cached events
                 * that are no longer in it (deletions don't ship as
                 * tombstones on a full fetch). */
                s_cache_count = 0;
            }
            cache_apply(&s_bundle);
            ok = true;
        } else {
            ESP_LOGE(TAG, "no valid CalendarBundle in gRPC reply (%u bytes)",
                     (unsigned)resp);
        }
    } else {
        ESP_LOGE(TAG, "empty gRPC reply (%u bytes)", (unsigned)resp);
    }

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

/* ---- clipped row text ----------------------------------------------- */

/* Clip a title to max_px by MEASURED width under the active font: u8g2 is
 * un-kerned so u8g2_GetStrWidth is the exact sum of glyph advances, and a
 * fixed chars-per-px divisor can never bound a proportional headline
 * (helvB24 W/M run 23-33 px/char).  The pure boundary search lives in
 * rd_clip.c (host-tested), so a clipped title is structurally guaranteed to
 * fit max_px and never to silently overrun the panel edge. */
static int rd_u8g2_width(const char *prefix, void *ctx)
{
    return u8g2_GetStrWidth((u8g2_t *)ctx, prefix);
}

static void draw_clipped_title(u8g2_t *u8g2, int x, int base, const char *s,
                               int max_px)
{
    if (max_px <= 0) {
        /* Never draw the full untruncated title into a zero/negative box (a
         * negative budget is the classic int -> huge-size_t cast footgun that
         * would collide with the right-aligned meta); drop it loudly. */
        ESP_LOGW(TAG, "title clip budget %dpx <= 0; dropping '%s'", max_px, s);
        return;
    }
    size_t n = strlen(s);
    if ((int)u8g2_GetStrWidth(u8g2, s) <= max_px) {
        u8g2_DrawStr(u8g2, x, base, s); /* whole title fits: no cut marker */
        return;
    }
    /* Prefer the real "…" (U+2026, UTF-8 \xE2\x80\xA6) when the active font
     * carries it; the _tf subsets lack it and u8g2 would render a fallback
     * blob glyph, so fall back to plain ASCII "..." -- a clipped title is
     * always visibly cut, in either font. */
    const char *ell = u8g2_IsGlyph(u8g2, 0x2026) ? "\xE2\x80\xA6" : "...";
    const int ell_w = u8g2_GetStrWidth(u8g2, ell);
    const size_t lo = rd_clip_prefix(s, n, max_px, ell_w, rd_u8g2_width,
                                     u8g2);
    if (lo == 0) {
        ESP_LOGW(TAG, "no prefix of '%s' + ellipsis fits in %dpx; dropping",
                 s, max_px);
        return;
    }
    if (lo >= n) { /* whole title fits alongside the ellipsis: draw it whole */
        u8g2_DrawStr(u8g2, x, base, s);
        return;
    }
    char tmp[LMA_PB_EVENT_TITLE_MAX + 1];
    memcpy(tmp, s, lo);
    tmp[lo] = '\0';
    strncat(tmp, ell, sizeof(tmp) - strlen(tmp) - 1);
    u8g2_DrawStr(u8g2, x, base, tmp);
}

/* Final SHOWN line of a vertically-truncated wrapped headline title.  The
 * wrapped block is bottom-anchored with room for n_fit lines (see
 * render_screen); when the title wrapped to more lines than that, the last
 * shown line MUST carry the cut marker — unlike draw_clipped_title the
 * whole-title fast path is deliberately SKIPPED, because even a line that
 * fits the 218 px column horizontally still signals the hidden tail.  Same
 * measured ellipsis selection (u8g2_IsGlyph(0x2026) -> real "…" or "...") and
 * the same rd_clip_prefix contract, so prefix + marker never exceed max_px. */
static void draw_wrapped_final(u8g2_t *u8g2, int x, int base,
                               const char *s, size_t len, int max_px)
{
    if (max_px <= 0) {
        /* Negative budget: the classic int -> huge-size_t cast footgun; drop
         * loudly rather than draw an over-wide line over the meta. */
        ESP_LOGW(TAG, "wrapped final-line budget %dpx <= 0; dropping '%s'",
                 max_px, s);
        return;
    }
    /* Measure the ellipsis under the ACTIVE (helvB18) font. */
    const char *ell = u8g2_IsGlyph(u8g2, 0x2026) ? "\xE2\x80\xA6" : "...";
    const int ell_w = u8g2_GetStrWidth(u8g2, ell);
    const size_t lo = rd_clip_prefix(s, len, max_px, ell_w, rd_u8g2_width,
                                     u8g2);
    char tmp[LMA_PB_EVENT_TITLE_MAX + 1];
    if (lo == 0) {
        /* Even one glyph + the marker is too wide: draw just the marker (it
         * always fits at the real 218 px budget). */
        memcpy(tmp, ell, strlen(ell) + 1);
        ESP_LOGW(TAG, "wrapped final line: only ellipsis fits of '%s'", s);
    } else {
        memcpy(tmp, s, lo);
        tmp[lo] = '\0';
        strncat(tmp, ell, sizeof(tmp) - strlen(tmp) - 1);
    }
    u8g2_DrawStr(u8g2, x, base, tmp);
}

/* Row cap: at most 3 event rows are drawn — the headline (pic + right-side
 * info) plus 2 compact text rows; a 4th+ visible event is dropped by design. */
#define RD_MAX_EVENT_ROWS 3

/* Headline wrapped-title vertical band (render_screen): the bottom-anchored
 * helvB18 block sits between the header hline (y=40) and the meta glyph top
 * (148 - 10x20 ascent 16 = 132).  TITLE_TOP_MARGIN is the clearance below
 * the hline before the first line's glyph top; TITLE_META_GAP is the gap
 * between the last line's glyph bottom and the meta glyph top.  Both are
 * panel-tuned against capture.py.  Real helvB18 metrics (measured via
 * u8g2_GetAscent/GetDescent, never assumed): ascent 19, descent -5, so the
 * 24px pitch fits 3 lines whose cap tops stay below the hline (baselines
 * 76/100/124); a 4th line would cross the hline and is truncated instead. */
#define TITLE_TOP_MARGIN 6
#define TITLE_META_GAP   3

/* ---- Display clock: derived from the server's epoch-ms watermark, and
 * advanced by esp_timer so the screen shows a live date/time to the minute.
 * No RTC/SNTP (esp_sntp is absent from the IDF build image); the server's
 * CalendarBundle.watermark_ms is the DB clock at sync (~ now). */
static uint64_t s_epoch_base_ms = 0;    /* 0 = no clock yet (pre-first-sync) */
static uint64_t s_epoch_base_tick = 0;  /* esp_timer ms when base was set */

static void clock_set(uint64_t utc_ms)
{
    s_epoch_base_ms = utc_ms;
    s_epoch_base_tick = esp_timer_get_time() / 1000;
}

static uint64_t clock_now_ms(void)
{
    if (s_epoch_base_ms == 0) {
        return 0;
    }
    return s_epoch_base_ms +
           (esp_timer_get_time() / 1000 - s_epoch_base_tick);
}

/* The pure calendar math lives in rd_datetime.c (rd_utc_ms_to_dt /
 * rd_utc_ms_to_local_day / rd_utc_ms_to_datetime / rd_format_event_meta) —
 * single source of truth, host-tested by rd_datetime_selftest.c.
 */

/* "pic" is just an icon-pack key; until real icon bitmaps are rendered,
 * draw a clean (un-credited) image slot box.  No fake glyph — an event with
 * no pic data just gets a tidy empty box. */
static void draw_pic_placeholder(u8g2_t *u8g2, int x, int y, int sz)
{
    u8g2_DrawFrame(u8g2, x, y, sz, sz);
}

typedef enum {
    RD_STATE_FRESH,   /* synced just now */
    RD_STATE_STALE,   /* connected but sync failed, showing cache */
    RD_STATE_OFFLINE, /* no wifi, showing cache (or an offline note) */
} rd_state_t;

/* ----------------------------------------------------------------- shot --
 * Always-on: stream the exact u8g2 full-frame buffer (== the bytes pushed to
 * the ST7305) over the USB-JTAG console so a host tool can reconstruct the
 * image the firmware drew, pixel for pixel.  Part of the normal build — the
 * whole point is to confirm the CURRENT production firmware's display, not a
 * special opt-in build.  Protocol: RD_SHOT_BEGIN/<bufsize,w,h>/lowercase-hex
 * /RD_SHOT_END.  Data is what the firmware rendered (frame buffer); the panel
 * also depends on the (reference-locked u8g2_st7305) write path.
 */
static void rd_shot_dump(u8g2_t *u8g2)
{
    const uint8_t *buf = u8g2_GetBufferPtr(u8g2);
    size_t n = u8g2_GetBufferSize(u8g2);
    u8g2_t *u = u8g2;
    printf("\nRD_SHOT_BEGIN bufsize=%u w=%d h=%d\n", (unsigned)n,
           u8g2_GetDisplayWidth(u), u8g2_GetDisplayHeight(u));
    for (size_t i = 0; i < n; i++) {
        printf("%02x", buf[i]);
        if ((i & 0x1f) == 0x1f) {
            printf("\n");
        }
    }
    printf("\nRD_SHOT_END\n");
    fflush(stdout);
}

static void render_screen(rd_state_t state)
{
    u8g2_t *u8g2 = u8g2_st7305_get_u8g2(&s_display);
    int W = u8g2_GetDisplayWidth(u8g2);
    int H = u8g2_GetDisplayHeight(u8g2);

    /* Invert: clear fills black; paint the whole panel white, then draw all
     * content in black -> white background, black text. */
    u8g2_ClearBuffer(u8g2);
    u8g2_SetDrawColor(u8g2, 1);
    u8g2_DrawBox(u8g2, 0, 0, W, H);
    u8g2_SetDrawColor(u8g2, 0);

    /* header: only the live date/time (title line removed) */
    char line[32];
    uint64_t now = clock_now_ms();
    if (now != 0) {
        u8g2_SetFont(u8g2, u8g2_font_10x20_tf);
        rd_utc_ms_to_datetime(now, RD_TZ_OFFSET_MINUTES, line, sizeof(line));
        draw_centered(u8g2, 24, line);
    }
    u8g2_DrawHLine(u8g2, 4, 40, W - 8);

    if (s_cache_count == 0) {
        const char *msg = (state == RD_STATE_OFFLINE) ? "no wifi  :  offline"
                                                      : "no calendar events";
        u8g2_SetFont(u8g2, u8g2_font_10x20_tf);
        draw_centered(u8g2, (H / 2) + 7, msg);
        u8g2_SendBuffer(u8g2);
        rd_shot_dump(u8g2);
        return;
    }

    u8g2_SetFont(u8g2, u8g2_font_10x20_tf);

    /* small status hint when data is not fresh (above the headline image) */
    if (state == RD_STATE_STALE) {
        draw_right(u8g2, W - 6, 50, "stale");
    } else if (state == RD_STATE_OFFLINE) {
        draw_right(u8g2, W - 6, 50, "no wifi");
    }

    const uint64_t now_ck = clock_now_ms(); /* 0 = clock not set yet (boot) */

    /* Qualifier anchor: local civil day + year of the display clock.  0/0
     * before the first sync (now_ck == 0) -> rows render range-only. */
    int64_t day_now = 0;
    int yr_now = 0;
    if (now_ck != 0) {
        int mo_tmp, dy_tmp, hh_tmp, mm_tmp;
        day_now = rd_utc_ms_to_local_day(now_ck, RD_TZ_OFFSET_MINUTES);
        rd_utc_ms_to_dt(now_ck, RD_TZ_OFFSET_MINUTES, &yr_now, &mo_tmp,
                        &dy_tmp, &hh_tmp, &mm_tmp);
    } else {
        /* Clock unset (pre-first-sync / lost): every event renders range-only
         * and past-event filtering is off, so cached rows can look current
         * while offline.  Log once per run so that state is diagnosable. */
        static bool s_warned_clock_unset = false;
        if (!s_warned_clock_unset) {
            s_warned_clock_unset = true;
            ESP_LOGW(TAG, "display clock unset (now_ck==0): date qualifiers "
                          "and past-event filtering are off");
        }
    }

    /* Visible events = ongoing + future, sorted by start_ms.  The panel draws
     * the picture only for the headliner; the rest are text-only rows. */
    const lma_cal_event_t *vis[LMA_PB_EVENT_MAX];
    size_t nvis = 0;
    for (size_t i = 0; i < s_cache_count && nvis < LMA_PB_EVENT_MAX; i++) {
        const lma_cal_event_t *ev = &s_cache[i];
        if (now_ck != 0 && ev->end_ms != 0 && ev->end_ms < now_ck) {
            continue; /* past — a family-calendar panel lists what's next */
        }
        vis[nvis++] = ev;
    }
    if (nvis == 0) {
        draw_centered(u8g2, (H / 2) + 7, "no upcoming events");
        u8g2_SendBuffer(u8g2);
        rd_shot_dump(u8g2);
        return;
    }

    const int img_sz = (int)LMA_PB_PIC_TILE;   /* 144 — the whole drawing */
    const int img_x = 20;                      /* LEFT-aligned (was centered) */
    const int img_y = 54;
    const int txt_x = img_x + img_sz + 12;     /* info column right of image */

    /* Headline event: the big picture (or a tidy slot, none served yet).
     * LEFT-aligned image; its range+qualifier meta line and clipped title are
     * drawn to the RIGHT, inside the image's row band (the text block is
     * vertically centred roughly on the picture).
     *
     * Draw with u8g2_DrawBitmap (MSB-first, bit 7 = leftmost pixel), NOT
     * u8g2_DrawXBM (LSB-first): the served tile_1bpp is MSB-first row-major
     * ("1 = ink", 2592 B). DrawXBM mirrors each 8-px byte, which showed up as
     * periodic stripes / mis-centering at byte boundaries on the real panel. */
    const lma_cal_event_t *hl = vis[0];
    if (hl->pic_data_len == LMA_PB_PIC_TILE_BYTES) {
        u8g2_DrawBitmap(u8g2, img_x, img_y, img_sz >> 3, img_sz,
                        hl->pic_data);
    } else {
        ESP_LOGW(TAG, "headline uid=%s has no pic data "
                      "(len=%u, expected %u); drawing placeholder",
                 hl->uid, (unsigned)hl->pic_data_len,
                 (unsigned)LMA_PB_PIC_TILE_BYTES);
        draw_pic_placeholder(u8g2, img_x, img_y, img_sz);
    }
    /* Headline title: word-wrapped helvB18 block drawn in the vertical band
     * above the right-aligned meta line (baseline 148), bottom-anchored so
     * the last line's glyph bottom clears the meta glyph top (148 - 10x20
     * ascent).  A single clipped helvB24 line used to ellipsise even a modest
     * title mid-word ("Peck Open ..."); wrapping renders the whole title on
     * up to n_fit lines, with the ellipsis ONLY on the final shown line and
     * only when the wrapped block overflows the band.  helvB18 ascent/descent
     * are measured at runtime (never hardcoded); the font is global state on
     * the u8g2_t, so 10x20 is restored below before the meta/compact rows. */
    {
        u8g2_SetFont(u8g2, u8g2_font_helvB18_tf);
        const int t_asc = u8g2_GetAscent(u8g2);
        const int t_desc = u8g2_GetDescent(u8g2);   /* <= 0 */
        const int t_line_h = t_asc - t_desc;        /* 29 for helvB18 */
        u8g2_SetFont(u8g2, u8g2_font_10x20_tf);
        const int meta_asc = u8g2_GetAscent(u8g2);  /* 16; measured, never 0 */
        u8g2_SetFont(u8g2, u8g2_font_helvB18_tf);

        const int max_px = (W - 6) - txt_x;         /* 218 -- never widened */
        rd_wrap_line_t wl[RD_WRAP_MAX_LINES];
        const size_t nl = rd_wrap_lines(hl->title, strlen(hl->title), max_px,
                                        rd_u8g2_width, u8g2, wl,
                                        RD_WRAP_MAX_LINES);
        /* Vertical fit is bottom-anchored and COMPUTED (never assumed): the
         * last line's baseline keeps its glyph bottom clear of the meta
         * glyph top, and the first line stays below the header hline (40).
         * gap/margin are panel-tuned constants; final value validated by
         * capture.py. */
        const int meta_base = img_y + img_sz / 2 + 22;      /* 148 */
        const int bottom_last =
            meta_base - meta_asc - TITLE_META_GAP - (-t_desc);
        const int top_anchor = 40 + TITLE_TOP_MARGIN;
        size_t n_fit = nl;
        /* Number of whole lines whose GLYPH TOP (baseline - ascent) stays at
         * or below top_anchor -- i.e. the first line never rises above the
         * header hline.  The ascent is subtracted from the raw band: with
         * helvB18 ascent 19 / line_h 24 the band holds 3 lines (baselines
         * 76/100/124) whose cap tops stay cleanly below the hline; a 4th
         * line would cross it, so longer titles truncate here instead. */
        const int band = (bottom_last - top_anchor - t_asc) / t_line_h + 1;
        if (band < 0 || (size_t)band < n_fit) {
            n_fit = (band < 0) ? 0u : (size_t)band;
        }
        if (n_fit > RD_WRAP_MAX_LINES) {
            n_fit = RD_WRAP_MAX_LINES;   /* we only hold that many lines */
        }
        const bool truncated = (nl > n_fit);

        for (size_t k = 0; k < n_fit; k++) {
            const int base = bottom_last - (int)(n_fit - 1 - k) * t_line_h;
            char tmp[LMA_PB_EVENT_TITLE_MAX + 1];
            const size_t lo = wl[k].off, ln = wl[k].len;
            memcpy(tmp, hl->title + lo, ln);
            tmp[ln] = '\0';
            if (k == n_fit - 1 && truncated) {
                /* Final shown line of an overflowing block: prefix + marker. */
                draw_wrapped_final(u8g2, txt_x, base, tmp, ln, max_px);
            } else {
                /* Whole line (fits max_px by construction); never ellipsise an
                 * intermediate line. */
                u8g2_DrawStr(u8g2, txt_x, base, tmp);
            }
        }
        if (truncated) {
            ESP_LOGW(TAG, "headline '%s' wrapped to %u lines, showing %u "
                          "with final-line ellipsis",
                     hl->title, (unsigned)nl, (unsigned)n_fit);
        }
        u8g2_SetFont(u8g2, u8g2_font_10x20_tf);
    }
    /* Right-aligned meta (range + qualifier): stays clear of the picture edge
     * (x=164) even for the widest "YYYY/MM/DD HH:MM-HH:MM" (~220 px). */
    {
        char meta[32];
        rd_format_event_meta(hl, RD_TZ_OFFSET_MINUTES, day_now, yr_now,
                             meta, sizeof(meta));
        draw_right(u8g2, W - 6, img_y + img_sz / 2 + 22, meta);
    }

    /* Following events: text-only compact rows — the 2nd..RD_MAX_EVENT_ROWS-1
     * visible only; anything beyond the third is dropped by design (time
     * right-aligned, title left-clipped). */
    int yy = img_y + img_sz + 32;
    const int row_stride = 24;
    size_t rendered = 1; /* the headline is always drawn */
    for (size_t v = 1; v < nvis && v < RD_MAX_EVENT_ROWS; v++) {
        const lma_cal_event_t *ev = vis[v];
        /* Right block = the measured meta (range + qualifier); the left title
         * clip follows its width so a qualifier can't be clipped away. */
        char meta[32];
        rd_format_event_meta(ev, RD_TZ_OFFSET_MINUTES, day_now, yr_now,
                             meta, sizeof(meta));
        draw_right(u8g2, W - 6, yy, meta);
        const int meta_w = u8g2_GetStrWidth(u8g2, meta);
        const int avail = (W - 6) - 20 - meta_w - 6; /* 6px meta margin */
        draw_clipped_title(u8g2, 20, yy, ev->title, avail);
        yy += row_stride;
        rendered++;
        /* Defensive backstop for future layout-constant edits (row 3 lands
         * at y=254 < H-16=284 today, so the RD_MAX_EVENT_ROWS cap is the
         * effective rule; keep guarding the panel's bottom edge anyway). */
        if (yy > H - 16) {
            break;
        }
    }

    u8g2_SendBuffer(u8g2);
    rd_shot_dump(u8g2);
    ESP_LOGI(TAG, "rendered %u visible of %u cached events (headline uid=%s)",
             (unsigned)rendered, (unsigned)s_cache_count, vis[0]->uid);
}

/* ------------------------------------------------------------------ hold */

static void hold_until_next_sync(rd_state_t state)
{
    if (s_wifi_started) {
        s_wifi_started = false; /* stop the disconnect-retry handler first */
        esp_wifi_disconnect();
        esp_wifi_stop();
    }
    /* Reflective ST7305 loses its image when the ESP32 light-sleeps the
       display power ("screen went blank"), so stay in active mode and let the
       RLCD keep the last rendered frame visible until the next sync.  Redraw
       every 30 s so the header date/time stays live (clock advances via
       esp_timer). */
    ESP_LOGI(TAG, "holding screen %u s (no light-sleep)",
             (unsigned)RD_SYNC_INTERVAL_SECONDS);
    uint32_t left_ms = (uint32_t)RD_SYNC_INTERVAL_SECONDS * 1000u;
    while (left_ms > 0) {
        uint32_t chunk = left_ms > 30000u ? 30000u : left_ms;
        vTaskDelay(pdMS_TO_TICKS(chunk));
        left_ms -= chunk;
        render_screen(state);
    }
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
    /* Full fetch on every boot: NVS stores no pic data, so each boot must
     * re-serve the headline image into RAM (the calendar is small; this is a
     * single small HTTPS-style reply every cycle, not per-event growth). */
    s_watermark = 0;

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
        hold_until_next_sync(state);
    }
}
