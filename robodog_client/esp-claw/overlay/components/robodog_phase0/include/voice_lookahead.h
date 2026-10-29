/*
 * RoboDog ESP-Claw two-slot TTS double-buffer exchange model (LMAO-owned,
 * PURE C).
 *
 * The Phase 2 voice loop plays the chunked agent reply strictly sequentially:
 * each `robodog_voice_speak(chunk)` blocked ~1-3 s on the Kokoro HTTP
 * round-trip before real-time pacing, so every inter-sentence boundary had a
 * silent gap.  THE FIX splits speak into a fetch phase and a play phase and
 * overlaps ONE lookahead prefetch: while chunk i plays, a small background
 * task fetches chunk i+1's WAV into the free slot of a TWO-SLOT double-buffer
 * (exactly one fetch in flight — no deeper pipeline, bounded ~3 MB SPIRAM is
 * a hard memory invariant, not a tunable).
 *
 * This module is the PURE-C BOOKKEEPING MODEL of that slot exchange: it
 * tracks which chunk index each of the two slots holds, which one is
 * currently PLAYING (so a claim can never hand back the playing slot), and
 * the single in-flight lookahead fetch.  It deliberately has NO ESP-IDF
 * dependencies (no esp_log, no Kconfig, no malloc): the same source is
 * compiled both by the device firmware (robodog_voice.c via the
 * robodog_phase0 component) and by the Bazel host unit test
 * `//robodog_client:test_voice_split`, so the invariants the voice loop
 * relies on — exactly-one-lookahead, strict order, no reuse of a playing
 * slot, clean fail/abort, full-turn accounting — are proven on the host
 * against the exact bits that run on the board (DRY, same pattern as
 * voice_split.c / voice_md.c).
 *
 * All state is caller-owned via the struct; `start()` is the SINGLE reset
 * point, so a fresh turn can never inherit a previous turn's in-flight
 * fetch, failure latch, abort flag or slot accounting.
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
 * @brief Two-slot TTS double-buffer exchange state.
 *
 * Indices are 1-based chunk numbers matching the console `voice chunk i/n`
 * contract; 0 is the "none"/"free" sentinel, which keeps an empty slot
 * distinct from any real chunk index (there is no chunk 0).
 */
typedef struct {
    size_t total;         /* chunk count this turn (n); 0 = no reply */
    size_t next_play;     /* next index to play, 1-based; 0 = none */
    size_t inflight;      /* index currently being fetched (0 = none) */
    int    inflight_slot; /* slot (0/1) the in-flight fetch owns; -1 when idle */
    size_t slot_ix[2];    /* chunk index a slot holds (0 = free) */
    size_t playing[2];    /* chunk index currently PLAYING from a slot (0 = none) */
    int    fetch_fail;    /* latch: last lookahead fetch failed -> sync fallback */
    int    aborted;       /* mid-queue engine abort (in-flight cleared) */
} robodog_voice_la_t;

/**
 * @brief (Re)initialise a double-buffer exchange for a turn of `n` chunks.
 *        The SINGLE reset point: zeroes every field, sets `total`/`next_play`
 *        and returns `inflight_slot` to -1.  A fresh turn never inherits a
 *        previous turn's in-flight fetch, fail latch, abort flag or slot
 *        accounting.
 */
void robodog_voice_lookahead_start(robodog_voice_la_t *s, size_t n);

/**
 * @brief Claim the free slot for a lookahead fetch of chunk `i` (i+1 during
 *        playback of chunk i).  Returns the slot (0 or 1) to fetch into, or
 *        -1 when the fetch must not run:
 *          - another lookahead fetch is already in flight (exactly-one);
 *          - `i` is out of the turn (i > total) or 0;
 *          - the only usable slot already holds `i` (no re-fetch / no steal);
 *          - both slots are playing (guarded; impossible in practice).
 *        A slot that is currently PLAYING is never handed out; the other slot
 *        is chosen.  On success the in-flight fetch ownership is recorded
 *        (inflight = i, inflight_slot = the returned slot).
 */
int robodog_voice_lookahead_claim(robodog_voice_la_t *s, size_t i);

/**
 * @brief Complete the in-flight lookahead fetch as SUCCESS: the in-flight
 *        slot now holds chunk `inflight` (slot_ix[inflight_slot] = inflight)
 *        and the in-flight is cleared so the next claim may run.
 */
void robodog_voice_lookahead_commit(robodog_voice_la_t *s);

/**
 * @brief Complete the in-flight lookahead fetch as FAILURE: the slot stays
 *        free (it never held a committed chunk), `fetch_fail` is latched (the
 *        pending chunk must be fetched synchronously — never skipped or
 *        reordered) and the in-flight is cleared so the next claim may run.
 */
void robodog_voice_lookahead_fail(robodog_voice_la_t *s);

/**
 * @brief Return the slot (0/1) whose committed chunk is `i`, or -1 when the
 *        current chunk is NOT already buffered (a synchronous fetch-then-play
 *        then plays it).  Strict order: a slot is returned only when
 *        slot_ix[slot] == i, so a stale/older index can never masquerade as
 *        chunk i.
 */
int robodog_voice_lookahead_prefetched(robodog_voice_la_t *s, size_t i);

/**
 * @brief True while a lookahead fetch is in flight (a slot is owned by the
 *        prefetch task and MUST NOT be freed/reset by the caller).
 */
int robodog_voice_lookahead_has_inflight(const robodog_voice_la_t *s);

/**
 * @brief Mark chunk `i` as PLAYING from `slot`.  MUST be called BEFORE any
 *        claim that could otherwise hand out `slot` (the no-double-claim
 *        invariant: while a slot is playing, claim() returns the OTHER slot).
 *        Also records the slot as holding `i` for the turn's accounting.
 */
void robodog_voice_lookahead_begin_play(robodog_voice_la_t *s, size_t i,
                                        int slot);

/**
 * @brief Mark chunk `i`'s playback from `slot` as finished and free the slot
 *        for the next reuse (slot_ix[slot] cleared when it still points at
 *        `i`).  The caller owns the actual buffer lifecycle.
 */
void robodog_voice_lookahead_end_play(robodog_voice_la_t *s, size_t i,
                                      int slot);

/**
 * @brief Abort a mid-queue turn: clear any in-flight ownership so the caller
 *        can free the slot buffers / chunk arena safely.  The prefetch task
 *        must already have been ABORT-acked by the caller BEFORE this —
 *        `abort()` only clears the model, it never touches the buffers.
 *        Sets the `aborted` diagnostic; `start()` clears it for the next turn.
 */
void robodog_voice_lookahead_abort(robodog_voice_la_t *s);

#ifdef __cplusplus
}
#endif
