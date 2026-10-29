/*
 * RoboDog ESP-Claw two-slot TTS double-buffer exchange model (LMAO-owned,
 * PURE C).
 *
 * Extracted from robodog_voice.c so the slot-exchange bookkeeping behind the
 * one-step TTS lookahead prefetch is host-testable under Bazel (the same DRY
 * pattern as voice_split.c / voice_md.c — R6/R8).  Pure C only: no esp_log,
 * no Kconfig symbols, no allocation; all state is caller-owned via the
 * robodog_voice_la_t struct and `start()` is the single reset point.
 *
 * The model answers exactly one question per chunk boundary: WHICH slot does
 * chunk i+1 play from while chunk i plays (fetched ahead or synchronous
 * fallback), and can the next fetch be armed into the just-freed slot safely.
 * See voice_lookahead.h for the full contract.
 *
 * SPDX-License-Identifier: Apache-2.0
 */
#include "voice_lookahead.h"

#define LA_SLOTS 2

void robodog_voice_lookahead_start(robodog_voice_la_t *s, size_t n)
{
    if (s == NULL) {
        return;
    }
    s->total = n;
    s->next_play = (n > 0) ? 1 : 0;
    s->inflight = 0;
    s->inflight_slot = -1;
    s->slot_ix[0] = 0;
    s->slot_ix[1] = 0;
    s->playing[0] = 0;
    s->playing[1] = 0;
    s->fetch_fail = 0;
    s->aborted = 0;
}

int robodog_voice_lookahead_claim(robodog_voice_la_t *s, size_t i)
{
    if (s == NULL || i == 0 || i > s->total) {
        return -1;
    }
    if (s->inflight != 0) {
        return -1; /* exactly-one-lookahead: a fetch is already in flight */
    }
    /* Pick the first non-playing slot.  While one slot plays that IS the free
     * slot; while the exchange is idle either works (deterministically the
     * lowest index).  A slot that already holds `i` is never re-fetched /
     * stolen — the data is buffered and the caller will find it via
     * prefetched(). */
    for (int k = 0; k < LA_SLOTS; k++) {
        if (s->playing[k] != 0) {
            continue; /* never hand out the playing slot */
        }
        if (s->slot_ix[k] == i) {
            return -1; /* next index already buffered here — no steal */
        }
        s->inflight = i;
        s->inflight_slot = k;
        return k;
    }
    return -1; /* both slots playing / unavailable (guarded) */
}

void robodog_voice_lookahead_commit(robodog_voice_la_t *s)
{
    if (s == NULL || s->inflight == 0) {
        return;
    }
    if (s->inflight_slot >= 0 && s->inflight_slot < LA_SLOTS) {
        s->slot_ix[s->inflight_slot] = s->inflight;
    }
    s->inflight = 0;
    s->inflight_slot = -1;
}

void robodog_voice_lookahead_fail(robodog_voice_la_t *s)
{
    if (s == NULL || s->inflight == 0) {
        return;
    }
    s->fetch_fail = 1; /* latch: the pending chunk needs a sync fetch */
    s->inflight = 0;   /* the slot never held a committed chunk */
    s->inflight_slot = -1;
}

int robodog_voice_lookahead_prefetched(robodog_voice_la_t *s, size_t i)
{
    if (s == NULL || i == 0) {
        return -1;
    }
    for (int k = 0; k < LA_SLOTS; k++) {
        if (s->slot_ix[k] == i) {
            return k;
        }
    }
    return -1;
}

int robodog_voice_lookahead_has_inflight(const robodog_voice_la_t *s)
{
    return (s != NULL && s->inflight != 0) ? 1 : 0;
}

void robodog_voice_lookahead_begin_play(robodog_voice_la_t *s, size_t i,
                                        int slot)
{
    if (s == NULL || i == 0 || slot < 0 || slot >= LA_SLOTS) {
        return;
    }
    s->playing[slot] = i;
    s->slot_ix[slot] = i; /* the slot holds the chunk being played */
    if (i == s->next_play && i <= s->total) {
        s->next_play = i + 1; /* in-order progress accounting */
    }
}

void robodog_voice_lookahead_end_play(robodog_voice_la_t *s, size_t i,
                                      int slot)
{
    if (s == NULL || slot < 0 || slot >= LA_SLOTS) {
        return;
    }
    if (s->playing[slot] == i) {
        s->playing[slot] = 0;
    }
    if (s->slot_ix[slot] == i) {
        s->slot_ix[slot] = 0; /* slot freed for the next reuse */
    }
}

void robodog_voice_lookahead_abort(robodog_voice_la_t *s)
{
    if (s == NULL) {
        return;
    }
    s->inflight = 0;
    s->inflight_slot = -1;
    s->aborted = 1;
}
