/*
 * RoboDog Phase 0 overlay API (LMAO-owned).
 *
 * DESIGN.md Phase 0 (spike): ESP-Claw builds for the board, the ST7305 shows
 * LVGL text (verified pixel-for-pixel via the RD_SHOT capture hook), and the
 * board proves its device-initiated HTTP round-trips to the user's model
 * endpoints (Whisper ASR, OpenAI-style LLM via the native claw_agent_mgr
 * chat-injection seam, Kokoro TTS).
 *
 * SPDX-License-Identifier: Apache-2.0
 */
#pragma once

#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief One-time init, called from app_main (after WiFi + HTTP + agent are
 *        up). Allocates the capture buffers, starts the RD_SHOT publisher task
 *        and registers the `rdshot` / `rd_asr` / `rd_tts` / `rd_chat` console
 *        commands.
 */
esp_err_t robodog_init(void);

/**
 * @brief Called from the board's SPI LCD flush path (patched setup_device.c)
 *        with the freshly flushed 1-bpp ST7305 panel memory.  Copies the frame
 *        (only when a publish is due) and wakes the RD_SHOT publisher task.
 */
void robodog_rdshot_publish(const uint8_t *buf, size_t len, int w, int h);

#ifdef __cplusplus
}
#endif
