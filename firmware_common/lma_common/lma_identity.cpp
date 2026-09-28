#include "lma_identity.h"

#include "esp_log.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "rtreticulum/destination.h"
#include "rtreticulum/identity.h"
#include "rtreticulum/type.h"

static const char* TAG = "lma_identity";

// Decode one hex character, or -1 when not a hex digit.
static int hexval(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

namespace lma_identity {

    RNS::Identity load_or_create(const char* ns, const char* baked_private_hex) {
        RNS::Identity identity;
        nvs_handle_t nv = 0;
        const bool nvs_ok = (nvs_open(ns, NVS_READWRITE, &nv) == ESP_OK);

        // Pinned identity (no-drift, lma_core/client_identity.py): a 64-byte
        // private-key hex baked in by install_all (LMAO_NODE_IDENTITY_HEX)
        // wins over NVS so a re-flash or NVS erase cannot silently change the
        // node's lxmf/delivery hash.  It is also written back to NVS so the
        // pin survives a later build without the define.
        if (baked_private_hex != nullptr && baked_private_hex[0] != '\0') {
            uint8_t key[64];
            const char* p = baked_private_hex;
            size_t n = 0;
            bool hex_ok = true;
            for (; n < sizeof(key) && p[0] && p[1]; n += 1, p += 2) {
                const int hi = hexval(p[0]);
                const int lo = hexval(p[1]);
                if (hi < 0 || lo < 0) { hex_ok = false; break; }
                key[n] = static_cast<uint8_t>((hi << 4) | lo);
            }
            if (hex_ok && n == sizeof(key) && p[0] == '\0') {
                RNS::Identity pinned(false);
                pinned.load_private_key(RNS::Bytes(key, sizeof(key)));
                identity = pinned;
                ESP_LOGI(TAG, "using pinned identity from build define (no drift)");
                if (nvs_ok) {
                    nvs_set_blob(nv, "identity64", key, sizeof(key));
                    nvs_commit(nv);
                    nvs_close(nv);
                }
                return identity;
            }
            ESP_LOGW(TAG, "LMAO_NODE_IDENTITY_HEX is not a 64-byte hex key "
                          "(%u bytes parsed) — falling back to NVS persistence",
                     (unsigned)n);
        }

        // No usable pinned identity: load-or-mint in NVS (original behaviour).
        if (nvs_ok) {
            uint8_t key[64];
            size_t len = sizeof(key);
            if (nvs_get_blob(nv, "identity64", key, &len) == ESP_OK && len == 64) {
                RNS::Identity loaded(false);
                loaded.load_private_key(RNS::Bytes(key, 64));
                identity = loaded;
                ESP_LOGI(TAG, "loaded persisted identity from NVS");
            } else {
                identity = RNS::Identity(true);
                RNS::Bytes pk = identity.get_private_key();
                nvs_set_blob(nv, "identity64", pk.data(), pk.size());
                nvs_commit(nv);
                ESP_LOGI(TAG, "generated and persisted identity to NVS");
            }
            nvs_close(nv);
        } else {
            identity = RNS::Identity(true);
        }
        return identity;
    }

    std::string hexstr(const void* data, size_t len) {
        static const char* H = "0123456789abcdef";
        const uint8_t* b = static_cast<const uint8_t*>(data);
        std::string s;
        s.reserve(len * 2);
        for (size_t i = 0; i < len; ++i) {
            s.push_back(H[b[i] >> 4]);
            s.push_back(H[b[i] & 0x0f]);
        }
        return s;
    }

    std::string delivery_hash(const RNS::Identity& identity) {
        RNS::Destination dm(identity, RNS::Type::Destination::OUT,
                            RNS::Type::Destination::SINGLE, "lxmf", "delivery");
        return hexstr(dm.hash());
    }

}
