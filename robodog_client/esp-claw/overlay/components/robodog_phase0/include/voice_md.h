/*
 * RoboDog ESP-Claw markdown -> speech-friendly text (LMAO-owned, PURE C).
 *
 * The esp-claw root agent replies in verbose multi-line markdown (headers
 * `**`, bullet lists, inline-code spans, tables, links).  Kokoro reads those
 * syntax markers literally — "asterisk asterisk" around every bold header —
 * so the TTS readout is painful to listen to (and the text-only panel shows
 * the same `**` glyph noise).  This module strips the markdown while keeping
 * the words, so the voice loop feeds clean prose to BOTH the TTS endpoint
 * and the RLCD.
 *
 * Pure C (no ESP-IDF): the same source is compiled by the device firmware
 * (robodog_voice.c via the robodog_phase0 component) and by the Bazel host
 * unit test `//robodog_client:test_voice_split` — same DRY pattern as
 * voice_split.c.
 *
 * SPDX-License-Identifier: Apache-2.0
 */
#pragma once

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief Strip markdown syntax to speech-friendly plain text.
 *
 * Removes emphasis markers (`**`/`*`/`__`/`_`), heading `#`, line bullets
 * (`- `, `* `, `+ `), blockquote `>`, backtick code spans/fences, strike
 * `~`, link `[text](url)`, and table-cell `|` — while keeping every word and
 * any punctuation that reads naturally (numbers like `1.` or a leading `-5`
 * are NOT list markers and pass through).  Whole-line constructs that carry
 * no speech (horizontal rules, table separators, code-fence delimiters) are
 * dropped, horizontal whitespace runs collapse to a single space, and blank
 * lines collapse (the chunk splitter still sees the surviving `\n`
 * paragraph breaks).
 *
 * @param[in]  src     NUL-terminated markdown source
 * @param[out] dst     output buffer for the plain text
 * @param[in]  dst_cap size of `dst` in bytes (incl. NUL); the output is
 *                     truncated (NUL-terminated) if it would not fit
 * @return characters written excluding the NUL terminator (<= dst_cap - 1)
 */
size_t robodog_voice_md_strip(const char *src, char *dst, size_t dst_cap);

#ifdef __cplusplus
}
#endif
