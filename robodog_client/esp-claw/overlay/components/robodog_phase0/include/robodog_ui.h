/*
 * RoboDog Phase 1 text-only RLCD chat UI (LMAO-owned).
 *
 * Brings the RLCD panel + LVGL up deterministically (esp-claw's own
 * display_service/system_ui are gated behind a Kconfig symbol the board
 * manager silently drops for this custom-typed panel, so normally nothing
 * initialises the display), then renders PR #278-style text-only parity:
 * no face/emote, ~16 px pure-black ASCII font on the white panel (the LVGL
 * theme default grey binarizes to thin greys on the 1-bpp reflective panel,
 * so the labels force lv_color_black()), left-aligned full-panel multiline
 * wrap, status bar (WiFi/agent state) on top.
 *
 * Roles/timing mirror today's SetChatMessage("user"|"assistant", ...):
 *   - robodog_ui_show_user     - render the user transcript (ASR completion)
 *   - robodog_ui_show_assistant - render the assistant reply as it finalizes
 *
 * SPDX-License-Identifier: Apache-2.0
 */
#pragma once

#include <stddef.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief Start the text-only UI: bring up the panel + LVGL adapter, build the
 *        chat screen and register the status-update timer.  Safe to call once
 *        from robodog_init (spawns its own task; returns immediately).
 */
esp_err_t robodog_ui_start(void);

/**
 * @brief Render/append the user transcript.  Starts a new turn: the body is
 *        scrolled so the fresh user line is visible and the agent state is
 *        set to "thinking" until an assistant reply finalizes.
 */
void robodog_ui_show_user(const char *text);

/**
 * @brief Render/append the assistant reply.  ASCII-sanitized; clears the
 *        in-turn "thinking" state.  Stale-caption rule: only ever appends the
 *        reply that just finalized for the current turn (never replays an old
 *        one) - the response must be passed from the current run.
 */
void robodog_ui_show_assistant(const char *text);

/**
 * @brief Set the live agent/voice state shown in the top-right status
 *        indicator without a text render.  state is a short ASCII label:
 *        "idle" / "listening" / "processing" / "responding" / "error".
 */
void robodog_ui_set_agent_state(const char *state);

#ifdef __cplusplus
}
#endif
