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

#include "esp_err.h"

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

#ifdef __cplusplus
}
#endif
