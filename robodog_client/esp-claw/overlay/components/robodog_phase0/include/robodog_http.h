/*
 * RoboDog overlay shared HTTP helper (LMAO-owned).
 *
 * A minimal full-body buffering wrapper around esp_http_client used by the
 * ASR/TTS probes (rd_asr / rd_tts) and the Phase 2 voice loop.  The whole
 * response is accumulated into a (re)growable SPIRAM buffer so callers can
 * parse the body (cJSON for ASR/JSON, RIFF scan + resample for TTS WAV).
 * DRY: single copy, shared by robodog_overlay.c's probes and robodog_voice.c.
 *
 * SPDX-License-Identifier: Apache-2.0
 */
#pragma once

#include <stddef.h>

#include "esp_err.h"
#include "esp_http_client.h"

#ifdef __cplusplus
extern "C" {
#endif

/** Growable response accumulator fed by robodog_http_event_handler. */
typedef struct {
    char *buf;      /* SPIRAM-backed, NUL-terminated after each append */
    size_t len;     /* bytes copied so far */
    size_t cap;     /* allocated capacity */
    int status;     /* HTTP status code (set by caller after perform) */
} robodog_http_rx_t;

/**
 * @brief esp_http_client event handler appending HTTP_EVENT_ON_DATA into the
 *        robodog_http_rx_t passed as user_data.  Grows the buffer in SPIRAM.
 */
esp_err_t robodog_http_event_handler(esp_http_client_event_t *evt);

/**
 * @brief (Re)initialise a response accumulator with `cap` initial SPIRAM bytes.
 *        The buffer is allocated lazily-safe: NULL means "allocation failed".
 */
void robodog_http_rx_init(robodog_http_rx_t *rx, size_t cap);

/** Free the accumulator buffer and reset the struct. */
void robodog_http_rx_free(robodog_http_rx_t *rx);

#ifdef __cplusplus
}
#endif
