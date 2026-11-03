/*
 * RoboDog ESP-Claw PCM silence trimmer (LMAO-owned, PURE C).
 *
 * See voice_pcm.h for the contract.  Extracted as a standalone pure-C unit so
 * the boundary trimming the chunked voice loop relies on is host-testable
 * under Bazel against the exact bits the firmware runs (DRY, same pattern as
 * voice_split.c / voice_md.c / voice_pipeline.c).
 *
 * SPDX-License-Identifier: Apache-2.0
 */
#include "voice_pcm.h"

/* Consecutive non-silent samples required to call a region speech: a single
 * click or a zero crossing inside a word must not end the trim. */
#define SPEECH_HOLD_SAMPLES 32

static bool is_silent(int16_t v)
{
    const int32_t a = (v < 0) ? -(int32_t)v : (int32_t)v;
    return a <= ROBODOG_PCM_SILENCE_LEVEL;
}

static size_t ms_to_samples(uint32_t rate, size_t ms)
{
    return (size_t)(((uint64_t)rate * ms) / 1000u);
}

void robodog_pcm_trim_silence(const int16_t *pcm, size_t n, uint32_t rate,
                              size_t lead_keep_ms, size_t tail_keep_ms,
                              size_t *out_off, size_t *out_n)
{
    size_t off = 0;
    if (out_off == NULL || out_n == NULL) {
        return;
    }
    *out_off = 0;
    *out_n = n;
    if (pcm == NULL || n == 0 || rate == 0) {
        return;
    }

    /* First sample that begins a run of non-silence. */
    size_t first_speech = n;
    size_t run = 0;
    for (size_t i = 0; i < n; i++) {
        if (is_silent(pcm[i])) {
            run = 0;
            continue;
        }
        if (++run >= SPEECH_HOLD_SAMPLES) {
            first_speech = i + 1 - run;
            break;
        }
    }
    if (first_speech == n) {
        return; /* entirely silent / no speech run: leave the chunk as-is */
    }

    /* Last sample that ends a run of non-silence. */
    size_t last_speech = first_speech;
    run = 0;
    for (size_t i = n; i-- > 0;) {
        if (is_silent(pcm[i])) {
            run = 0;
            continue;
        }
        if (++run >= SPEECH_HOLD_SAMPLES) {
            last_speech = i + run - 1;
            break;
        }
    }

    const size_t lead_keep = ms_to_samples(rate, lead_keep_ms);
    const size_t tail_keep = ms_to_samples(rate, tail_keep_ms);
    /* Keep `lead_keep` of the leading silence, but never trim past the
     * speech start (a chunk whose lead-in is shorter than the budget keeps
     * all of it). */
    off = (first_speech > lead_keep) ? (first_speech - lead_keep) : 0;
    size_t end = last_speech + 1 + tail_keep;
    if (end > n) {
        end = n;
    }
    if (end <= off) {
        return; /* degenerate: never return an empty window */
    }
    *out_off = off;
    *out_n = end - off;
}
