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

#include <string.h>

#include "esp_log.h"
#include "esp_heap_caps.h"

static const char *TAG = "robodog_http";

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
