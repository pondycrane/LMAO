/*
 * RoboDog ESP-Claw whole-reply chunk splitter (LMAO-owned, PURE C).
 *
 * Extracted from robodog_voice.c so the same source is host-testable under
 * Bazel (R6: the voice-verify gate's whole-reply coverage claim must not
 * depend on an untested static function).  Pure C only — no esp_log, no
 * Kconfig symbols; the caller passes the per-chunk `cap` and the `max_chunks`
 * bound explicitly.  See voice_split.h for the contract.
 *
 * SENTENCE ALIGNMENT is the point of this module.  A chunk boundary is a
 * place where the speaker stops and starts again, so cutting mid-sentence
 * ("...and the blinds are" | "closed, so the room...") makes the read-out
 * sound broken even though every word is spoken.  The preferred budget `cap`
 * is therefore a SOFT limit: when the window ends mid-sentence the splitter
 * extends to the NEXT sentence ender, up to ROBODOG_VOICE_SPLIT_MAX_BYTES
 * (the RLCD UI field, which is the real ceiling).  `cap` only wins when a
 * sentence ender fits inside it, and the word-boundary / hard-cut fallbacks
 * are reached only for genuinely unpunctuated text.
 *
 * UTF-8: every bound is a BYTE budget (the UI command field and the TTS
 * request are byte-sized), so a chunk is never cut inside a multi-byte
 * sequence — the cut is always snapped to a codepoint boundary.  Sentence
 * breaks include the CJK punctuation a Traditional Chinese reply uses
 * (。！？；…), so Chinese prose is split at sentence ends too.
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

/* A chunk ends AFTER one of these (the break char stays in the chunk).
 * These are the ONLY seams a spoken reply should have: a sentence end. */
static bool is_sentence_break(uint32_t cp)
{
    return cp == '.' || cp == '!' || cp == '?' || cp == ';' ||
           cp == 0x3002 || /* 。 ideographic full stop */
           cp == 0xFF01 || /* ！ fullwidth exclamation */
           cp == 0xFF1F || /* ？ fullwidth question */
           cp == 0xFF1B || /* ； fullwidth semicolon */
           cp == 0x2026;   /* … ellipsis (Chinese sentences often end so) */
}

/* A newline is only a WEAK break.  Treating it as a sentence end (as this
 * module used to) cut replies at the agent's line wrapping — "I can tell you
 * the time, the weather,\n" became one chunk and "or a joke." the next, i.e.
 * a seam in the middle of a sentence, which is exactly what made the read-out
 * sound broken.  Weak breaks are used only when the reply carries no sentence
 * punctuation at all (a stripped bullet list). */
static bool is_line_break(uint32_t cp)
{
    return cp == '\n' || cp == '\r';
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
        /* Preferred budget for this chunk.  The FIRST chunk may use a tighter
         * budget so the speaker starts sooner: synthesis of chunk 1 is the
         * entire time-to-first-audio.  A tighter budget only wins when a
         * sentence ender fits inside it — it never forces a mid-sentence cut.
         */
        size_t budget = cap;
        if (n == 0 && first_cap != 0 && first_cap < budget) {
            budget = first_cap;
        }
        if (budget == 0) {
            budget = 1;
        }
        /* The ceiling is the UI text field: a chunk may exceed the preferred
         * budget to reach a sentence ender, but never the field. */
        size_t ceiling = ROBODOG_VOICE_SPLIT_MAX_BYTES;
        if (budget > ceiling) {
            budget = ceiling;
        }
        if (remain < ceiling) {
            ceiling = remain;
        }

        /* Walk whole codepoints up to the ceiling, remembering:
         *   last_break  — the LAST sentence ender inside the budget
         *   next_break  — the FIRST sentence ender past the budget
         *   last_space  — the last word boundary inside the budget
         */
        size_t end = 0;
        size_t end_budget = 0; /* last codepoint boundary at/below budget */
        size_t last_break = 0; /* last sentence ender inside the budget */
        size_t next_break = 0; /* first sentence ender past the budget */
        size_t last_line = 0;  /* last newline inside the budget (weak) */
        size_t last_space = 0;
        while (end < ceiling) {
            uint32_t cp = ROBODOG_UTF8_INVALID;
            size_t clen = robodog_utf8_decode(reply + pos + end,
                                              remain - end, &cp);
            if (clen == 0) {
                clen = 1; /* malformed byte: treat as one replacement char */
            }
            if (end + clen > ceiling) {
                break; /* this codepoint would split across the ceiling */
            }
            end += clen;
            if (end <= budget) {
                end_budget = end;
            }
            if (is_sentence_break(cp)) {
                if (end <= budget) {
                    last_break = end;
                } else if (next_break == 0) {
                    next_break = end;
                }
            } else if (is_line_break(cp)) {
                if (end <= budget) {
                    last_line = end;
                }
            } else if (cp == ' ' && end - 1 <= budget) {
                last_space = end - 1;
            }
            /* Past the budget we only need the FIRST sentence ender; stop as
             * soon as we have it so a long reply costs no extra scanning. */
            if (next_break != 0 && end > budget) {
                break;
            }
        }
        if (end == 0) {
            /* ceiling is smaller than the first codepoint: emit it whole
             * rather than loop forever. */
            const size_t clen =
                robodog_utf8_decode(reply + pos, remain, NULL);
            end = clen > 0 ? clen : 1;
            last_break = end;
        }

        /* Sentence ender inside the budget -> use it.  Otherwise extend to
         * the first sentence ender past the budget (soft overflow) so the
         * seam is still a sentence boundary.  Only unpunctuated text falls
         * back to a word boundary or a hard cut. */
        /* Fallback order matters: a run-on sentence with no punctuation
         * still respects the preferred budget (hard cut at `budget`, snapped
         * to a codepoint) rather than swallowing the whole ceiling. */
        size_t cut;
        if (last_break != 0) {
            cut = last_break;
        } else if (next_break != 0) {
            cut = next_break;
        } else if (last_line != 0) {
            cut = last_line;
        } else if (last_space != 0) {
            cut = last_space;
        } else if (end_budget != 0) {
            cut = end_budget;
        } else {
            /* No codepoint boundary fits the budget (budget < one character):
             * emit exactly one codepoint so the loop always advances. */
            const size_t clen =
                robodog_utf8_decode(reply + pos, remain, NULL);
            cut = clen > 0 ? clen : 1;
        }
        /* Strip trailing spaces so the queued chunk is clean prose.  Only
         * ASCII whitespace is trimmed, so the boundary stays UTF-8 safe. */
        while (cut > 0 && (reply[pos + cut - 1] == ' ' ||
                           reply[pos + cut - 1] == '\t' ||
                           reply[pos + cut - 1] == '\n' ||
                           reply[pos + cut - 1] == '\r')) {
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
