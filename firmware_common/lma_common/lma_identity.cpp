#include "lma_identity.h"

#include "esp_log.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "rtreticulum/destination.h"
#include "rtreticulum/identity.h"
#include "rtreticulum/type.h"

static const char* TAG = "lma_identity";

namespace lma_identity {

    RNS::Identity load_or_create(const char* ns) {
        RNS::Identity identity;
        nvs_handle_t nv = 0;
        if (nvs_open(ns, NVS_READWRITE, &nv) == ESP_OK) {
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
