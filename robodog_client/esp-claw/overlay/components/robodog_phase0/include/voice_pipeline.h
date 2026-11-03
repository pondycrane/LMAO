/*
 * RoboDog ESP-Claw TTS chunk pipeline (LMAO-owned, PURE C).
 *
 * WHY THIS EXISTS
 * ---------------
 * Qwen3-TTS on the household LAN synthesizes at roughly real time
 * (measured RTF ~0.7-1.1x warm, worse when the GPU host is busy), and the
 * endpoint returns the WHOLE WAV at once (Content-Length, no chunked
 * streaming: time-to-first-byte == total time).  So a chunk's audio cannot
 * start before its synthesis finished.
 *
 * The Phase 2 voice loop plays the chunked agent reply back-to-back.  A
 * ONE-step lookahead (fetch i+1 while i plays) only hides the fetch when the
 * fetch is shorter than the playback — which is exactly what fails when the
 * endpoint is slower than real time, and the reply then has multi-second
 * silences between sentences ("long delay between response segments").
 *
 * This model generalizes that exchange to an N-SLOT PIPELINE with K fetches
 * in flight (K <= N-1), so synthesis runs K chunks ahead of playback.  The
 * pipeline keeps the speaker fed as long as the AGGREGATE synthesis rate
 * covers real time (K x RTF >= 1), instead of requiring every single fetch
 * to beat its own playback duration.
 *
 * The module is the PURE-C BOOKKEEPING MODEL of that slot exchange: it
 * tracks which chunk index each slot holds and in which state (FREE /
 * FETCHING / READY / PLAYING), which fetches are in flight, and which chunk
 * is next to fetch / next to play.  It has NO ESP-IDF dependencies (no
 * esp_log, no Kconfig, no allocation): the same source is compiled by the
 * device firmware (robodog_voice.c) and by the Bazel host unit test
 * `//robodog_client:test_voice_pipeline`, so the invariants the voice loop
 * relies on — bounded in-flight fetches, strict order, never handing out a
 * playing slot, clean fail/abort, full-turn accounting — are proven on the
 * host against the exact bits that run on the board (DRY, same pattern as
 * voice_split.c / voice_md.c).
 *
 * All state is caller-owned via the struct; `start()` is the SINGLE reset
 * point, so a fresh turn can never inherit a previous turn's in-flight
 * fetches, failure latch or slot accounting.
 *
 * SPDX-License-Identifier: Apache-2.0
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Hard upper bound on slots; the firmware's slot count is a Kconfig knob
 * (CONFIG_ROBODOG_VOICE_TTS_SLOTS) validated against this at compile time. */
#define ROBODOG_VOICE_PIPE_MAX_SLOTS 8

/** Per-slot state. */
typedef enum {
    ROBODOG_VOICE_SLOT_FREE = 0, /* no chunk buffered; may be claimed */
    ROBODOG_VOICE_SLOT_FETCHING, /* a background fetch owns it (in flight) */
    ROBODOG_VOICE_SLOT_READY,    /* the chunk's WAV is buffered */
    ROBODOG_VOICE_SLOT_PLAYING,  /* the speaker is reading it right now */
} robodog_voice_slot_state_t;

/**
 * @brief N-slot TTS pipeline state.
 *
 * Chunk indices are 1-based to match the console `voice chunk i/n` contract;
 * 0 is the "none" sentinel (there is no chunk 0).
 */
typedef struct {
    size_t total;      /* chunk count this turn (n); 0 = no reply */
    size_t slots;      /* usable slot count this turn (2..MAX_SLOTS) */
    size_t max_inflight; /* max concurrent background fetches (1..slots-1) */
    size_t next_fetch; /* lowest index not yet claimed for a fetch (1-based) */
    size_t next_play;  /* lowest index not yet played (1-based) */
    size_t inflight;   /* slots currently FETCHING */
    unsigned char state[ROBODOG_VOICE_PIPE_MAX_SLOTS];
    size_t ix[ROBODOG_VOICE_PIPE_MAX_SLOTS]; /* chunk index held by a slot */
    int fetch_fail;    /* latch: at least one lookahead fetch failed */
    int aborted;       /* mid-queue engine abort */
} robodog_voice_pipe_t;

/**
 * @brief (Re)initialise a pipeline for a turn of `total` chunks over `slots`
 *        buffers, with at most `max_inflight` background fetches at once.
 *
 *        The SINGLE reset point: every field is zeroed, `next_fetch`/
 *        `next_play` start at 1, and a fresh turn never inherits a previous
 *        turn's in-flight fetches, failure latch, abort flag or slot
 *        accounting.  `slots` is clamped to [2, MAX_SLOTS] and
 *        `max_inflight` to [1, slots-1], which is what guarantees a FREE slot
 *        is always available for the synchronous fallback fetch.
 */
void robodog_voice_pipe_start(robodog_voice_pipe_t *s, size_t total,
                              size_t slots, size_t max_inflight);

/**
 * @brief Claim a FREE slot for a background fetch of the next chunk in order.
 *        Writes that chunk index to `*out_index` and returns the slot, or
 *        returns -1 when nothing may be armed:
 *          - the turn is over / aborted / empty (next_fetch > total);
 *          - `max_inflight` fetches are already in flight;
 *          - no slot is FREE (every slot is READY or PLAYING).
 *        Claiming is strictly ascending: it never skips, reorders or
 *        re-fetches an index, and it NEVER hands out a PLAYING or READY slot.
 */
int robodog_voice_pipe_claim(robodog_voice_pipe_t *s, size_t *out_index);

/**
 * @brief Complete a claimed fetch as SUCCESS: the slot now holds its chunk
 *        (READY) and stops counting as in flight.  No-op when the slot is not
 *        FETCHING (a stale/late completion after an abort).
 */
void robodog_voice_pipe_commit(robodog_voice_pipe_t *s, int slot);

/**
 * @brief Complete a claimed fetch as FAILURE: the slot returns to FREE (it
 *        never held a usable chunk), stops counting as in flight, and the
 *        failure latch is set so the caller knows the missing chunk must be
 *        fetched synchronously — never skipped or reordered.
 */
void robodog_voice_pipe_fail(robodog_voice_pipe_t *s, int slot);

/**
 * @brief Record a SYNCHRONOUS fetch of chunk `index` into `slot` as buffered
 *        (the fallback path): the slot becomes READY holding `index`.  Does
 *        not touch the in-flight count.
 */
void robodog_voice_pipe_mark_ready(robodog_voice_pipe_t *s, size_t index,
                                   int slot);

/** @brief Slot holding chunk `index` in READY state, or -1 when it is not
 *         buffered yet (in flight, failed, or not yet claimed). */
int robodog_voice_pipe_ready_slot(const robodog_voice_pipe_t *s, size_t index);

/** @brief True while chunk `index` is owned by an in-flight background
 *         fetch (so the caller must WAIT for it rather than re-fetch). */
bool robodog_voice_pipe_in_flight(const robodog_voice_pipe_t *s, size_t index);

/** @brief Slot currently FETCHING chunk `index`, or -1 (that fetch's
 *         completion bit is what the caller waits on). */
int robodog_voice_pipe_fetching_slot(const robodog_voice_pipe_t *s,
                                     size_t index);

/** @brief First FREE slot, or -1 when every slot is FETCHING/READY/PLAYING. */
int robodog_voice_pipe_free_slot(const robodog_voice_pipe_t *s);

/** @brief Number of background fetches currently in flight. */
size_t robodog_voice_pipe_inflight(const robodog_voice_pipe_t *s);

/**
 * @brief Mark chunk `index` as PLAYING from `slot`.  The slot must be READY
 *        and must hold EXACTLY `index` (a mismatched call is a no-op: a slot
 *        can only play the chunk that was buffered into it).  MUST be called
 *        BEFORE arming further fetches: claim() never hands out a PLAYING
 *        slot, so the buffer being read cannot be overwritten.
 */
void robodog_voice_pipe_begin_play(robodog_voice_pipe_t *s, size_t index,
                                   int slot);

/**
 * @brief Mark chunk `index`'s playback from `slot` as finished: the slot is
 *        FREE for the next fetch and the play cursor advances.  The caller
 *        owns the actual buffer lifecycle (the slot's storage is reused, never
 *        freed, by the firmware).
 */
void robodog_voice_pipe_end_play(robodog_voice_pipe_t *s, size_t index,
                                 int slot);

/**
 * @brief Abort a mid-queue turn: every FETCHING slot is released (the caller
 *        must have stopped its fetch task first — `abort()` only clears the
 *        model, it never touches buffers) and the play/fetch cursors freeze.
 *        Sets the `aborted` diagnostic; `start()` clears it for the next turn.
 */
void robodog_voice_pipe_abort(robodog_voice_pipe_t *s);

#ifdef __cplusplus
}
#endif
