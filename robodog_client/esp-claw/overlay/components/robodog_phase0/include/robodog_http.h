/*
 * RoboDog overlay shared HTTP helper (LMAO-owned).
 *
 * A minimal full-body buffering wrapper around esp_http_client used by the
 * ASR/TTS probes (rd_asr / rd_tts) and the Phase 2 voice loop.  The whole
 * response is accumulated into a (re)growable SPIRAM buffer so callers can
 * parse the body (cJSON for ASR/JSON, RIFF scan + resample for TTS WAV).
 * DRY: single copy, shared by robodog_phase0.c's probes and robodog_voice.c.
 * Also the single home of the JSON escaper and the RIFF/WAVE payload locator
 * (both used by the voice loop and the probes).
 *
 * SPDX-License-Identifier: Apache-2.0
 */
#pragma once

#include <stddef.h>
#include <stdint.h>

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

/**
 * @brief JSON-escape a NUL-terminated string for embedding inside a JSON
 *        string value (quotes, backslashes, newlines and control chars become
 *        backslash escapes).  Returns a heap-allocated escaped copy the caller
 *        must free(), or NULL on OOM.
 *        Single copy of record: used by the TTS request body (robodog_voice.c)
 *        and by the line-safe console reply log (voice + rd_chat) so a raw
 *        "%s" never truncates a multi-line agent reply on the E2E hook.
 */
char *robodog_json_escape(const char *s);

/**
 * @brief Locate the PCM payload of a RIFF/WAVE blob (robust to non-44-byte
 *        headers) and, in the same pass, read the sample rate declared in the
 *        fmt chunk.  Returns the byte offset of the `data` chunk payload, or 0
 *        when the blob is not a RIFF/WAVE file or has no data chunk.
 * @param[out] out_rate  the declared sample rate (0 when the fmt chunk is
 *                       absent/garbage); may be NULL to ignore.
 */
size_t robodog_wav_pcm_offset(const uint8_t *wav, size_t len, uint32_t *out_rate);

#ifdef __cplusplus
}
#endif
