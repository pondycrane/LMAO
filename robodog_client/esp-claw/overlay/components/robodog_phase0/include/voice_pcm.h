/*
 * RoboDog ESP-Claw PCM silence trimmer (LMAO-owned, PURE C).
 *
 * The voice loop synthesizes the reply in sentence-sized chunks and plays them
 * back-to-back.  Qwen3-TTS pads each returned WAV with silence: a leading gap
 * (the model's own start latency) and a trailing gap (its stop token).  Played
 * verbatim, every chunk boundary becomes a double silence — the audible
 * "pause between response segments" that makes a chunked read-out sound
 * choppy instead of like one continuous answer.
 *
 * This module trims that padding so consecutive chunks butt up against each
 * other:
 *   - LEADING silence is trimmed entirely (except on the turn's FIRST chunk,
 *     where a small lead-in keeps the attack natural — the caller passes
 *     `lead_keep_ms` = 0 for later chunks).
 *   - TRAILING silence is trimmed down to `tail_keep_ms`, keeping a short tail
 *     so a final plosive/breath is not clipped.
 * Silence is detected on 16-bit samples with a fixed amplitude threshold and
 * a hold window, so a brief low-energy dip inside speech never splits the
 * chunk.  Pure C (no ESP-IDF, no allocation): the same source is compiled by
 * the firmware and by the Bazel host unit test.
 *
 * SPDX-License-Identifier: Apache-2.0
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* |sample| at or below this counts as silence (~ -54 dBFS on int16).  Speech
 * from the TTS endpoint is normalized loud; the noise floor of its padding is
 * exact digital silence, so the threshold only has to clear dither. */
#define ROBODOG_PCM_SILENCE_LEVEL 256

/**
 * @brief Trim leading/trailing silence from 16-bit mono PCM in place (by
 *        returning a window, never by moving samples).
 *
 * @param[in]  pcm            samples (mono, s16)
 * @param[in]  n              sample count
 * @param[in]  rate           sample rate in Hz (for the ms budgets)
 * @param[in]  lead_keep_ms   leading silence kept (0 = trim it all)
 * @param[in]  tail_keep_ms   trailing silence kept (0 = trim it all)
 * @param[out] out_off        index of the first kept sample
 * @param[out] out_n          number of kept samples
 *
 * A chunk that is ENTIRELY silence (or shorter than the hold window) is
 * returned unchanged — trimming must never turn a real (if quiet) chunk into
 * nothing to play.  The caller keeps ownership of `pcm`; `out_off`/`out_n`
 * describe the sub-window to play.
 */
void robodog_pcm_trim_silence(const int16_t *pcm, size_t n, uint32_t rate,
                              size_t lead_keep_ms, size_t tail_keep_ms,
                              size_t *out_off, size_t *out_n);

#ifdef __cplusplus
}
#endif
