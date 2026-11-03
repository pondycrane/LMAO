/*
 * RoboDog ESP-Claw UTF-8 helpers (LMAO-owned, PURE C).
 *
 * The RLCD text path was ASCII-only (non-ASCII codepoints collapsed to '?'),
 * which turned every Traditional Chinese reply into a row of question marks.
 * The display now renders with a generated Noto Sans CJK TC font
 * (gen_font_tc_16.sh), so the text path must keep UTF-8 intact and only
 * replace codepoints the font cannot draw.
 *
 * Pure C with NO ESP-IDF / LVGL dependencies: the same source runs on the
 * device (robodog_ui.c, voice_split.c, voice_transcript.c,
 * robodog_session.c) and in the Bazel host unit test
 * `//robodog_client:test_voice_split`, so the byte-boundary invariants the
 * panel/TTS path relies on are proven on the host against the exact bits
 * that run on the board (DRY, no port).
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

/* Returned in *cp when the byte sequence at the cursor is not valid UTF-8. */
#define ROBODOG_UTF8_INVALID 0xFFFDu

/**
 * @brief Decode one UTF-8 codepoint.
 *
 * @param[in]  s    start of the sequence
 * @param[in]  len  bytes available
 * @param[out] cp   codepoint (ROBODOG_UTF8_INVALID on malformed input);
 *                  may be NULL
 * @return bytes consumed (1..4), or 0 when the input at `s` is malformed
 *         (callers advance by 1 byte and substitute a replacement glyph).
 */
size_t robodog_utf8_decode(const char *s, size_t len, uint32_t *cp);

/**
 * @brief Length of the sequence introduced by a lead byte.
 * @return 1 for ASCII, 2..4 for a valid multi-byte lead, 0 for an invalid
 *         lead (continuation byte / 0xF8..0xFF).
 */
size_t robodog_utf8_seq_len(unsigned char lead);

/**
 * @brief Largest index j <= i that starts a codepoint (never splits a
 *        multi-byte sequence).  Returns 0 when no boundary precedes i.
 */
size_t robodog_utf8_prev_boundary(const char *s, size_t i);

/**
 * @brief Smallest index j >= i that starts a codepoint.  Returns strlen(s)
 *        when i is at/after the end.
 */
size_t robodog_utf8_next_boundary(const char *s, size_t i);

/**
 * @brief True when the generated RLCD font has a glyph slot for `cp`.
 *
 * Mirrors the glyph ranges rasterised by gen_font_tc_16.sh (ASCII, general +
 * CJK punctuation, Bopomofo, CJK Unified Ideographs incl. Extension A,
 * compatibility ideographs and fullwidth forms).  The ranges are a superset
 * of the OTF's coverage; the handful of unassigned holes draw LVGL's
 * placeholder box.
 */
bool robodog_utf8_glyph_supported(uint32_t cp);

/**
 * @brief Copy `src` to `dst`, replacing anything the RLCD font cannot draw
 *        with `replace`.
 *
 * Valid, drawable codepoints are copied byte-for-byte (never split).  Invalid
 * bytes and undrawable codepoints (emoji, exotic scripts, ...) become one
 * `replace` byte each.  Control characters other than '\n' and '\t' are
 * dropped; CRLF's '\r' is dropped.  The result is always NUL-terminated and
 * never exceeds `dst_cap` bytes.
 *
 * @return the written length (strlen(dst)).
 */
size_t robodog_utf8_sanitize(char *dst, size_t dst_cap, const char *src,
                             char replace);

/**
 * @brief Count codepoints in a NUL-terminated UTF-8 string (each malformed
 *        byte counts as one).
 */
size_t robodog_utf8_strlen(const char *s);

/**
 * @brief True when `s` contains at least one CJK ideograph (Unified, ext. A
 *        or compatibility), i.e. the text is (at least partly) Chinese.
 *
 * Used to pick the reply-language directive when the ASR endpoint does not
 * report a language tag: the voice turn must answer a Chinese utterance in
 * Chinese, not English.
 */
bool robodog_utf8_has_cjk(const char *s);

#ifdef __cplusplus
}
#endif
