/*
 * rd_store.h -- NVS-backed board config + the persisted calendar cache.
 *
 * Split out of robodog.c (which had NVS config, cache merge, WiFi, the
 * transport, rendering and the app loop in one 1200-line file).  Two rules
 * live here:
 *
 *  - the NVS KEY NAMES have exactly one definition (this header) -- they were
 *    previously copied into a #define, a positional key array, a config.h
 *    comment and flash.sh's CSV, which is how the namespace-mismatch bug the
 *    flash script warns about happened;
 *  - the persisted event blob carries a SCHEMA VERSION.  The cache is a raw
 *    struct dump, so reordering a field in lma_cal_event_t would silently
 *    reinterpret every stored event.  A version bump clears the cache instead
 *    (a boot full fetch rebuilds it).
 */
#ifndef RD_STORE_H
#define RD_STORE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "lma_proto.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ---- NVS contract (flash.sh writes the same namespace/key) ---- */
#define RD_NVS_NAMESPACE  "robodog"
#define RD_NVS_KEY_SSID   "wifi_ssid"
#define RD_NVS_KEY_PASS   "wifi_pass"
#define RD_NVS_KEY_HOST   "server_host"
#define RD_NVS_KEY_KID    "kid_name"
#define RD_NVS_KEY_WM     "wm"
#define RD_NVS_KEY_WM_KID "wm_kid"
#define RD_NVS_KEY_COUNT  "ec"
#define RD_NVS_KEY_SCHEMA "sch"
#define RD_NVS_KEY_EVENT_PREFIX "e"

/* Bump when lma_cal_event_t's persisted layout changes.  Version 1 is the
 * first versioned layout (the pre-version firmware stored the same struct
 * without the key, so its cache reads as version 0 and is cleared once). */
#define RD_CACHE_SCHEMA 1u

/* Runtime config (defaults from config.h, overridden by NVS). */
typedef struct {
    char wifi_ssid[33];
    char wifi_pass[65];
    char server_host[65];
    char kid_name[32];
} rd_config_t;

/* The merged calendar the panel renders.  Images live in RAM only (the 24 KB
 * NVS partition cannot hold tiles, and a stale tile is worse than none). */
typedef struct {
    lma_cal_event_t events[LMA_PB_EVENT_MAX];
    size_t count;
    uint64_t watermark_ms;
    /* The single headline tile and the uid it belongs to.  RAM only. */
    char pic_uid[LMA_PB_EVENT_UID_MAX + 1];
    uint8_t pic_data[LMA_PB_PIC_TILE_BYTES];
    size_t pic_data_len;
} rd_cache_t;

/* Load config: config.h defaults, then any NVS keys that are present. */
void rd_config_load(rd_config_t *cfg);

void rd_cache_init(rd_cache_t *cache);

/* Load the persisted cache.  Clears it when the stored schema version does
 * not match RD_CACHE_SCHEMA (and rewrites the version). */
void rd_cache_load(rd_cache_t *cache);

/* Clear the in-RAM cache and erase its NVS blobs (kid re-assignment, schema
 * mismatch, or an explicit reset). */
void rd_cache_clear(rd_cache_t *cache);

/* Merge a reply into the cache: upsert by uid, drop tombstones, advance the
 * watermark, adopt the served headline tile when its uid is still the
 * headline, re-sort by start, then persist the non-image fields. */
void rd_cache_apply(rd_cache_t *cache, const lma_cal_bundle_t *bundle);

/* The uid of the first visible (ongoing-or-future) event, or NULL.  The
 * renderer draws the cached tile only while this matches cache->pic_uid. */
const char *rd_cache_headline_uid(const rd_cache_t *cache, uint64_t now_ms);

#ifdef __cplusplus
}
#endif

#endif /* RD_STORE_H */
