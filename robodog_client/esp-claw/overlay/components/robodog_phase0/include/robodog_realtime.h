/*
 * RoboDog Phase 3 — omni realtime voice path (vLLM-Omni OpenAI-Realtime).
 *
 * Why this exists: the Phase 2 loop is three serial HTTP calls, each of which
 * returns a WHOLE response before the next can start —
 *   Whisper ASR (4.2 s fixed + 0.67 x utterance)
 *   -> claw_core (waits for the full reply; no stream)
 *   -> Qwen3-TTS (TTFB == total, RTF ~ 1)
 * measured 24-32 s from end-of-speech to first audio on this board.
 *
 * An omni model (MiniCPM-o 4.5 duplex, or Qwen3-Omni turn-based) streams
 * transcript deltas AND PCM deltas from ONE session, so first audio lands
 * after a fraction of a second of generation.  The board streams its mic into
 * that session as it captures — there is no utterance upload at all.
 *
 * TWO PER-TURN SHAPES, chosen by the caller (esp-claw decides, per turn):
 *   OMNI  — response.create with modalities ["audio","text"]: the model
 *           answers the turn itself and we play its audio.  Fastest; the
 *           esp-claw skills/MCP/tools loop is NOT in this path.
 *   AGENT — response.create with modalities ["text"]: we get the transcript
 *           and no speech, then claw_core answers (skills, MCP, web search,
 *           self-programming) and Qwen3-TTS speaks the reply.  Full
 *           capability, slower.
 * AUTO picks between them with a capability-keyword router (Kconfig-tunable).
 *
 * Deliberately device-side and standalone: no bridge, no host process.  The
 * model server is an endpoint with the same status as the Whisper/Qwen3-TTS
 * services it replaces.
 *
 * Half-duplex (mic is not read while audio plays): this board's codec pair has
 * no AEC (`esp_audio_effects` ships ALC/DRC/EQ/mixer/rate-cvt, no echo
 * canceller), so barge-in is deferred exactly as in Phase 2.
 *
 * SPDX-License-Identifier: Apache-2.0
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/** Per-turn shape.  AUTO resolves via the keyword router. */
typedef enum {
    ROBODOG_RT_MODE_AUTO = 0,   /**< router decides (see Kconfig) */
    ROBODOG_RT_MODE_OMNI = 1,   /**< omni answers + speaks the turn */
    ROBODOG_RT_MODE_AGENT = 2,  /**< omni transcribes; claw_core answers */
} robodog_rt_mode_t;

/** What one turn produced.  Fixed buffers: no per-turn heap churn. */
typedef struct {
    char transcript[512];      /**< what the user said (model-produced) */
    char reply[1024];          /**< omni's own reply text (OMNI mode) */
    uint32_t first_audio_ms;   /**< commit -> first audio delta (0 = none) */
    bool     spoke;            /**< true when omni audio was played */
    bool     fault;            /**< a real fault (never "user was silent") */
} robodog_rt_turn_t;

/**
 * @brief Create the playback task + event primitives.  No network yet.
 *        Called once from robodog_init.
 */
esp_err_t robodog_rt_init(void);

/**
 * @brief Open the WebSocket and complete the session.update/session.created
 *        handshake.  Idempotent: a live session is reused.  Logs
 *        `RT session=<id> caps=...` on success.
 */
esp_err_t robodog_rt_connect(void);

/** @brief Close the session (idempotent). */
void robodog_rt_disconnect(void);

/** @brief True while the session handshake has completed. */
bool robodog_rt_connected(void);

/**
 * @brief Run ONE turn: stream the mic into the session as it is captured,
 *        commit at end-of-utterance, request the response for `mode`, and
 *        (OMNI) play the returned audio through the shared mixer track.
 *
 * Connects on demand.  `out` is fully reset on entry.  Returns ESP_OK when the
 * turn reached response.done; ESP_ERR_TIMEOUT on a wedged session; ESP_FAIL on
 * a session/protocol error.  A turn with no speech returns ESP_OK with an
 * empty transcript (the caller decides whether that is idle or an error).
 *
 * @param[in]  mode  OMNI / AGENT / AUTO
 * @param[out] out   transcript + reply + timing
 */
esp_err_t robodog_rt_turn(robodog_rt_mode_t mode, robodog_rt_turn_t *out);

/**
 * @brief Resolve AUTO to OMNI or AGENT for `text` (the capability router).
 *        Exposed so the E2E can prove the routing decision from the console
 *        (`RT route=<omni|agent> reason=<keyword|default|forced>`).
 */
robodog_rt_mode_t robodog_rt_route(const char *text, const char **out_reason);

/** @brief Console: `rd_rt` probes the session (connect + log), no turn. */
esp_err_t robodog_rt_register_commands(void);

#ifdef __cplusplus
}
#endif
