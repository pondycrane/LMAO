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

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief Split `reply` into sentence-bounded chunks for the TTS queue.
 *
 * Each chunk is at most `cap` characters, cut at the first `.`/`!`/`?`/`;`
 * or newline break within the window (else the last space, else a hard
 * cutoff), with trailing whitespace stripped and the leading whitespace of
 * the next window skipped.  Chunks are written back-to-back as NUL-terminated
 * strings into `arena`; `offsets[i]` holds the byte offset of chunk i.
 *
 * @param[in]  reply       the full agent reply (NUL-terminated)
 * @param[in]  cap         max characters per chunk
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
                                 char *arena, size_t arena_cap,
                                 size_t *offsets, size_t max_chunks,
                                 bool *truncated);

#ifdef __cplusplus
}
#endif
