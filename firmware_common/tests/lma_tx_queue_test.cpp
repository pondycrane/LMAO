// Host unit test for the LMAF outbound dead-letter queue.
//
// This is the sender-side loss recovery both device trees (and, in policy, the
// server) run: keep what was transmitted, re-send it on the shared schedule
// until something proves delivery, then give up loudly.  Keeping it
// host-testable means the only part left to hardware is the radio itself.
#include <cstdio>
#include <string>

#include "lma_tx_queue.h"

using namespace lma_attachment;

static int failures = 0;

static void check(bool ok, const char* label) {
    if (ok) {
        std::printf("ok   %s\n", label);
        return;
    }
    std::printf("FAIL %s\n", label);
    failures++;
}

static void check_u32(uint32_t actual, uint32_t expected, const char* label) {
    if (actual == expected) {
        std::printf("ok   %s\n", label);
        return;
    }
    std::printf("FAIL %s (actual %u, expected %u)\n", label, (unsigned)actual,
                (unsigned)expected);
    failures++;
}

static const uint32_t KIND_REPORT = 1;   // state-like: newer supersedes older
static const uint32_t KIND_XFER   = 2;   // keyed: matched by transfer id

static void test_first_send_waits_for_the_policy_delay() {
    double t = 100.0;
    TxQueue q(TxQueueConfig{}, [&t]() { return t; });
    q.sent("report-1", "ENV-1", KIND_REPORT, /*supersede=*/true);
    check_u32(q.size(), 1, "queue holds the sent envelope");

    std::string key, env;
    check(q.next_due(&key, &env) == TxQueue::Action::NONE,
          "nothing is due before the policy delay");
    check_u32(q.resends(), 0, "no resend counted yet");

    t += 31.0;   // policy: first re-offer after 30 s
    check(q.next_due(&key, &env) == TxQueue::Action::RESEND, "due after the delay");
    check(key == "report-1", "resend names the queued key");
    check(env == "ENV-1", "resend carries the identical bytes");
    check_u32(q.resends(), 1, "resend counted");
}

static void test_schedule_is_the_shared_policy() {
    double t = 0.0;
    TxQueue q(TxQueueConfig{}, [&t]() { return t; });
    q.sent("k", "E", KIND_XFER);

    std::string key, env;
    t = 31.0;
    check(q.next_due(&key, &env) == TxQueue::Action::RESEND, "policy: 1st re-send ~30 s");
    t = 31.0 + 89.0;
    check(q.next_due(&key, &env) == TxQueue::Action::NONE, "policy: not early at 89 s");
    t = 31.0 + 91.0;
    check(q.next_due(&key, &env) == TxQueue::Action::RESEND, "policy: 2nd re-send ~90 s");
    t = 122.0 + 269.0;
    check(q.next_due(&key, &env) == TxQueue::Action::NONE, "policy: not early at 269 s");
    t = 122.0 + 271.0;
    check(q.next_due(&key, &env) == TxQueue::Action::RESEND, "policy: 3rd re-send ~270 s");

    // Budget spent: the next due tick abandons instead of transmitting again.
    t += 1000.0;
    check(q.next_due(&key, &env) == TxQueue::Action::ABANDON, "policy: then give up");
    check(key == "k" && env == "E", "give-up reports what was dropped");
    check_u32(q.abandoned(), 1, "abandon counted");
    check_u32(q.size(), 0, "abandoned item is gone");
    check(q.next_due(&key, &env) == TxQueue::Action::NONE, "empty queue stays quiet");
}

static void test_confirmation_paths() {
    double t = 0.0;
    TxQueue q(TxQueueConfig{}, [&t]() { return t; });

    q.sent("xfer-abc", "E1", KIND_XFER);
    check(q.confirm("xfer-abc"), "confirm by key clears the item");
    check(!q.confirm("xfer-abc"), "confirming twice is a no-op");
    check_u32(q.size(), 0, "queue empty after confirm");

    q.sent("report-1", "E2", KIND_REPORT, /*supersede=*/true);
    q.sent("xfer-def", "E3", KIND_XFER);
    check(q.confirm_oldest(), "any inbound message proves the oldest item");
    check_u32(q.size(), 1, "only the oldest was cleared");
    check(q.contains("xfer-def"), "the newer item survives");
    check(q.confirm_oldest(), "the last item drains oldest-first");
    check_u32(q.size(), 0, "nothing left outstanding");
}

static void test_newer_state_supersedes_the_older() {
    double t = 0.0;
    TxQueue q(TxQueueConfig{}, [&t]() { return t; });
    q.sent("report-1", "OLD", KIND_REPORT, /*supersede=*/true);
    q.sent("report-2", "NEW", KIND_REPORT, /*supersede=*/true);

    check_u32(q.size(), 1, "only one report is ever in flight");
    check(!q.contains("report-1"), "the unconfirmed older report was replaced");

    std::string key, env;
    t += 31.0;
    check(q.next_due(&key, &env) == TxQueue::Action::RESEND, "the newer report is due");
    check(env == "NEW", "the newest reading is what gets re-sent");
}

static void test_capacity_evicts_the_oldest() {
    double t = 0.0;
    TxQueueConfig cfg;
    cfg.capacity = 2;
    TxQueue q(cfg, [&t]() { return t; });
    q.sent("a", "A", KIND_XFER);
    q.sent("b", "B", KIND_XFER);
    q.sent("c", "C", KIND_XFER);

    check_u32(q.size(), 2, "capacity respected");
    check_u32(q.evicted(), 1, "eviction counted");
    check(!q.contains("a"), "oldest evicted first");
    check(q.contains("b") && q.contains("c"), "newest kept");
}

static void test_abandon_leaves_other_items_alone() {
    double t = 0.0;
    TxQueue q(TxQueueConfig{}, [&t]() { return t; });
    q.sent("stale", "S", KIND_XFER);

    // Walk the stale item all the way through the shared schedule.
    std::string key, env;
    for (double step : {31.0, 91.0, 271.0}) {
        t += step;
        check(q.next_due(&key, &env) == TxQueue::Action::RESEND, "the stale item re-sends");
    }
    // A fresh item arrives while the stale one has spent its budget.
    q.sent("fresh", "F", KIND_XFER);
    t += 1000.0;
    check(q.next_due(&key, &env) == TxQueue::Action::ABANDON, "the stale item gives up");
    check(key == "stale", "and it is the one reported");
    check(q.contains("fresh"), "the fresh item is untouched");
    check_u32(q.size(), 1, "queue keeps the fresh item");
}

int main() {
    test_first_send_waits_for_the_policy_delay();
    test_schedule_is_the_shared_policy();
    test_confirmation_paths();
    test_newer_state_supersedes_the_older();
    test_capacity_evicts_the_oldest();
    test_abandon_leaves_other_items_alone();

    if (failures) {
        std::printf("\n%d FAILURES\n", failures);
        return 1;
    }
    std::printf("\nall LMAF dead-letter queue checks passed\n");
    return 0;
}
