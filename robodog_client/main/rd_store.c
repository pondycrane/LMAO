/*
 * rd_store.c -- NVS config + persisted calendar cache (see rd_store.h).
 */
#include "rd_store.h"

#include <stdio.h>
#include <string.h>

#include "esp_log.h"
#include "nvs.h"

#include "config.h"

static const char *TAG = "rd_store";

/* One table owns the key -> destination -> default mapping, so a key cannot
 * be renamed in one place and forgotten in another (the positional array this
 * replaces had four parallel arrays). */
typedef struct {
    const char *key;
    char *dst;
    size_t cap;
    const char *def;
} rd_cfg_field_t;

void rd_config_load(rd_config_t *cfg)
{
    const rd_cfg_field_t fields[] = {
        {RD_NVS_KEY_SSID, cfg->wifi_ssid, sizeof(cfg->wifi_ssid),
         RD_WIFI_SSID_DEFAULT},
        {RD_NVS_KEY_PASS, cfg->wifi_pass, sizeof(cfg->wifi_pass),
         RD_WIFI_PASS_DEFAULT},
        {RD_NVS_KEY_HOST, cfg->server_host, sizeof(cfg->server_host),
         RD_SERVER_HOST_DEFAULT},
        {RD_NVS_KEY_KID, cfg->kid_name, sizeof(cfg->kid_name),
         RD_KID_NAME_DEFAULT},
    };
    const size_t n = sizeof(fields) / sizeof(fields[0]);

    for (size_t i = 0; i < n; i++) {
        snprintf(fields[i].dst, fields[i].cap, "%s", fields[i].def);
    }

    nvs_handle_t h;
    if (nvs_open(RD_NVS_NAMESPACE, NVS_READONLY, &h) != ESP_OK) {
        return;
    }
    for (size_t i = 0; i < n; i++) {
        size_t sz = fields[i].cap;
        if (nvs_get_str(h, fields[i].key, fields[i].dst, &sz) != ESP_OK) {
            snprintf(fields[i].dst, fields[i].cap, "%s", fields[i].def);
        }
    }
    nvs_close(h);
}

/* "e<idx>" into the CALLER's buffer: the old cache_key() returned a shared
 * static, which is a landmine the moment two callers (or a log line and the
 * write) interleave. */
static void cache_key(size_t i, char *buf, size_t cap)
{
    snprintf(buf, cap, RD_NVS_KEY_EVENT_PREFIX "%u", (unsigned)i);
}

void rd_cache_init(rd_cache_t *cache)
{
    memset(cache, 0, sizeof(*cache));
}

static uint8_t schema_read(nvs_handle_t h)
{
    uint8_t v = 0;
    (void)nvs_get_u8(h, RD_NVS_KEY_SCHEMA, &v);   /* absent -> 0 */
    return v;
}

static void schema_write(uint8_t v)
{
    nvs_handle_t h;
    if (nvs_open(RD_NVS_NAMESPACE, NVS_READWRITE, &h) != ESP_OK) {
        return;
    }
    nvs_set_u8(h, RD_NVS_KEY_SCHEMA, v);
    nvs_commit(h);
    nvs_close(h);
}

void rd_cache_load(rd_cache_t *cache)
{
    nvs_handle_t h;
    rd_cache_init(cache);

    if (nvs_open(RD_NVS_NAMESPACE, NVS_READONLY, &h) != ESP_OK) {
        schema_write(RD_CACHE_SCHEMA);
        return;
    }
    const uint8_t schema = schema_read(h);
    if (schema != RD_CACHE_SCHEMA) {
        ESP_LOGW(TAG, "cache schema %u != %u: clearing the persisted cache "
                      "(a boot full fetch rebuilds it)",
                 (unsigned)schema, (unsigned)RD_CACHE_SCHEMA);
        nvs_close(h);
        rd_cache_clear(cache);
        schema_write(RD_CACHE_SCHEMA);
        return;
    }

    uint8_t cnt = 0;
    if (nvs_get_u8(h, RD_NVS_KEY_COUNT, &cnt) == ESP_OK) {
        for (size_t i = 0; i < cnt && i < LMA_PB_EVENT_MAX; i++) {
            char key[12];
            size_t sz = sizeof(cache->events[i]);
            cache_key(i, key, sizeof(key));
            if (nvs_get_blob(h, key, &cache->events[i], &sz) != ESP_OK) {
                continue;   /* stale/partial blob -> the next fetch fills it */
            }
            cache->count++;
        }
    }
    (void)nvs_get_u64(h, RD_NVS_KEY_WM, &cache->watermark_ms);
    nvs_close(h);
}

static void cache_persist(const rd_cache_t *cache)
{
    nvs_handle_t h;
    if (nvs_open(RD_NVS_NAMESPACE, NVS_READWRITE, &h) != ESP_OK) {
        return;
    }
    nvs_set_u8(h, RD_NVS_KEY_COUNT, (uint8_t)cache->count);
    for (size_t i = 0; i < cache->count; i++) {
        char key[12];
        cache_key(i, key, sizeof(key));
        nvs_set_blob(h, key, &cache->events[i], sizeof(cache->events[i]));
    }
    for (size_t i = cache->count; i < LMA_PB_EVENT_MAX; i++) {
        char key[12];
        cache_key(i, key, sizeof(key));
        nvs_erase_key(h, key);   /* may be NOT_FOUND -> ignored */
    }
    nvs_set_u64(h, RD_NVS_KEY_WM, cache->watermark_ms);
    nvs_set_u8(h, RD_NVS_KEY_SCHEMA, RD_CACHE_SCHEMA);
    nvs_commit(h);
    nvs_close(h);
}

void rd_cache_clear(rd_cache_t *cache)
{
    rd_cache_init(cache);

    nvs_handle_t h;
    if (nvs_open(RD_NVS_NAMESPACE, NVS_READWRITE, &h) != ESP_OK) {
        return;
    }
    nvs_erase_key(h, RD_NVS_KEY_COUNT);
    nvs_erase_key(h, RD_NVS_KEY_WM);
    for (size_t i = 0; i < LMA_PB_EVENT_MAX; i++) {
        char key[12];
        cache_key(i, key, sizeof(key));
        nvs_erase_key(h, key);   /* may be NOT_FOUND -> ignored */
    }
    nvs_set_u8(h, RD_NVS_KEY_SCHEMA, RD_CACHE_SCHEMA);
    nvs_commit(h);
    nvs_close(h);
}

static int cache_find_by_uid(const rd_cache_t *cache, const char *uid)
{
    for (size_t i = 0; i < cache->count; i++) {
        if (strcmp(cache->events[i].uid, uid) == 0) {
            return (int)i;
        }
    }
    return -1;
}

static void cache_remove(rd_cache_t *cache, size_t idx)
{
    if (idx + 1 < cache->count) {
        memmove(&cache->events[idx], &cache->events[idx + 1],
                (cache->count - idx - 1) * sizeof(cache->events[0]));
    }
    cache->count--;
}

/* Rising start_ms so the calendar renders in chronological order. */
static void cache_sort_by_start(rd_cache_t *cache)
{
    for (size_t i = 1; i < cache->count; i++) {
        lma_cal_event_t v = cache->events[i];
        size_t j = i;
        while (j > 0 && cache->events[j - 1].start_ms > v.start_ms) {
            cache->events[j] = cache->events[j - 1];
            j--;
        }
        cache->events[j] = v;
    }
}

const char *rd_cache_headline_uid(const rd_cache_t *cache, uint64_t now_ms)
{
    const lma_cal_event_t *best = NULL;
    for (size_t i = 0; i < cache->count; i++) {
        const lma_cal_event_t *ev = &cache->events[i];
        if (now_ms != 0 && ev->end_ms != 0 && ev->end_ms < now_ms) {
            continue;
        }
        if (best == NULL || ev->start_ms < best->start_ms) {
            best = ev;
        }
    }
    return best ? best->uid : NULL;
}

void rd_cache_apply(rd_cache_t *cache, const lma_cal_bundle_t *bundle)
{
    if (bundle->format != 0 && bundle->format != 1) {
        ESP_LOGW(TAG, "bundle format=%u (expected 1)",
                 (unsigned)bundle->format);
    }
    size_t applied = 0;
    for (size_t i = 0; i < bundle->event_count; i++) {
        const lma_cal_event_t *ev = &bundle->events[i];
        int idx = cache_find_by_uid(cache, ev->uid);
        if (ev->deleted) {
            if (idx >= 0) {
                cache_remove(cache, (size_t)idx);
                applied++;
            }
        } else if (idx >= 0) {
            cache->events[idx] = *ev;
            applied++;
        } else if (cache->count < LMA_PB_EVENT_MAX) {
            cache->events[cache->count++] = *ev;
            applied++;
        } else {
            ESP_LOGW(TAG, "cache full, dropping event uid=%s", ev->uid);
        }
    }
    if (bundle->watermark_ms > cache->watermark_ms) {
        cache->watermark_ms = bundle->watermark_ms;
    }
    /* Adopt the served tile only when it belongs to the event that is now the
     * headline; otherwise drop it so a drawing never sits beside a different
     * event's title. */
    if (bundle->pic_data_len == LMA_PB_PIC_TILE_BYTES) {
        const char *headline = rd_cache_headline_uid(cache, bundle->watermark_ms);
        if (headline != NULL && strcmp(headline, bundle->pic_uid) == 0) {
            memcpy(cache->pic_data, bundle->pic_data, LMA_PB_PIC_TILE_BYTES);
            cache->pic_data_len = LMA_PB_PIC_TILE_BYTES;
            snprintf(cache->pic_uid, sizeof(cache->pic_uid), "%s",
                     bundle->pic_uid);
        } else {
            cache->pic_data_len = 0;
            cache->pic_uid[0] = '\0';
        }
    } else if (bundle->event_count > 0) {
        /* No tile served (delta without the headline, or none exists): keep a
         * tile only while its uid is still the headline. */
        const char *headline = rd_cache_headline_uid(cache, bundle->watermark_ms);
        if (headline == NULL || strcmp(headline, cache->pic_uid) != 0) {
            cache->pic_data_len = 0;
            cache->pic_uid[0] = '\0';
        }
    }
    cache_sort_by_start(cache);
    cache_persist(cache);
    ESP_LOGI(TAG, "applied %u delta events, cache=%u, watermark=%llu, tile=%u",
             (unsigned)applied, (unsigned)cache->count,
             (unsigned long long)cache->watermark_ms,
             (unsigned)cache->pic_data_len);
}
