/*
 * RoboDog overlay shared HTTP helper (LMAO-owned).
 *
 * Implements the growable full-body response accumulator declared in
 * robodog_http.h.  Extracted from robodog_overlay.c (rd_asr / rd_tts) so the
 * Phase 2 voice loop reuses the exact same buffering instead of duplicating
 * it.  SPIRAM-backed to keep multi-hundred-KB TTS bodies out of internal RAM.
 *
 * SPDX-License-Identifier: Apache-2.0
 */
#include "robodog_http.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_log.h"
#include "esp_heap_caps.h"

static const char *TAG = "robodog_http";

/* ── JSON escaping (single copy: TTS body + line-safe reply logs) ───────── */

char *robodog_json_escape(const char *s)
{
    if (s == NULL) {
        return NULL;
    }
    size_t cap = strlen(s) * 6 + 8;
    char *out = malloc(cap);
    if (out == NULL) {
        return NULL;
    }
    size_t o = 0;
    for (const char *p = s; *p != '\0'; p++) {
        unsigned char c = (unsigned char)*p;
        const char *esc = NULL;
        switch (c) {
        case '"':  esc = "\\\""; break;
        case '\\': esc = "\\\\"; break;
        case '\n': esc = "\\n";  break;
        case '\r': esc = "\\r";  break;
        case '\t': esc = "\\t";  break;
        default:   break;
        }
        if (esc != NULL) {
            size_t l = strlen(esc);
            memcpy(out + o, esc, l);
            o += l;
        } else if (c < 0x20) {
            o += (size_t)snprintf(out + o, cap - o, "\\u%04x", c);
        } else {
            out[o++] = (char)c;
        }
    }
    out[o] = '\0';
    return out;
}

/* ── RIFF/WAVE payload locator (single copy: ASR probe + TTS parse) ─────── */

size_t robodog_wav_pcm_offset(const uint8_t *wav, size_t len, uint32_t *out_rate)
{
    if (out_rate != NULL) {
        *out_rate = 0;
    }
    if (wav == NULL || len < 12 || memcmp(wav, "RIFF", 4) != 0 ||
        memcmp(wav + 8, "WAVE", 4) != 0) {
        return 0;
    }
    size_t o = 12;
    while (o + 8 <= len) {
        const uint32_t chunk_size =
            (uint32_t)wav[o + 4] | ((uint32_t)wav[o + 5] << 8) |
            ((uint32_t)wav[o + 6] << 16) | ((uint32_t)wav[o + 7] << 24);
        if (memcmp(wav + o, "data", 4) == 0) {
            return o + 8;
        }
        if (memcmp(wav + o, "fmt ", 4) == 0 && chunk_size >= 16 &&
            o + 24 <= len && out_rate != NULL) {
            *out_rate = (uint32_t)wav[o + 12] | ((uint32_t)wav[o + 13] << 8) |
                        ((uint32_t)wav[o + 14] << 16) |
                        ((uint32_t)wav[o + 15] << 24);
        }
        o += 8 + chunk_size + (chunk_size & 1u);
    }
    return 0;
}

esp_err_t robodog_http_event_handler(esp_http_client_event_t *evt)
{
    robodog_http_rx_t *rx = (robodog_http_rx_t *)evt->user_data;
    if (evt->event_id == HTTP_EVENT_ON_DATA && rx != NULL && evt->data_len > 0) {
        if (rx->len + evt->data_len + 1 > rx->cap) {
            const size_t newcap = rx->len + evt->data_len + 128;
            char *n = heap_caps_realloc(rx->buf, newcap,
                                        MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
            if (n == NULL) {
                ESP_LOGE(TAG, "response buffer grow failed (%u)",
                         (unsigned)newcap);
                return ESP_ERR_NO_MEM;
            }
            rx->buf = n;
            rx->cap = newcap;
        }
        memcpy(rx->buf + rx->len, evt->data, evt->data_len);
        rx->len += evt->data_len;
        rx->buf[rx->len] = '\0';
    }
    return ESP_OK;
}

void robodog_http_rx_init(robodog_http_rx_t *rx, size_t cap)
{
    memset(rx, 0, sizeof(*rx));
    rx->cap = cap;
    rx->buf = heap_caps_malloc(cap, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (rx->buf != NULL) {
        rx->buf[0] = '\0';
    }
}

void robodog_http_rx_free(robodog_http_rx_t *rx)
{
    if (rx->buf != NULL) {
        heap_caps_free(rx->buf);
    }
    memset(rx, 0, sizeof(*rx));
}
