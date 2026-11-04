/*
 * RoboDog ESP-Claw whole-reply chunk splitter (LMAO-owned, PURE C).
 *
 * The esp-claw root agent is verbose by default (a full "what can you do" dump
 * is ~60 s of speech), so the FULL agent reply is split at sentence/line
 * breaks into a queue of chunks each at most `cap` chars, spoken back-to-back
 * in order — the read-out voice finishes every word of the reply.
 *
 * This module is deliberately a standalone pure-C unit with NO ESP-IDF
 * dependencies (no esp_log, no Kconfig): the same source is compiled both by
 * the device firmware (robodog_voice.c via the robodog_phase0 component) and
 * by the Bazel host unit test `//robodog_client:test_voice_split`, so the
 * lossless/ordered/≤cap invariants the voice-verify gate relies on are proven
 * on the host against the exact bits that run on the board (DRY, no port).
 *
 * SPDX-License-Identifier: Apache-2.0
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>

/* Absolute byte ceiling for ONE chunk.  Two independent bounds meet here:
 *   - the RLCD UI command field is `char text[480]` (robodog_ui.c), so a chunk
 *     plus its NUL must never exceed 479 bytes or the on-screen sentence would
 *     be truncated relative to the one the voice reads (R5);
 *   - the per-slot TTS response buffer (CONFIG_ROBODOG_VOICE_TTS_MAX_RESPONSE,
 *     384 KB = ~8.2 s of 24 kHz s16) must hold the whole WAV, so a chunk must
 *     stay well under ~8 s of speech — the caller passes that tighter ceiling
 *     as `cap + overflow`.
 * `cap` is a PREFERRED budget, not a hard one: the splitter may run past it to
 * reach a sentence ender, but never past the ceiling it is given. */
#define ROBODOG_VOICE_SPLIT_MAX_BYTES 479

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief Split `reply` into sentence-bounded chunks for the TTS queue.
 *
 * Cuts are SENTENCE-ALIGNED within a bounded overflow.  `cap` is a preferred
 * budget: the cut lands after the last `.`/`!`/`?`/`;` — or a CJK sentence
 * break (。！？；…) — that fits inside it.  When the window ends mid-sentence
 * the splitter runs on to the FIRST sentence ender past `cap`, but only up to
 * `ROBODOG_VOICE_SPLIT_MAX_BYTES` — in practice the caller passes the
 * per-slot WAV buffer bound, so a chunk always fits one slot.  A sentence that
 * cannot finish inside that window falls back to a CLAUSE break (`,` `，` `、`
 * `：` `:`), which is a natural pause rather than a mid-clause cut, then to a
 * space, then to a hard cut at `cap`.
 * A newline is only a WEAK break: it is used when the reply carries no
 * sentence punctuation at all (a stripped bullet list), never in preference
 * to a real sentence end — treating it as one cut replies at the agent's
 * line wrapping, which is what made the read-out sound broken.  Only
 * genuinely unpunctuated text falls back to a word boundary inside the
 * budget, then to a hard cut at `budget` (codepoint-aligned).
 *
 * A chunk therefore fits `ROBODOG_VOICE_SPLIT_MAX_BYTES` bytes, not `cap`;
 * `arena` must be sized from the ceiling (see the caller).  Trailing
 * whitespace is stripped and the leading whitespace of the next window is
 * skipped, so no chunk starts or ends blank.  The cut always lands on a UTF-8
 * codepoint boundary, so a Traditional Chinese reply is never split
 * mid-character.  Chunks are written back-to-back as NUL-terminated strings
 * into `arena`; `offsets[i]` holds the byte offset of chunk i.
 *
 * @param[in]  reply       the full agent reply (NUL-terminated)
 * @param[in]  cap         PREFERRED bytes per chunk (soft: a chunk may run
 *                         past it to reach a sentence ender)
 * @param[in]  ceiling     HARD byte limit for one chunk — the caller passes
 *                         `cap + overflow`, sized so the chunk's WAV fits one
 *                         TTS pipeline slot (and never above
 *                         ROBODOG_VOICE_SPLIT_MAX_BYTES, the RLCD field)
 * @param[in]  first_cap   preferred bytes for the FIRST chunk only (0 = use
 *                         `cap`).  The first chunk's synthesis is the whole
 *                         time-to-first-audio, so a smaller first budget makes
 *                         the reply start speaking sooner when the opening
 *                         sentence is short; it never forces a mid-sentence
 *                         cut.
 * @param[out] arena       output buffer for the NUL-terminated chunk strings
 * @param[in]  arena_cap   size of `arena` in bytes
 * @param[out] offsets     receives the byte offset of each chunk; must hold at
 *                         least `max_chunks` size_t entries
 * @param[in]  max_chunks  hard cap on the queue length
 * @param[out] truncated   set to true when `reply` has trailing text past
 *                         what `max_chunks` chunks can carry (the tail was
 *                         dropped and the reply was NOT fully read out);
 *                         unchanged on a complete split
 * @return the chunk count n (0 <= n <= max_chunks).  When `reply` is blank,
 *         n is 0 and nothing is written.
 */
size_t robodog_voice_split_reply(const char *reply, size_t cap,
                                 size_t ceiling, size_t first_cap,
                                 char *arena, size_t arena_cap,
                                 size_t *offsets, size_t max_chunks,
                                 bool *truncated);

#ifdef __cplusplus
}
#endif
