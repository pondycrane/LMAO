/*
 * RoboDog ESP-Claw TTS chunk pipeline (LMAO-owned, PURE C).
 *
 * Extracted from robodog_voice.c so the slot-exchange bookkeeping behind the
 * multi-slot TTS lookahead prefetch is host-testable under Bazel (the same DRY
 * pattern as voice_split.c / voice_md.c).  Pure C only: no esp_log, no
 * Kconfig symbols, no allocation; all state is caller-owned via the
 * robodog_voice_pipe_t struct and `start()` is the single reset point.
 *
 * The model answers two questions at every point of a turn: WHICH chunk may be
 * fetched next (in order, bounded by the in-flight budget and free slots) and
 * WHICH slot chunk i plays from (already buffered, or a synchronous fallback
 * fetch into a free slot).  See voice_pipeline.h for the full contract.
 *
 * SPDX-License-Identifier: Apache-2.0
 */
#include "voice_pipeline.h"

#include <string.h>

#define PIPE_SLOTS ROBODOG_VOICE_PIPE_MAX_SLOTS

void robodog_voice_pipe_start(robodog_voice_pipe_t *s, size_t total,
                              size_t slots, size_t max_inflight)
{
    if (s == NULL) {
        return;
    }
    memset(s, 0, sizeof(*s));
    if (slots < 2) {
        slots = 2;
    }
    if (slots > PIPE_SLOTS) {
        slots = PIPE_SLOTS;
    }
    if (max_inflight < 1) {
        max_inflight = 1;
    }
    /* K <= slots-2 is the invariant that keeps a FREE slot available for the
     * synchronous fallback fetch even while one slot is playing and K chunks
     * are FETCHING/READY.  (With 2 slots that leaves the single lookahead.) */
    const size_t k_max = (slots >= 3) ? (slots - 2) : 1;
    if (max_inflight > k_max) {
        max_inflight = k_max;
    }
    s->total = total;
    s->slots = slots;
    s->max_inflight = max_inflight;
    s->next_fetch = (total > 0) ? 1 : 0;
    s->next_play = (total > 0) ? 1 : 0;
}

static bool slot_valid(const robodog_voice_pipe_t *s, int slot)
{
    return s != NULL && slot >= 0 && (size_t)slot < s->slots;
}

int robodog_voice_pipe_claim(robodog_voice_pipe_t *s, size_t *out_index)
{
    if (s == NULL || out_index == NULL) {
        return -1;
    }
    if (s->aborted || s->next_fetch == 0 || s->next_fetch > s->total) {
        return -1; /* turn over / aborted / empty */
    }
    if (s->inflight >= s->max_inflight) {
        return -1; /* in-flight budget spent */
    }
    for (int k = 0; k < (int)s->slots; k++) {
        if (s->state[k] != ROBODOG_VOICE_SLOT_FREE) {
            continue; /* never steal a FETCHING / READY / PLAYING slot */
        }
        *out_index = s->next_fetch++;
        s->state[k] = ROBODOG_VOICE_SLOT_FETCHING;
        s->ix[k] = *out_index;
        s->inflight++;
        return k;
    }
    return -1; /* every slot busy: wait for a play to finish */
}

void robodog_voice_pipe_commit(robodog_voice_pipe_t *s, int slot)
{
    if (!slot_valid(s, slot)) {
        return;
    }
    if (s->state[slot] != ROBODOG_VOICE_SLOT_FETCHING) {
        return; /* stale completion (post-abort): nothing to account */
    }
    s->state[slot] = ROBODOG_VOICE_SLOT_READY;
    if (s->inflight > 0) {
        s->inflight--;
    }
}

void robodog_voice_pipe_fail(robodog_voice_pipe_t *s, int slot)
{
    if (!slot_valid(s, slot)) {
        return;
    }
    if (s->state[slot] != ROBODOG_VOICE_SLOT_FETCHING) {
        return;
    }
    s->state[slot] = ROBODOG_VOICE_SLOT_FREE;
    s->ix[slot] = 0;
    if (s->inflight > 0) {
        s->inflight--;
    }
    s->fetch_fail = 1; /* latch: the missing chunk needs a sync fetch */
}

void robodog_voice_pipe_mark_ready(robodog_voice_pipe_t *s, size_t index,
                                   int slot)
{
    if (!slot_valid(s, slot) || index == 0) {
        return;
    }
    s->state[slot] = ROBODOG_VOICE_SLOT_READY;
    s->ix[slot] = index;
}

int robodog_voice_pipe_ready_slot(const robodog_voice_pipe_t *s, size_t index)
{
    if (s == NULL || index == 0) {
        return -1;
    }
    for (int k = 0; k < (int)s->slots; k++) {
        if (s->state[k] == ROBODOG_VOICE_SLOT_READY && s->ix[k] == index) {
            return k;
        }
    }
    return -1;
}

bool robodog_voice_pipe_in_flight(const robodog_voice_pipe_t *s, size_t index)
{
    if (s == NULL || index == 0) {
        return false;
    }
    for (int k = 0; k < (int)s->slots; k++) {
        if (s->state[k] == ROBODOG_VOICE_SLOT_FETCHING && s->ix[k] == index) {
            return true;
        }
    }
    return false;
}

int robodog_voice_pipe_fetching_slot(const robodog_voice_pipe_t *s,
                                     size_t index)
{
    if (s == NULL || index == 0) {
        return -1;
    }
    for (int k = 0; k < (int)s->slots; k++) {
        if (s->state[k] == ROBODOG_VOICE_SLOT_FETCHING && s->ix[k] == index) {
            return k;
        }
    }
    return -1;
}

int robodog_voice_pipe_free_slot(const robodog_voice_pipe_t *s)
{
    if (s == NULL) {
        return -1;
    }
    for (int k = 0; k < (int)s->slots; k++) {
        if (s->state[k] == ROBODOG_VOICE_SLOT_FREE) {
            return k;
        }
    }
    return -1;
}

size_t robodog_voice_pipe_inflight(const robodog_voice_pipe_t *s)
{
    return (s != NULL) ? s->inflight : 0;
}

void robodog_voice_pipe_begin_play(robodog_voice_pipe_t *s, size_t index,
                                   int slot)
{
    if (!slot_valid(s, slot) || index == 0) {
        return;
    }
    if (s->state[slot] != ROBODOG_VOICE_SLOT_READY || s->ix[slot] != index) {
        return; /* only the buffered chunk in that slot can play */
    }
    s->state[slot] = ROBODOG_VOICE_SLOT_PLAYING;
}

void robodog_voice_pipe_end_play(robodog_voice_pipe_t *s, size_t index,
                                 int slot)
{
    if (!slot_valid(s, slot)) {
        return;
    }
    if (s->state[slot] == ROBODOG_VOICE_SLOT_PLAYING) {
        s->state[slot] = ROBODOG_VOICE_SLOT_FREE;
        s->ix[slot] = 0;
    }
    if (index != 0 && index == s->next_play && index <= s->total) {
        s->next_play = index + 1;
    }
}

void robodog_voice_pipe_abort(robodog_voice_pipe_t *s)
{
    if (s == NULL) {
        return;
    }
    for (int k = 0; k < (int)s->slots; k++) {
        if (s->state[k] == ROBODOG_VOICE_SLOT_FETCHING) {
            s->state[k] = ROBODOG_VOICE_SLOT_FREE;
            s->ix[k] = 0;
        }
    }
    s->inflight = 0;
    s->aborted = 1;
}
