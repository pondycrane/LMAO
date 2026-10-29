/*
 * RoboDog ESP-Claw host unit tests for the one-step TTS lookahead
 * double-buffer exchange (R9: exactly-one-lookahead, strict order, no reuse
 * of a playing slot, clean fail/abort, full-turn accounting).
 *
 * These compile the EXACT source the device firmware builds
 * (voice_lookahead.c, pure C) under Bazel as part of
 * `//robodog_client:test_voice_split` (same binary as the
 * splitter/transcript/md tests, sharing their no-framework harness), so the
 * slot-exchange invariants the gapless-prefetch voice loop relies on are
 * proven on the host — a double-claim of a playing slot, a skip/reorder on
 * fetch failure, or a stale in-flight surviving into the next turn can never
 * sail through with green hardware gates.
 *
 * The harness's `s_failures` counter and `CHECK` macro are the SAME symbols
 * test_voice_split.c owns — this TU externs them and the shared `main` in
 * test_voice_split.c calls every test_* here.
 *
 * SPDX-License-Identifier: Apache-2.0
 */
#include <stdio.h>
#include <stdlib.h>

#include "voice_lookahead.h"

extern int s_failures; /* shared harness counter (defined in test_voice_split.c) */

#define CHECK(cond, fmt, ...)                                                  \
    do {                                                                       \
        if (!(cond)) {                                                         \
            s_failures++;                                                      \
            fprintf(stderr, "FAIL %s:%d: " fmt "\n", __func__, __LINE__,       \
                    ##__VA_ARGS__);                                            \
        }                                                                      \
    } while (0)

/* ── Invariant 1: exactly-one-lookahead ────────────────────────────────── */
/* claim() must refuse while a fetch is in flight; commit()/fail() free the
 * in-flight BEFORE the next claim may run. */
void test_lookahead_exactly_one(void)
{
    robodog_voice_la_t s;
    robodog_voice_lookahead_start(&s, 8);

    /* Deterministic preference: both slots idle -> slot 0. */
    CHECK(robodog_voice_lookahead_claim(&s, 2) == 0,
          "claim 2 on an idle exchange -> slot 0, got %d",
          robodog_voice_lookahead_claim(&s, 2));
    CHECK(robodog_voice_lookahead_has_inflight(&s) == 1,
          "claim 2 must record in-flight");
    CHECK(robodog_voice_lookahead_claim(&s, 3) == -1,
          "claim 3 while 2 is in flight MUST refuse (exactly one fetch)");
    CHECK(robodog_voice_lookahead_claim(&s, 2) == -1,
          "re-claim 2 while in flight MUST refuse");
    CHECK(robodog_voice_lookahead_prefetched(&s, 2) == -1,
          "chunk 2 NOT committed yet -> no slot");

    robodog_voice_lookahead_commit(&s);
    CHECK(robodog_voice_lookahead_has_inflight(&s) == 0,
          "commit must free the in-flight");
    CHECK(robodog_voice_lookahead_prefetched(&s, 2) == 0,
          "committed 2 into slot 0 -> prefetched(2)==0");

    /* After commit a new claim is legal again.  Slot 0 still holds 2 (not
     * playing here), slot 1 is free -> deterministic slot 0 again. */
    CHECK(robodog_voice_lookahead_claim(&s, 3) == 0,
          "claim 3 after commit allowed -> slot 0, got %d",
          robodog_voice_lookahead_claim(&s, 3));

    /* fail() frees the in-flight the same way, and latches the failure. */
    robodog_voice_lookahead_fail(&s);
    CHECK(robodog_voice_lookahead_has_inflight(&s) == 0,
          "fail must free the in-flight");
    CHECK(s.fetch_fail == 1, "fail must latch fetch_fail");
    CHECK(robodog_voice_lookahead_claim(&s, 4) == 0,
          "claim 4 after fail allowed (latch does not block later claims)");
    robodog_voice_lookahead_commit(&s);
}

/* ── Invariant 2: strict order + sync-fallback correctness ─────────────── */
/* prefetched(i) returns a slot only when that slot holds chunk i; a failed
 * lookahead leaves NO slot for the pending index (so the caller falls back to
 * a synchronous fetch of EXACTLY that index — never a later one). */
void test_lookahead_strict_order(void)
{
    robodog_voice_la_t s;
    robodog_voice_lookahead_start(&s, 6);

    /* Commit chunk 2 into slot 1 while slot 0 is playing chunk 1. */
    robodog_voice_lookahead_begin_play(&s, 1, 0);
    CHECK(robodog_voice_lookahead_claim(&s, 2) == 1,
          "claim 2 while slot 0 plays chunk 1 -> slot 1");
    robodog_voice_lookahead_commit(&s);

    CHECK(robodog_voice_lookahead_prefetched(&s, 2) == 1,
          "prefetched(2) -> slot 1 (strict order: slot_ix[1]==2)");
    CHECK(robodog_voice_lookahead_prefetched(&s, 1) == 0,
          "prefetched(1) -> slot 0 (chunk 1 is being played from slot 0)");
    CHECK(robodog_voice_lookahead_prefetched(&s, 3) == -1,
          "nothing holds chunk 3 yet -> -1");
    CHECK(robodog_voice_lookahead_prefetched(&s, 0) == -1,
          "chunk 0 (sentinel) can never masquerade as buffered");

    /* A fetch failure must NOT leave a slot claiming the pending index. */
    robodog_voice_lookahead_end_play(&s, 1, 0);
    robodog_voice_lookahead_begin_play(&s, 2, 1);
    CHECK(robodog_voice_lookahead_claim(&s, 3) == 0,
          "claim 3 while slot 1 plays chunk 2 -> slot 0, got %d",
          robodog_voice_lookahead_claim(&s, 3));
    robodog_voice_lookahead_fail(&s); /* the 3-lookahead FAILS */
    CHECK(s.fetch_fail == 1, "a failed lookahead latches fetch_fail");
    CHECK(robodog_voice_lookahead_prefetched(&s, 3) == -1,
          "failed lookahead MUST leave no slot for chunk 3 (sync fallback "
          "fetches it fresh; never a stale/later index)");
    robodog_voice_lookahead_end_play(&s, 2, 1);
}

/* ── Invariant 3: no double-claim of a playing slot / no steal ─────────── */
void test_lookahead_no_double_claim_playing(void)
{
    robodog_voice_la_t s;
    robodog_voice_lookahead_start(&s, 5);

    /* While slot 0 plays, claim() must return the OTHER slot (1) — never 0. */
    robodog_voice_lookahead_begin_play(&s, 1, 0);
    CHECK(robodog_voice_lookahead_claim(&s, 2) == 1,
          "playing slot 0 -> claim must return slot 1, got %d",
          robodog_voice_lookahead_claim(&s, 2));
    robodog_voice_lookahead_commit(&s); /* slot 1 now holds chunk 2 */
    /* No steal: a re-claim of the JUST-BUFFERED index (already in the other
     * slot) is refused — the prefetch would otherwise overwrite the buffered
     * next chunk before it plays. */
    CHECK(robodog_voice_lookahead_claim(&s, 2) == -1,
          "claim of the already-buffered index MUST refuse (no steal)");
    robodog_voice_lookahead_end_play(&s, 1, 0); /* slot 0 freed for reuse */

    /* Mirror: while slot 1 plays, claim() returns slot 0. */
    robodog_voice_lookahead_begin_play(&s, 2, 1);
    CHECK(robodog_voice_lookahead_claim(&s, 3) == 0,
          "playing slot 1 -> claim must return slot 0, got %d",
          robodog_voice_lookahead_claim(&s, 3));
    robodog_voice_lookahead_commit(&s); /* slot 0 now holds chunk 3 */
    CHECK(robodog_voice_lookahead_claim(&s, 3) == -1,
          "no steal of the just-buffered chunk 3 either");
    robodog_voice_lookahead_end_play(&s, 2, 1);
    /* After the play ends, the freed slot is reusable for the next pick. */
    robodog_voice_lookahead_begin_play(&s, 3, 0);
    CHECK(robodog_voice_lookahead_claim(&s, 4) == 1,
          "slot 1 freed by end_play -> claim 4 lands in slot 1, got %d",
          robodog_voice_lookahead_claim(&s, 4));
    robodog_voice_lookahead_commit(&s);
    robodog_voice_lookahead_end_play(&s, 3, 0);
}

/* ── Invariant 4: clean fail/abort, start() fully resets ───────────────── */
void test_lookahead_fail_abort(void)
{
    robodog_voice_la_t s;
    robodog_voice_lookahead_start(&s, 5);
    robodog_voice_lookahead_begin_play(&s, 1, 0);
    CHECK(robodog_voice_lookahead_claim(&s, 2) == 1, "claim 2 into slot 1");
    robodog_voice_lookahead_fail(&s);
    /* fail(): slot free, fetch_fail latched, in-flight cleared, no slot claims. */
    CHECK(s.fetch_fail == 1, "fail latches fetch_fail");
    CHECK(robodog_voice_lookahead_has_inflight(&s) == 0,
          "fail clears in-flight");
    CHECK(s.slot_ix[1] == 0, "failed slot must stay free (nothing committed)");
    CHECK(robodog_voice_lookahead_prefetched(&s, 2) == -1,
          "no committed slot for the failed index");
    CHECK(robodog_voice_lookahead_claim(&s, 3) == 1,
          "next claim works after a fail (slot 1 free again)");
    robodog_voice_lookahead_commit(&s); /* slot 1 holds chunk 3 */
    robodog_voice_lookahead_end_play(&s, 1, 0);

    /* abort(): clears in-flight (and records aborted) so the caller can free
     * buffers; slot/playing contents left for the next start() to wipe. */
    robodog_voice_lookahead_begin_play(&s, 2, 0); /* chunk 2 sync-fetched here */
    CHECK(robodog_voice_lookahead_claim(&s, 4) == 1,
          "claim 4 into slot 1 (slot 1 holds 3 != 4), got %d",
          robodog_voice_lookahead_claim(&s, 4));
    CHECK(robodog_voice_lookahead_has_inflight(&s) == 1, "in-flight set");
    robodog_voice_lookahead_abort(&s);
    CHECK(robodog_voice_lookahead_has_inflight(&s) == 0,
          "abort clears in-flight (caller may now free buffers safely)");
    CHECK(s.aborted == 1, "abort records the aborted diagnostic");

    /* start() is the single reset point: a fresh turn inherits NOTHING. */
    robodog_voice_lookahead_start(&s, 3);
    CHECK(s.fetch_fail == 0, "start() clears the fetch_fail latch");
    CHECK(s.aborted == 0, "start() clears the aborted flag");
    CHECK(robodog_voice_lookahead_has_inflight(&s) == 0,
          "start() clears any in-flight");
    CHECK(s.inflight_slot == -1, "start() returns inflight_slot to -1");
    CHECK(s.next_play == 1, "start() resets next_play to 1");
    CHECK(s.slot_ix[0] == 0 && s.slot_ix[1] == 0,
          "start() frees both slots");
    CHECK(s.playing[0] == 0 && s.playing[1] == 0,
          "start() clears both playing markers");
    CHECK(s.total == 3, "start() sets the turn total");
}

/* ── Invariant 5: full-turn accounting (firmware turn sequence) ────────── */
/* Drive the EXACT call sequence the double-buffer turn loop makes for a
 * 5-chunk reply and prove each index is played exactly once, in order, with
 * no in-flight / no leftover slot accounting at the end. */
void test_lookahead_full_turn(void)
{
    robodog_voice_la_t s;
    const size_t total = 5;
    robodog_voice_lookahead_start(&s, total);

    /* Chunk 1 is fetched synchronously into slot 0, then plays (slot 0). */
    robodog_voice_lookahead_begin_play(&s, 1, 0);
    /* ... while it plays, arm the lookahead for chunk 2 into the free slot. */
    CHECK(robodog_voice_lookahead_claim(&s, 2) == 1,
          "turn: claim 2 while 1 plays -> slot 1");
    robodog_voice_lookahead_commit(&s);
    robodog_voice_lookahead_end_play(&s, 1, 0);
    CHECK(robodog_voice_lookahead_prefetched(&s, 2) == 1,
          "turn: chunk 2 buffered in slot 1 (gapless swap)");

    for (size_t i = 2; i < total; i++) {
        const int slot = (int)((i % 2 == 0) ? 1 : 0);
        robodog_voice_lookahead_begin_play(&s, i, slot);
        /* exactly one fetch in flight at a time, strict order: the claim
         * always lands in the just-freed OTHER slot. */
        const int nxt = (int)robodog_voice_lookahead_claim(&s, i + 1);
        CHECK(nxt == 1 - slot, "turn: claim %zu while %zu plays -> slot %d "
              "(got %d)", i + 1, i, 1 - slot, nxt);
        robodog_voice_lookahead_commit(&s);
        /* while chunk i plays, the just-committed i+1 stays buffered. */
        CHECK(robodog_voice_lookahead_prefetched(&s, i + 1) == 1 - slot,
              "turn: chunk %zu buffered in slot %d during %zu's play",
              i + 1, 1 - slot, i);
        robodog_voice_lookahead_end_play(&s, i, slot);
    }

    /* Last chunk (5): begin/play, NO lookahead armed (i == n). */
    robodog_voice_lookahead_begin_play(&s, total, (int)((total - 1) % 2));
    CHECK(robodog_voice_lookahead_claim(&s, total + 1) == -1,
          "claim past the turn total MUST refuse");
    robodog_voice_lookahead_end_play(&s, total, (int)((total - 1) % 2));

    /* Full-turn accounting: every index played in order, nothing left over. */
    CHECK(s.next_play == total + 1,
          "turn end: next_play == n+1 (%zu), got %zu", total + 1, s.next_play);
    CHECK(robodog_voice_lookahead_has_inflight(&s) == 0,
          "turn end: no in-flight fetch");
    CHECK(s.slot_ix[0] == 0 && s.slot_ix[1] == 0,
          "turn end: both slots free (slot_ix cleared)");
    CHECK(s.playing[0] == 0 && s.playing[1] == 0,
          "turn end: nothing still playing");
    CHECK(robodog_voice_lookahead_prefetched(&s, 1) == -1 &&
              robodog_voice_lookahead_prefetched(&s, total) == -1,
          "turn end: no chunk index is still buffered (all accounted)");
}

/* ── n == 1: single-chunk reply, no prefetch ever armed ────────────────── */
void test_lookahead_single_chunk(void)
{
    robodog_voice_la_t s;
    robodog_voice_lookahead_start(&s, 1);
    robodog_voice_lookahead_begin_play(&s, 1, 0);
    CHECK(robodog_voice_lookahead_claim(&s, 2) == -1,
          "n==1: no lookahead may be armed (2 > total)");
    robodog_voice_lookahead_end_play(&s, 1, 0);
    CHECK(s.next_play == 2, "n==1: next_play reaches n+1 == 2, got %zu",
          s.next_play);
    CHECK(robodog_voice_lookahead_has_inflight(&s) == 0,
          "n==1: no in-flight");
    CHECK(s.slot_ix[0] == 0 && s.slot_ix[1] == 0, "n==1: both slots free");
}
