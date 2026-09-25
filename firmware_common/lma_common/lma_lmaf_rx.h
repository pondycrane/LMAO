#pragma once

#include <functional>
#include <map>
#include <string>
#include <vector>

#include "lma_attachment.h"

// Device-side LMAF receive glue.
//
// Takes the decrypted plaintext of an inbound LXMF message and drives the
// reassembler: manifest -> chunks -> whole-payload verification -> the
// AttachmentAck that must go back.  Nothing here touches Reticulum, ESP-IDF or
// crypto (sha256 is injected), so the host test drives exactly the logic the
// firmware runs — the device tree only feeds bytes in and transmits the
// returned envelope.  See proto/lma_messages.proto for the wire format and
// firmware_common/lma_common/lma_attachment.h for the framing core.
namespace lma_attachment {

    struct LmafRxConfig {
        uint32_t max_payload_bytes = 4096;   // reject transfers larger than this
        uint32_t max_chunk_size    = LO_OPP_CHUNK_SIZE * 4;  // tolerate a richer sender profile
        uint32_t max_sessions      = 2;
        uint32_t rx_window         = 4;      // chunks to ask for before acking (advisory)
        double   ttl_seconds       = 300.0;
        uint32_t lmaf_version      = 1;
        // Transfers this receiver has already delivered, remembered so a
        // sender's retry (RetryPolicy above) is answered with COMPLETE instead
        // of being downloaded a second time.  Bounded + TTL'd.
        uint32_t completed_memory   = 8;
        double   completed_ttl_seconds = 600.0;
        std::vector<uint32_t>        kinds  = { KIND_CHART };
        std::vector<std::string>     codecs = { "lmao:chart-line-v1" };
    };

    // Whole-payload digest check: given the reassembled payload and the
    // manifest's declared digest, decide whether it is intact.  The device
    // passes RTReticulum's sha256; the host test injects a recorder, keeping
    // this unit dependency-free.
    using VerifyFn = std::function<bool(const std::string& payload,
                                        const std::string& expected_digest)>;

    class LmafReceiver {
    public:
        enum class Result {
            IGNORED,            // not an LMAF message (or not for this receiver)
            MANIFEST_ACCEPTED,  // session opened (or resumed)
            MANIFEST_REJECTED,  // unusable manifest — REJECTED ack available
            CHUNK_ACCEPTED,     // stored, still missing chunks
            CHUNK_DUPLICATE,    // already held
            NEED_ACK,           // a chunk (or its manifest) must be resent
            COMPLETE,           // payload stored and digest-verified — or, for a
                                // manifest of an already-delivered transfer, the
                                // duplicate: re-acked so the sender stops
                                // retrying, nothing downloaded again
            HASH_MISMATCH,      // digest failed — ABORTED ack, session dropped
            NO_SESSION,         // chunk without a manifest, request suppressed
            IO_ERROR,           // local store refused the chunk — ABORTED ack
        };

        LmafReceiver(LmafRxConfig config, VerifyFn verify,
                     Reassembler::NowFn now = Reassembler::NowFn());

        // Feed one inbound LXM plaintext (source hash + signature + msgpack
        // payload, already decrypted by Reticulum).
        Result feed(const std::string& plaintext);

        // Valid after COMPLETE: the verified payload and its manifest.
        const std::string& payload() const { return _payload; }
        const Manifest&    manifest() const { return _manifest; }

        // Valid for MANIFEST_REJECTED, NEED_ACK, COMPLETE, HASH_MISMATCH and
        // IO_ERROR: the LMAOEnvelope to send back to the sender.
        bool                         has_ack() const { return _has_ack; }
        const std::string&           ack_envelope() const { return _ack; }
        uint32_t                     ack_status() const { return _ack_status; }
        const std::string&           ack_id() const { return _ack_id; }
        const std::vector<uint32_t>& ack_missing() const { return _ack_missing; }

        // Manifest-request policy: how many times a receiver will ask for a
        // missing manifest, and how long it waits between asks.
        uint32_t max_manifest_requests   = 3;
        double   manifest_request_interval = 5.0;

        // What this node accepts, for the sender to size its chunks.
        std::string capability_envelope() const;

        // True while `id` is remembered as already delivered (see
        // LmafRxConfig::completed_memory).  A manifest for such an id is
        // answered with COMPLETE and nothing is downloaded again.
        bool is_completed(const std::string& id);

        const LmafRxConfig& config() const { return _config; }

        // Drop an in-flight session (e.g. after the caller handled a failure).
        void reset(const std::string& id) { _reasm.reset(id); }

    private:
        Result on_manifest(const Manifest& m);
        Result on_chunk(const Decoded& d);
        // Rate-limited "resend the manifest" request; false when suppressed.
        bool   maybe_request_manifest(const std::string& id);
        void   set_ack(const std::string& id, uint32_t status,
                       const std::vector<uint32_t>& missing, const std::string& reason);

        LmafRxConfig _config;
        VerifyFn     _verify;
        Reassembler  _reasm;
        Reassembler::NowFn _now;

        // A chunk whose manifest never arrived can only be placed by asking for
        // the manifest again (the sender retransmits on NEED).  Bounded per id
        // so a lossy link cannot turn into a request storm.
        struct ManifestRequest {
            uint32_t count = 0;
            double   last  = 0.0;
        };
        std::map<std::string, ManifestRequest> _manifest_requests;

        // Already-delivered transfers (id -> expiry), so a sender's retry is
        // answered instead of re-downloaded.  Bounded by
        // LmafRxConfig::completed_memory, expiring after completed_ttl_seconds.
        std::map<std::string, double> _completed;
        bool remember_completed(const std::string& id);

        std::string _payload;
        Manifest    _manifest;

        bool                  _has_ack = false;
        std::string           _ack;
        std::string           _ack_id;
        uint32_t              _ack_status = 0;
        std::vector<uint32_t> _ack_missing;
    };

}
