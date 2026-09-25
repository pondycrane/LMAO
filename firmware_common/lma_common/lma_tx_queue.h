#pragma once

#include <cstdint>
#include <functional>
#include <string>
#include <vector>

#include "lma_attachment.h"

// LMAF outbound dead-letter queue — the *sender's* half of loss recovery.
//
// The receiver drives the normal repair path: a NEED names the chunks it is
// missing, an empty NEED asks for a lost manifest.  What no receiver can ever
// report is a packet it never saw at all, and on a half-duplex LoRa link
// silence is ambiguous anyway — the peer's *reply* may be the thing that was
// lost.  So the sender keeps what it transmitted and re-sends it on the shared
// RetryPolicy schedule until something proves delivery, then gives up loudly.
// Bounded on purpose: on a 1% duty-cycle band an unbounded retry is
// indistinguishable from a jammer.
//
// Confirmation is deliberately coarse, because that is all a link without
// delivery proofs can offer:
//   * state-like payloads (a sensor report) — *any* inbound message confirms
//     the oldest outstanding item, since the server only answers what it
//     accepted.  A newer report supersedes an unconfirmed older one (`kind`),
//     because latest state wins and only one should ever be in flight.
//   * keyed payloads (an attachment transfer) — confirm by key, matched
//     against the transfer id in the AttachmentAck.
// One queue serves both, so the schedule, the eviction policy and the give-up
// signal exist exactly once and are host-tested.
namespace lma_attachment {

    struct TxQueueConfig {
        uint32_t    capacity = 4;   // outstanding items; oldest evicted beyond
        RetryPolicy policy{};       // shared schedule (lma_attachment.h)
    };

    class TxQueue {
    public:
        using NowFn = std::function<double()>;

        enum class Action {
            NONE,     // nothing due
            RESEND,   // transmit *envelope again
            ABANDON,  // schedule exhausted — the item was dropped
        };

        explicit TxQueue(TxQueueConfig config = TxQueueConfig(), NowFn now = NowFn());

        // Record an envelope that was just transmitted.  Items sharing `kind`
        // supersede each other (the newer replaces the unconfirmed older).
        // Record an envelope that was just transmitted.  `supersede` is for
        // state-like payloads only (a newer sensor report replaces the
        // unconfirmed older one of the same `kind`: latest state wins).
        // Distinct attachment transfers must NOT supersede each other, hence
        // the explicit flag rather than grouping alone.
        void sent(const std::string& key, std::string envelope, uint32_t kind,
                  bool supersede = false);

        // Delivery proof.  confirm() matches a key (attachment acks);
        // confirm_oldest() takes any inbound message as proof of the oldest
        // outstanding item (reports), and returns false when nothing was queued.
        bool confirm(const std::string& key);
        bool confirm_oldest();

        // The one item that is due now.  RESEND restarts its timer and bumps its
        // attempt count; ABANDON removes it (key/envelope still reported so the
        // caller can log it).  Call once per loop tick.
        Action next_due(std::string* key, std::string* envelope);

        uint32_t size() const { return (uint32_t)_items.size(); }
        uint32_t resends() const { return _resends; }
        uint32_t abandoned() const { return _abandoned; }
        uint32_t evicted() const { return _evicted; }
        bool     contains(const std::string& key) const;
        void     clear();

        // The schedule in force (for logging the give-up budget).
        const TxQueueConfig& config() const { return _config; }

    private:
        struct Item {
            std::string key;
            std::string envelope;
            uint32_t    kind      = 0;
            uint32_t    attempts  = 0;
            double      last_sent = 0.0;
        };

        double now() const;

        TxQueueConfig     _config;
        NowFn             _now;
        std::vector<Item> _items;   // oldest first
        uint32_t          _resends   = 0;
        uint32_t          _abandoned = 0;
        uint32_t          _evicted   = 0;
    };

}
