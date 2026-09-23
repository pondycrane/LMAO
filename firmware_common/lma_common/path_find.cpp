#include "path_find.h"

#include <cstring>
#include <exception>
#include <string>

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "rtreticulum/bytes.h"
#include "rtreticulum/cryptography/random.h"
#include "rtreticulum/destination.h"
#include "rtreticulum/identity.h"
#include "rtreticulum/packet.h"
#include "rtreticulum/transport.h"

static const char* TAG = "pathfind";

namespace {
    constexpr uint64_t MIN_INTERVAL_S = 20ULL;

    uint64_t now_s() { return (uint64_t)(esp_timer_get_time() / 1000000ULL); }

    RNS::Bytes hex_to_bytes(const char* hex) {
        RNS::Bytes out;
        if (!hex) return out;
        size_t len = strlen(hex);
        if (len % 2) return out;
        for (size_t i = 0; i < len; i += 2) {
            auto v = [](char c) -> uint8_t {
                if (c >= '0' && c <= '9') return (uint8_t)(c - '0');
                if (c >= 'a' && c <= 'f') return (uint8_t)(c - 'a' + 10);
                if (c >= 'A' && c <= 'F') return (uint8_t)(c - 'A' + 10);
                return 0;
            };
            uint8_t b = (uint8_t)((v(hex[i]) << 4) | v(hex[i + 1]));
            out.append(RNS::Bytes(&b, 1));
        }
        return out;
    }

    bool s_armed = true;
    uint64_t s_last = 0;
}

namespace path_find {

    bool can_request() {
        if (s_armed) { s_armed = false; return true; }
        if (now_s() - s_last >= MIN_INTERVAL_S) { s_last = now_s(); return true; }
        return false;
    }

    void request(const char* dst_hex) {
        RNS::Bytes dst = hex_to_bytes(dst_hex);
        if (dst.size() != 16) {
            ESP_LOGW(TAG, "bad destination hash");
            return;
        }
        if (!can_request()) return;

        try {
            // PLAIN control destination: rnstransport/path/request. Build its
            // hash exactly as RNS 1.3.x: hash = trunc(sha256( name_hash )),
            // name_hash = trunc16( sha256("rnstransport.path.request") ).
            // Use the precomputed-hash ctor — the identity-taking ctor calls
            // expand_name()->identity.hexhash() which aborts on a keyless Identity.
            RNS::Bytes name_bytes("rnstransport.path.request");
            RNS::Bytes name_hash =
                RNS::Identity::full_hash(name_bytes)
                    .left(RNS::Type::Identity::NAME_HASH_LENGTH / 8);
            RNS::Bytes dh = RNS::Identity::truncated_hash(name_hash);
            ESP_LOGI(TAG, "plain hash %s", dh.toHex().c_str());

            RNS::Identity no_keys(false);
            RNS::Destination pl(no_keys, RNS::Type::Destination::OUT,
                                RNS::Type::Destination::PLAIN, dh);
            ESP_LOGI(TAG, "S2 dest ok");

            RNS::Bytes tag = RNS::Cryptography::random(16);
            ESP_LOGI(TAG, "S3 random ok");
            RNS::Bytes payload; payload.append(dst); payload.append(tag);

            RNS::Packet pkt(pl, payload);   // DATA / CONTEXT_NONE / BROADCAST
            ESP_LOGI(TAG, "S4 packet ctor ok");
            pkt.pack();
            ESP_LOGI(TAG, "S5 pack ok frame=%uB", (unsigned)pkt.raw().size());
            RNS::Transport::broadcast(pkt.raw(), nullptr);
            ESP_LOGI(TAG, "S6 broadcast ok");
        } catch (const std::exception& e) {
            ESP_LOGE(TAG, "path-request failed: %s", e.what());
        }
    }

}
