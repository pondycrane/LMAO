/*
 * RoboDog Phase 2 voice loop (LMAO-owned).
 *
 * DESIGN.md Phase 2: the on-device, all-device-initiated voice loop —
 *   RD_LISTEN trigger -> ES7210 mic capture (16 kHz s16 mono via the shared
 *   audio_hub APP subscriber) -> energy-VAD endpoint -> Whisper ASR ->
 *   claw_agent_mgr LLM run -> Kokoro TTS -> ES8311 speaker (shared APP track).
 * The reply is rendered stale-caption-safe through the Phase 1 text-only UI
 * (robodog_ui_show_user / robodog_ui_show_assistant) and the transcript is
 * logged to the console so the host E2E can assert each leg.
 *
 * Half-duplex by construction: the mic subscriber is opened only for the
 * listen phase and closed before TTS playback, so the mic is never read into
 * the speaker path (no AEC/barge-in in v1 — DESIGN defers that).
 *
 * SPDX-License-Identifier: Apache-2.0
 */
#pragma once

#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"
#include "robodog_http.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief Start the voice loop: create the turn event group + task and register
 *        the `rd_listen` console command.  Safe to call once from
 *        robodog_init (spawns its task; returns immediately).
 */
esp_err_t robodog_voice_init(void);

/**
 * @brief Trigger one listen-to-reply voice turn (thread-safe).  Called by the
 *        `rd_listen` console command; a no-op while a turn is already running.
 */
void robodog_voice_listen_start(void);

/**
 * @brief POST captured 16 kHz s16 mono PCM to the Whisper endpoint and return
 *        the transcript (heap-allocated, caller frees) or NULL on failure /
 *        OOM.  The single copy of the device->ASR path — the `rd_asr` probe
 *        and the voice turn both call this.
 */
char *robodog_voice_asr(const int16_t *pcm, size_t samples);

/**
 * @brief Fetch a TTS WAV for `reply` from the Kokoro endpoint and play it
 *        through the ES8311 (speaker PA + APP mixer track, paced real-time
 *        via mixer_write_paced).  The single copy of the device->TTS->speaker
 *        path; voice_turn calls this once per queued sentence chunk.
 * @return ESP_OK on a fully-played reply; an esp_err otherwise.
 */
esp_err_t robodog_voice_speak(const char *reply);

/**
 * @brief POST `reply` to the Kokoro endpoint and buffer the returned WAV in
 *        `rx` WITHOUT playing it (the `rd_tts` probe's endpoint round-trip,
 *        no speaker needed — isolates ASR/TTS HTTP from the audio path).
 *        Single copy of the Kokoro request body + retry/error handling shared
 *        with robodog_voice_speak.  `rx` MUST already be initialised; the
 *        caller owns it on every return.
 */
esp_err_t robodog_voice_tts_fetch(const char *reply, robodog_http_rx_t *rx);

#ifdef __cplusplus
}
#endif
