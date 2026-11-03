/*
 * RoboDog ESP-Claw host unit tests for the N-slot TTS chunk pipeline.
 *
 * Same harness as test_voice_split.c (shared s_failures counter + the single
 * main() there), compiled against the EXACT voice_pipeline.c the firmware
 * runs (//robodog_client:esp_claw_voice_pure), so the slot-exchange
 * invariants the voice loop relies on are proven on the host.
 *
 * SPDX-License-Identifier: Apache-2.0
 */
#include <stdio.h>
#include <stdlib.h>

#include "voice_pipeline.h"

extern int s_failures; /* shared harness counter (defined in test_voice_split.c) */

#define CHECK(cond, fmt, ...)                                                  \
    do {                                                                       \
        if (!(cond)) {                                                         \
            s_failures++;                                                      \
            fprintf(stderr, "FAIL %s:%d: " fmt "\n", __func__, __LINE__,       \
                    ##__VA_ARGS__);                                            \
        }                                                                      \
    } while (0)

/* ── Invariant 1: bounded in-flight, strictly ascending claims ─────────── */
/* claim() arms chunks in order and refuses once the in-flight budget is
 * spent; commit()/fail() return the budget BEFORE the next claim may run. */
void test_pipe_bounded_inflight(void)
{
    robodog_voice_pipe_t s;
    robodog_voice_pipe_start(&s, 8, 4, 2);
    CHECK(s.slots == 4 && s.max_inflight == 2,
          "start keeps slots=4 inflight=2 (got %zu/%zu)", s.slots,
          s.max_inflight);

    size_t i0 = 0, i1 = 0, i2 = 0;
    CHECK(robodog_voice_pipe_claim(&s, &i0) == 0 && i0 == 1,
          "first claim -> slot 0, chunk 1");
    CHECK(robodog_voice_pipe_claim(&s, &i1) == 1 && i1 == 2,
          "second claim -> slot 1, chunk 2");
    CHECK(robodog_voice_pipe_claim(&s, &i2) == -1,
          "third claim MUST refuse (max_inflight=2)");
    CHECK(robodog_voice_pipe_inflight(&s) == 2, "two fetches in flight");

    robodog_voice_pipe_commit(&s, 1);
    CHECK(robodog_voice_pipe_inflight(&s) == 1, "commit frees one slot budget");
    CHECK(robodog_voice_pipe_ready_slot(&s, 2) == 1, "committed 2 -> slot 1");
    CHECK(robodog_voice_pipe_in_flight(&s, 1), "chunk 1 still in flight");

    CHECK(robodog_voice_pipe_claim(&s, &i2) == 2 && i2 == 3,
          "claim after commit -> slot 2, chunk 3 (ascending, no skip)");
    CHECK(robodog_voice_pipe_inflight(&s) == 2, "budget back to 2");

    robodog_voice_pipe_fail(&s, 2);
    CHECK(robodog_voice_pipe_inflight(&s) == 1, "fail frees the slot budget");
    CHECK(s.fetch_fail == 1, "fail latches fetch_fail");
    CHECK(robodog_voice_pipe_ready_slot(&s, 3) == -1,
          "failed fetch leaves NO ready slot for chunk 3");
    CHECK(robodog_voice_pipe_free_slot(&s) == 2,
          "the failed slot is FREE again for the sync fallback");
}

/* ── Invariant 2: K <= slots-2 keeps a FREE slot for the fallback ──────── */
/* Even with every permitted fetch in flight AND one slot playing, a free slot
 * always remains for a synchronous fetch — the property the firmware's
 * fallback path depends on. */
void test_pipe_free_slot_reserved(void)
{
    robodog_voice_pipe_t s;
    robodog_voice_pipe_start(&s, 10, 5, 3); /* K = slots-2 */
    CHECK(s.max_inflight == 3, "K=slots-2 accepted (got %zu)", s.max_inflight);

    size_t i = 0;
    for (int k = 0; k < 3; k++) {
        CHECK(robodog_voice_pipe_claim(&s, &i) >= 0,
              "claim %d allowed while K=slots-2", k);
    }
    CHECK(robodog_voice_pipe_claim(&s, &i) == -1,
          "budget spent: no fourth fetch");
    CHECK(robodog_voice_pipe_free_slot(&s) == 3,
          "the free slot MUST remain while K fetches are in flight");

    /* Simulate the worst case the invariant protects: one slot playing while
     * K fetches are in flight -> a free slot is STILL available. */
    robodog_voice_pipe_start(&s, 10, 5, 3);
    int first = robodog_voice_pipe_claim(&s, &i);
    robodog_voice_pipe_commit(&s, first);
    robodog_voice_pipe_begin_play(&s, i, first); /* one slot now playing */
    for (int k = 0; k < 3; k++) {
        CHECK(robodog_voice_pipe_claim(&s, &i) >= 0,
              "claim %d while a slot plays and K=slots-2", k);
    }
    CHECK(robodog_voice_pipe_claim(&s, &i) == -1, "budget still bounded");
    CHECK(robodog_voice_pipe_free_slot(&s) >= 0,
          "a FREE slot survives 1 playing + K in flight (sync fallback safe)");

    /* K is clamped: asking for more in-flight than slots-2 is refused. */
    robodog_voice_pipe_start(&s, 4, 2, 5);
    CHECK(s.max_inflight == 1, "2 slots -> single lookahead (got %zu)",
          s.max_inflight);
    robodog_voice_pipe_start(&s, 6, 4, 7);
    CHECK(s.max_inflight == 2, "4 slots -> K clamped to 2 (got %zu)",
          s.max_inflight);
    robodog_voice_pipe_start(&s, 4, 0, 0);
    CHECK(s.slots == 2 && s.max_inflight == 1,
          "degenerate slots/inflight clamp to 2/1 (got %zu/%zu)", s.slots,
          s.max_inflight);
}

/* ── Invariant 3: never hand out a PLAYING or READY slot ───────────────── */
void test_pipe_no_steal_of_busy_slots(void)
{
    robodog_voice_pipe_t s;
    robodog_voice_pipe_start(&s, 6, 4, 2); /* 1 playing + 2 in flight + 1 free */
    size_t i = 0;
    CHECK(robodog_voice_pipe_claim(&s, &i) == 0 && i == 1, "claim chunk 1");
    robodog_voice_pipe_commit(&s, 0);
    robodog_voice_pipe_begin_play(&s, 1, 0);

    /* Slot 0 is playing: claims must land in the OTHER slots only. */
    size_t a = 0, b = 0;
    CHECK(robodog_voice_pipe_claim(&s, &a) == 1 && a == 2,
          "claim while slot 0 plays -> slot 1");
    CHECK(robodog_voice_pipe_claim(&s, &b) == 2 && b == 3,
          "claim -> slot 2 (never the playing slot 0)");
    CHECK(robodog_voice_pipe_claim(&s, &i) == -1,
          "in-flight budget spent -> no third claim");
    robodog_voice_pipe_commit(&s, 1);
    robodog_voice_pipe_commit(&s, 2);
    CHECK(robodog_voice_pipe_free_slot(&s) == 3,
          "K=slots-2 leaves slot 3 free (sync fallback reserved)");
    /* The freed budget allows exactly the reserved slot, never the playing
     * one: claim() skips slot 0. */
    CHECK(robodog_voice_pipe_claim(&s, &i) == 3 && i == 4,
          "claim after commits -> slot 3, chunk 4 (playing slot 0 untouched)");
    robodog_voice_pipe_commit(&s, 3);
    CHECK(s.state[0] == ROBODOG_VOICE_SLOT_PLAYING,
          "the playing slot is never handed out by claim()");

    /* Re-claiming an already-buffered index must NOT overwrite it: claim
     * only ever hands out FREE slots, and every READY slot holds a pending
     * chunk. */
    CHECK(robodog_voice_pipe_ready_slot(&s, 2) == 1, "chunk 2 still buffered");
    CHECK(robodog_voice_pipe_ready_slot(&s, 3) == 2, "chunk 3 still buffered");

    /* Playing a chunk that is not the one buffered in the slot is refused
     * (no uninitialised / mismatched play). */
    robodog_voice_pipe_begin_play(&s, 4, 1);
    CHECK(s.state[1] == ROBODOG_VOICE_SLOT_READY,
          "begin_play of chunk 4 on the slot holding 2 is a no-op");

    robodog_voice_pipe_end_play(&s, 1, 0);
    CHECK(robodog_voice_pipe_free_slot(&s) == 0,
          "end_play frees the slot for the next fetch");
}

/* ── Invariant 4: full-turn accounting (the firmware turn sequence) ────── */
/* Drive the exact call sequence robodog_voice.c makes for a 7-chunk reply
 * over 4 slots with K=2: arm up to the budget, play in order, and prove every
 * index is played exactly once from a READY slot, with nothing left over. */
void test_pipe_full_turn(void)
{
    robodog_voice_pipe_t s;
    const size_t total = 7;
    robodog_voice_pipe_start(&s, total, 4, 2);
    size_t played = 0;
    size_t last = 0;

    for (size_t i = 1; i <= total; i++) {
        /* Arm lookahead BEFORE waiting, exactly like the turn loop. */
        size_t idx = 0;
        int slot;
        while ((slot = robodog_voice_pipe_claim(&s, &idx)) >= 0) {
            robodog_voice_pipe_commit(&s, slot);
        }
        const int ps = robodog_voice_pipe_ready_slot(&s, i);
        CHECK(ps >= 0, "turn: chunk %zu must be READY before play", i);
        robodog_voice_pipe_begin_play(&s, i, ps);
        robodog_voice_pipe_end_play(&s, i, ps);
        played++;
        last = i;
    }

    CHECK(played == total && last == total,
          "turn: every chunk played exactly once (%zu/%zu)", played, total);
    CHECK(s.next_play == total + 1, "turn end: next_play == n+1 (got %zu)",
          s.next_play);
    CHECK(s.next_fetch == total + 1, "turn end: next_fetch == n+1 (got %zu)",
          s.next_fetch);
    CHECK(robodog_voice_pipe_inflight(&s) == 0, "turn end: no in-flight fetch");
    CHECK(robodog_voice_pipe_free_slot(&s) == 0, "turn end: every slot FREE");
    CHECK(robodog_voice_pipe_ready_slot(&s, 1) == -1 &&
              robodog_voice_pipe_ready_slot(&s, total) == -1,
          "turn end: no chunk still buffered (all accounted)");
    CHECK(robodog_voice_pipe_claim(&s, &last) == -1,
          "turn end: claim past the total MUST refuse");
}

/* ── Invariant 5: sync-fallback adoption + clean fail/abort/reset ──────── */
void test_pipe_sync_fallback_and_reset(void)
{
    robodog_voice_pipe_t s;
    robodog_voice_pipe_start(&s, 4, 4, 2);
    size_t idx = 0;
    const int slot = robodog_voice_pipe_claim(&s, &idx); /* chunk 1 */
    robodog_voice_pipe_fail(&s, slot);                   /* lookahead failed */
    CHECK(robodog_voice_pipe_ready_slot(&s, 1) == -1, "failed -> not ready");
    CHECK(robodog_voice_pipe_in_flight(&s, 1) == false, "failed -> not in flight");
    CHECK(robodog_voice_pipe_free_slot(&s) == slot,
          "failed slot is FREE for the synchronous fetch");

    /* The firmware sync-fetches chunk 1 into that slot and adopts it. */
    robodog_voice_pipe_mark_ready(&s, 1, slot);
    CHECK(robodog_voice_pipe_ready_slot(&s, 1) == slot,
          "adopted sync fetch is READY");
    CHECK(robodog_voice_pipe_inflight(&s) == 0,
          "adoption must not change the in-flight count");
    robodog_voice_pipe_begin_play(&s, 1, slot);
    robodog_voice_pipe_end_play(&s, 1, slot);

    /* abort(): releases FETCHING slots so the caller can stop its task, and
     * freezes the cursors. */
    robodog_voice_pipe_claim(&s, &idx);
    robodog_voice_pipe_claim(&s, &idx);
    CHECK(robodog_voice_pipe_inflight(&s) == 2, "two in flight before abort");
    robodog_voice_pipe_abort(&s);
    CHECK(robodog_voice_pipe_inflight(&s) == 0, "abort releases in-flight");
    CHECK(s.aborted == 1, "abort records the aborted diagnostic");
    CHECK(robodog_voice_pipe_claim(&s, &idx) == -1, "no claim after abort");

    /* A late completion after abort must not corrupt the accounting. */
    robodog_voice_pipe_commit(&s, 0);
    robodog_voice_pipe_fail(&s, 1);
    CHECK(robodog_voice_pipe_inflight(&s) == 0,
          "late completions after abort are ignored");

    /* start() is the single reset point: a fresh turn inherits NOTHING. */
    robodog_voice_pipe_start(&s, 3, 4, 2);
    CHECK(s.fetch_fail == 0, "start() clears the fetch_fail latch");
    CHECK(s.aborted == 0, "start() clears the aborted flag");
    CHECK(robodog_voice_pipe_inflight(&s) == 0, "start() clears in-flight");
    CHECK(s.next_play == 1 && s.next_fetch == 1,
          "start() resets both cursors to 1");
    CHECK(robodog_voice_pipe_free_slot(&s) == 0, "start() frees every slot");
    CHECK(s.total == 3, "start() sets the turn total");
}

/* ── n == 1: single-chunk reply, no lookahead ever armed ───────────────── */
void test_pipe_single_chunk(void)
{
    robodog_voice_pipe_t s;
    robodog_voice_pipe_start(&s, 1, 4, 2);
    size_t idx = 0;
    const int slot = robodog_voice_pipe_claim(&s, &idx);
    CHECK(slot >= 0 && idx == 1, "n==1: chunk 1 claimed once");
    robodog_voice_pipe_commit(&s, slot);
    robodog_voice_pipe_begin_play(&s, 1, slot);
    CHECK(robodog_voice_pipe_claim(&s, &idx) == -1,
          "n==1: no further claim (past the total)");
    robodog_voice_pipe_end_play(&s, 1, slot);
    CHECK(s.next_play == 2, "n==1: next_play reaches n+1 == 2");
    CHECK(robodog_voice_pipe_inflight(&s) == 0, "n==1: no in-flight");
    CHECK(robodog_voice_pipe_free_slot(&s) == 0, "n==1: slot free again");
}
