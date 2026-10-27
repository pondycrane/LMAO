/*
 * RoboDog ESP-Claw whole-reply chunk splitter (LMAO-owned, PURE C).
 *
 * Extracted from robodog_voice.c so the same source is host-testable under
 * Bazel (R6: the voice-verify gate's whole-reply coverage claim must not
 * depend on an untested static function).  Pure C only — no esp_log, no
 * Kconfig symbols; the caller passes the per-chunk `cap` and the `max_chunks`
 * bound explicitly.  See voice_split.h for the contract.
 *
 * Truncation contract (R3): when the reply needs more than `max_chunks`
 * chunks, the loop stops and the caller is told via `*truncated` so the
 * terminal voice state can report the degradation instead of ending in a
 * false-complete "idle" — the device NEVER silently pretends a tail-dropped
 * reply was fully read out.
 *
 * SPDX-License-Identifier: Apache-2.0
 */
#include "voice_split.h"

#include <string.h>

size_t robodog_voice_split_reply(const char *reply, size_t cap,
                                 char *arena, size_t arena_cap,
                                 size_t *offsets, size_t max_chunks,
                                 bool *truncated)
{
    const size_t len = strlen(reply);
    size_t pos = 0, n = 0, aoff = 0;

    while (pos < len && n < max_chunks) {
        const size_t remain = len - pos;
        const size_t win = remain < cap ? remain : cap;

        /* Preferred: a sentence/line break at or before the window end. */
        size_t cut = 0;
        for (size_t i = win; i > 0 && cut == 0; i--) {
            const char c = reply[pos + i];
            if (c == '.' || c == '!' || c == '?' || c == ';' || c == '\n') {
                cut = i + 1; /* include the break char */
            }
        }
        if (cut > win) {
            cut = win; /* break char sat exactly at the cap: keep in-bounds */
        }
        /* Fall back to the last space in the window. */
        if (cut == 0) {
            for (size_t i = win; i > 0 && cut == 0; i--) {
                if (reply[pos + i] == ' ') {
                    cut = i;
                }
            }
        }
        if (cut == 0) {
            cut = win; /* hard cutoff (no break char/space in the window) */
        }
        /* Strip trailing spaces so the queued chunk is clean prose. */
        while (cut > 0 && (reply[pos + cut - 1] == ' ' ||
                           reply[pos + cut - 1] == '\t')) {
            cut--;
        }
        /* Skip leading whitespace of the next window so chunks don't start
         * blank (a chunk cut at '\n' already leaves the newline behind). */
        size_t next = pos + cut;
        while (next < len && (reply[next] == ' ' || reply[next] == '\t' ||
                              reply[next] == '\n')) {
            next++;
        }

        if (aoff + cut + 1 > arena_cap) {
            break; /* arena sized from max_chunks must always fit; guard only */
        }
        memcpy(arena + aoff, reply + pos, cut);
        arena[aoff + cut] = '\0';
        offsets[n] = aoff;
        aoff += cut + 1;
        n++;

        pos = next;
    }
    if (truncated != NULL) {
        *truncated = (pos < len);
    }
    return n;
}
