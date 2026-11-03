/*
 * RoboDog ESP-Claw whole-reply chunk splitter (LMAO-owned, PURE C).
 *
 * Extracted from robodog_voice.c so the same source is host-testable under
 * Bazel (R6: the voice-verify gate's whole-reply coverage claim must not
 * depend on an untested static function).  Pure C only — no esp_log, no
 * Kconfig symbols; the caller passes the per-chunk `cap` and the `max_chunks`
 * bound explicitly.  See voice_split.h for the contract.
 *
 * UTF-8: `cap` is a BYTE budget (the UI command field and the TTS request are
 * byte-sized), so a chunk is never cut inside a multi-byte sequence — the cut
 * is always snapped back to a codepoint boundary.  Sentence breaks include
 * the CJK punctuation a Traditional Chinese reply uses (。！？；…), so Chinese
 * prose is still split at sentence ends rather than at an arbitrary byte.
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

#include "robodog_utf8.h"

/* A chunk ends AFTER one of these (the break char stays in the chunk). */
static bool is_sentence_break(uint32_t cp)
{
    return cp == '.' || cp == '!' || cp == '?' || cp == ';' || cp == '\n' ||
           cp == 0x3002 || /* 。 ideographic full stop */
           cp == 0xFF01 || /* ！ fullwidth exclamation */
           cp == 0xFF1F || /* ？ fullwidth question */
           cp == 0xFF1B || /* ； fullwidth semicolon */
           cp == 0x2026;   /* … ellipsis (Chinese sentences often end so) */
}

size_t robodog_voice_split_reply(const char *reply, size_t cap,
                                 size_t first_cap, char *arena,
                                 size_t arena_cap, size_t *offsets,
                                 size_t max_chunks, bool *truncated)
{
    const size_t len = strlen(reply);
    size_t pos = 0, n = 0, aoff = 0;

    while (pos < len && n < max_chunks) {
        const size_t remain = len - pos;
        /* The FIRST chunk may use a tighter budget so the speaker starts
         * sooner: synthesis of chunk 1 is the entire time-to-first-audio. */
        size_t budget = cap;
        if (n == 0 && first_cap != 0 && first_cap < budget) {
            budget = first_cap;
        }
        if (budget == 0) {
            budget = 1;
        }
        const size_t win = remain < budget ? remain : budget;

        /* Walk whole codepoints while they fit the byte budget, remembering
         * the last sentence break and the last space (cut BEFORE a space). */
        size_t end = 0;
        size_t last_break = 0;
        size_t last_space = 0;
        while (end < win) {
            uint32_t cp = ROBODOG_UTF8_INVALID;
            size_t clen = robodog_utf8_decode(reply + pos + end, remain - end,
                                              &cp);
            if (clen == 0) {
                clen = 1; /* malformed byte: treat as one replacement char */
            }
            if (end + clen > win) {
                break; /* this codepoint would split across the budget */
            }
            end += clen;
            if (is_sentence_break(cp)) {
                last_break = end;
            } else if (cp == ' ') {
                last_space = end - 1;
            }
        }
        if (end == 0) {
            /* cap is smaller than the first codepoint: emit it whole rather
             * than loop forever. */
            const size_t clen =
                robodog_utf8_decode(reply + pos, remain, NULL);
            end = clen > 0 ? clen : 1;
            last_break = end;
        }

        size_t cut = last_break != 0 ? last_break
                                     : (last_space != 0 ? last_space : end);
        /* Strip trailing spaces so the queued chunk is clean prose.  Only
         * ASCII whitespace is trimmed, so the boundary stays UTF-8 safe. */
        while (cut > 0 && (reply[pos + cut - 1] == ' ' ||
                           reply[pos + cut - 1] == '\t')) {
            cut--;
        }
        if (cut == 0) {
            cut = end; /* all-whitespace window: keep the bytes as-is */
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
