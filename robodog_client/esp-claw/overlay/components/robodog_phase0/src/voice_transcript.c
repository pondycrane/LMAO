/*
 * RoboDog ESP-Claw transcript accumulation helper (LMAO-owned, PURE C).
 *
 * Extracted from the UI_CMD_ASSISTANT_APPEND case of robodog_ui.c so the join
 * + cap-keep step has a host-test boundary (R7).  See voice_transcript.h for
 * the contract.  Portable: no strlcat (a plain host toolchain need not provide
 * the BSD/glibc extension), only strlen / memcpy / memmove.
 *
 * UTF-8: the buffer holds CJK text, so neither the tail truncation (buffer
 * full) nor the drop-oldest head trim may cut a multi-byte sequence — both
 * are snapped to codepoint boundaries.
 *
 * SPDX-License-Identifier: Apache-2.0
 */
#include "voice_transcript.h"

#include <string.h>

#include "robodog_utf8.h"

void robodog_transcript_append_chunk(char *buf, size_t buf_size,
                                     size_t keep_cap, const char *chunk)
{
    if (buf_size == 0) {
        return;
    }
    size_t len = strlen(buf);
    const size_t clen = strlen(chunk);

    /* Single-space join: only where neither side already provides one. */
    if (len > 0 && chunk[0] != '\n' && chunk[0] != ' ' && buf[len - 1] != '\n') {
        if (len + 1 < buf_size) {
            buf[len] = ' ';
            len++;
            buf[len] = '\0';
        }
    }
    /* Append the chunk, bounded by buf_size-1. */
    const size_t room = buf_size - 1 - len;
    size_t take = clen < room ? clen : room;
    if (take < clen) {
        take = robodog_utf8_prev_boundary(chunk, take); /* never split a char */
    }
    memcpy(buf + len, chunk, take);
    len += take;
    buf[len] = '\0';

    /* drop-oldest: keep only the most-recent keep_cap bytes so a monster
     * reply can never roll the chunk being read off the buffer. */
    if (len > keep_cap) {
        size_t off = robodog_utf8_next_boundary(buf, len - keep_cap);
        const size_t kept = len - off;
        memmove(buf, buf + off, kept);
        buf[kept] = '\0';
    }
}
