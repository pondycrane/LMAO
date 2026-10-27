/*
 * RoboDog ESP-Claw transcript accumulation helper (LMAO-owned, PURE C).
 *
 * The chunked voice queue grows the on-screen assistant reply one sentence
 * chunk at a time; the panel must show the SAME sentence the voice is reading
 * (DESIGN.md I1).  This tiny state-mutation (single-space join + drop-oldest
 * cap-keep) is extracted out of robodog_ui.c so it is host-testable under
 * Bazel against the exact bits the firmware runs (R7 — the display/audio sync
 * must not regress silently between hardware E2E runs).
 *
 * Pure C only — no ESP-IDF dependencies, no strlcat (not on a plain host
 * toolchain); the join/copy/trim below use only strlen/memcpy/memmove.
 *
 * SPDX-License-Identifier: Apache-2.0
 */
#pragma once

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief Append one sentence chunk to an accumulated reply with the chunked-
 *        voice join + cap-keep rules:
 *          - single-space join: a space is inserted between the existing text
 *            and the chunk only when neither side already provides one (chunks
 *            cut at punctuation don't need it; a chunk starting with a space
 *            or newline, or a buffer ending in a newline, is appended as-is);
 *          - drop-oldest: after the append, only the most-recent `keep_cap`
 *            characters are kept, so a huge reply can never roll the chunk
 *            currently being read off the buffer.
 * @param[in,out] buf       NUL-terminated accumulation buffer
 * @param[in]     buf_size  allocated size of `buf` in bytes (>= 1)
 * @param[in]     keep_cap  max chars retained after the append (<= buf_size-1)
 * @param[in]     chunk     the sentence chunk to append (NUL-terminated)
 */
void robodog_transcript_append_chunk(char *buf, size_t buf_size,
                                     size_t keep_cap, const char *chunk);

#ifdef __cplusplus
}
#endif
