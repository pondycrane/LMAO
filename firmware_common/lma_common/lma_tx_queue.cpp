#include "lma_tx_queue.h"

#include <algorithm>

namespace lma_attachment {

    TxQueue::TxQueue(TxQueueConfig config, NowFn now)
        : _config(config), _now(now) {}

    double TxQueue::now() const {
        return _now ? _now() : 0.0;
    }

    void TxQueue::sent(const std::string& key, std::string envelope, uint32_t kind,
                       bool supersede) {
        // A newer item of the same kind supersedes the unconfirmed older one:
        // for state-like payloads the latest reading is the only one worth
        // re-sending, and it keeps a permanently unreachable peer from filling
        // the queue with stale copies of the same telemetry.
        if (supersede) {
            _items.erase(std::remove_if(_items.begin(), _items.end(),
                                        [kind](const Item& i) { return i.kind == kind; }),
                         _items.end());
        }

        Item item;
        item.key       = key;
        item.envelope  = std::move(envelope);
        item.kind      = kind;
        item.attempts  = 0;
        item.last_sent = now();
        _items.push_back(std::move(item));

        while (_items.size() > (size_t)_config.capacity) {
            _items.erase(_items.begin());
            _evicted++;
        }
    }

    bool TxQueue::confirm(const std::string& key) {
        for (auto it = _items.begin(); it != _items.end(); ++it) {
            if (it->key == key) {
                _items.erase(it);
                return true;
            }
        }
        return false;
    }

    bool TxQueue::confirm_oldest() {
        if (_items.empty()) return false;
        _items.erase(_items.begin());
        return true;
    }

    TxQueue::Action TxQueue::next_due(std::string* key, std::string* envelope) {
        if (_items.empty()) return Action::NONE;

        const double t = now();
        for (auto it = _items.begin(); it != _items.end(); ++it) {
            if (t - it->last_sent < retry_delay_seconds(_config.policy, it->attempts)) {
                continue;
            }
            if (it->attempts >= _config.policy.max_attempts) {
                if (key) *key = it->key;
                if (envelope) *envelope = it->envelope;
                _items.erase(it);
                _abandoned++;
                return Action::ABANDON;
            }
            it->attempts++;
            it->last_sent = t;
            if (key) *key = it->key;
            if (envelope) *envelope = it->envelope;
            _resends++;
            return Action::RESEND;
        }
        return Action::NONE;
    }

    bool TxQueue::contains(const std::string& key) const {
        for (const Item& i : _items) {
            if (i.key == key) return true;
        }
        return false;
    }

    void TxQueue::clear() {
        _items.clear();
    }

}
