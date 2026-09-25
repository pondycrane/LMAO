#pragma once

#include <chrono>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <map>
#include <string>
#include <vector>

// LMAF — LMAO Attachment Framing.
//
// Transport-agnostic split/reassemble framing for payloads that do not fit a
// single LXMF packet (chart histories, voice notes, photos, audio, video).
// Wire-compatible with proto/lma_messages.proto fields 40/41/42/50 — see that
// file for the protocol invariants.  Read those before changing anything here.
//
// Canonical copy (DRY): both native firmware trees compile this file (each
// build.sh stages firmware_common/lma_common into its components/), and the
// host test //firmware_common:lma_attachment_test pins its bytes to the
// protobuf-generated encoding, so the MCU encoder, the server (protobuf) and
// the MicroPython client cannot drift apart.
//
// This unit is pure C++ (no RTReticulum, no ESP-IDF): the host test exercises
// exactly the code the devices run.
namespace lma_attachment {

    // AttachmentKind
    enum Kind : uint32_t {
        KIND_UNSPECIFIED = 0,
        KIND_VOICE       = 1,
        KIND_AUDIO       = 2,
        KIND_IMAGE       = 3,
        KIND_VIDEO       = 4,
        KIND_FILE        = 5,
        KIND_CHART       = 6,
        KIND_SIGNAL      = 7,
    };

    // AttachmentAck.Status
    enum AckStatus : uint32_t {
        ACK_RECEIVING = 0,
        ACK_COMPLETE  = 1,
        ACK_NEED      = 2,
        ACK_REJECTED  = 3,
        ACK_ABORTED   = 4,
    };

    // Encoded-chunk budget.  A LMAF envelope MUST ride in one opportunistic
    // LXMF packet: LXMF packs msgpack([ts, title, content, fields]) and caps
    // that frame at ENCRYPTED_PACKET_MAX_CONTENT(295) + TIMESTAMP_SIZE(8) +
    // STRUCT_OVERHEAD(8) = 311 B.  With a "p:Envelope" title (12 B) and the
    // array/map framing (14 B) that leaves 285 B for the LMAOEnvelope, and a
    // chunk envelope costs 30 B + data[].  Hence:
    //   * 240 B chunk data  -> 270 B envelope, 15 B of slack for new fields
    //   * 285 B             -> hard cap asserted by the host test
    // Do NOT raise LO_OPP_CHUNK_SIZE without redoing that arithmetic: exceeding
    // it makes LXMF silently fall back to link delivery, which a LoRa leaf node
    // cannot service (see AGENTS.md / README on the half-duplex leaf).
    static const uint32_t LO_OPP_CHUNK_SIZE  = 240;
    static const uint32_t MAX_ENVELOPE_BYTES = 285;

    // ── Sender-side retry ("dead letter") policy ────────────────────────────
    //
    // Normal LMAF recovery is receiver-driven: a peer holding some chunks names
    // the missing ones in a NEED, and a peer holding none asks for the manifest.
    // What that cannot cover is a transfer the peer never noticed at all — every
    // frame lost, so no ack of any kind comes back.  A sender that stops there
    // loses the transfer silently (fine for a chart the next report
    // regenerates, fatal for a voice note).
    //
    // So every sender re-offers the *manifest* — the recovery primitive, one
    // packet: a peer holding chunks answers with a NEED, a peer holding none
    // asks for the manifest again.  The schedule is bounded because on a 1%
    // duty-cycle band an unbounded retry is indistinguishable from a jammer.
    //
    // This table is the single source of truth: lma_core/attachment.py mirrors
    // it for the server, and both test suites assert the same literal sequence.
    struct RetryPolicy {
        uint32_t max_attempts        = 3;     // re-offers after the first send
        double   first_delay_seconds = 30.0;  // wait before re-offer 0
        double   backoff_factor      = 3.0;   // 30 s, 90 s, 270 s
    };

    // Seconds to wait before re-offer `attempt` (0-based): first_delay *
    // backoff^attempt.
    double retry_delay_seconds(const RetryPolicy& policy, uint32_t attempt);

    struct Manifest {
        std::string id;              // sha256(payload)[:16] — session key
        std::string payload_sha256;  // full 32 B digest, verified after assembly
        uint32_t    kind         = KIND_UNSPECIFIED;
        std::string codec;           // "codec2:700C", "lmao:chart-line-v1", ...
        uint32_t    chunk_size   = 0;
        uint32_t    chunk_count  = 0;
        uint64_t    total_bytes  = 0;
        uint32_t    sample_rate  = 0;
        uint32_t    channels     = 0;
        uint32_t    duration_ms  = 0;
        uint32_t    width        = 0;
        uint32_t    height       = 0;
        std::string node_id;
        uint64_t    created_ms   = 0;
    };

    // ── Encoders ────────────────────────────────────────────────────────────
    // Protobuf wire format, ascending field order, default-valued scalars
    // omitted — byte-identical to the protobuf-generated encoders (map-valued
    // Manifest.meta is server-only: the device never emits it, and the decoder
    // skips unknown fields).
    std::string encode_manifest(const Manifest& m);
    std::string encode_chunk(const std::string& id, uint32_t index,
                             const std::string& data, uint32_t crc);
    std::string encode_ack(const std::string& id, uint32_t status, uint32_t have_count,
                           const std::vector<uint32_t>& missing,
                           const std::string& reason = std::string());
    std::string encode_capability(uint32_t lmaf_version, const std::vector<uint32_t>& kinds,
                                  const std::vector<std::string>& codecs,
                                  uint32_t max_chunk_size, uint64_t max_attachment_bytes,
                                  uint32_t rx_window, uint32_t airtime_budget_bps = 0);

    // LMAOEnvelope wrappers (oneof field 40 = manifest, 41 = chunk, 42 = ack,
    // 50 = capability).  These are what actually goes in LXMF content.
    std::string envelope_manifest(const Manifest& m);
    std::string envelope_chunk(const std::string& id, uint32_t index,
                               const std::string& data, uint32_t crc);
    std::string envelope_ack(const std::string& id, uint32_t status, uint32_t have_count,
                             const std::vector<uint32_t>& missing,
                             const std::string& reason = std::string());
    std::string envelope_capability(uint32_t lmaf_version, const std::vector<uint32_t>& kinds,
                                    const std::vector<std::string>& codecs,
                                    uint32_t max_chunk_size, uint64_t max_attachment_bytes,
                                    uint32_t rx_window, uint32_t airtime_budget_bps = 0);

    // ── Decoding ────────────────────────────────────────────────────────────
    enum class Payload {
        NONE,        // empty or unparsable — ignore
        MANIFEST,    // envelope payload is LMAF (one of the four below)
        CHUNK,
        ACK,
        CAPABILITY,
        OTHER,       // valid LMAOEnvelope, not LMAF (sensor/text/command/...)
    };

    struct Decoded {
        Payload     payload = Payload::NONE;
        Manifest    manifest;
        std::string chunk_id;
        uint32_t    chunk_index = 0;
        std::string chunk_data;
        uint32_t    chunk_crc32 = 0;
        std::string ack_id;
        uint32_t    ack_status  = 0;
        uint32_t    ack_have    = 0;
        std::vector<uint32_t> ack_missing;
        std::string ack_reason;
        uint32_t    caps_version  = 0;
        uint32_t    caps_max_chunk = 0;
        uint64_t    caps_max_bytes = 0;
        uint32_t    caps_rx_window = 0;
        uint32_t    caps_airtime_bps = 0;
        std::vector<uint32_t>    caps_kinds;
        std::vector<std::string> caps_codecs;
    };

    // Never throws; unparsable input yields Payload::NONE.
    Decoded decode_envelope(const std::string& bytes);

    // CRC-32 (reflected, zlib/uzlib polynomial 0xEDB88320) over `len` bytes.
    uint32_t crc32(const void* data, size_t len, uint32_t seed = 0);

    // ── Reassembly ──────────────────────────────────────────────────────────
    //
    // Chunks are streamed to a sink rather than accumulated: a device writes
    // straight to flash at index * chunk_size, so a 64 KB payload never lands
    // in RAM.  The sink must support random-access writes and return false on
    // I/O failure.
    struct Limits {
        uint32_t max_total_bytes = 64u * 1024u;
        uint32_t max_chunk_size  = 4096;
        uint32_t max_sessions    = 4;
        double   ttl_seconds     = 900.0;
    };

    class Reassembler {
    public:
        using Sink = std::function<bool(uint32_t index, const char* data, size_t len)>;
        using NowFn = std::function<double()>;

        enum class Outcome {
            ACCEPTED,      // stored (COMPLETE is signalled separately)
            DUPLICATE,     // already held — ignored
            COMPLETE,      // last outstanding chunk stored; verify the digest now
            NO_SESSION,    // no manifest for this id (or it expired)
            BAD_CRC,       // chunk CRC mismatch — not stored
            BAD_INDEX,     // index >= chunk_count
            TOO_LARGE,     // chunk data exceeds manifest.chunk_size
            IO_ERROR,      // sink refused the write
            BAD_MANIFEST,  // inconsistent or over-limit manifest
        };

        explicit Reassembler(const Limits& limits = Limits(), NowFn now = NowFn());

        // Start (or resume) a session.  Re-sending an identical manifest keeps
        // the chunks already held; a manifest whose shape differs resets it.
        Outcome begin(const Manifest& m, Sink sink, std::string* err = nullptr);

        Outcome on_chunk(const std::string& id, uint32_t index,
                         const std::string& data, uint32_t crc);

        std::vector<uint32_t> missing(const std::string& id) const;
        uint32_t have_count(const std::string& id) const;
        bool     complete(const std::string& id) const;
        bool     manifest(const std::string& id, Manifest* out) const;

        void reset(const std::string& id);
        void reset_all();
        // Drop partial sessions idle for longer than limits().ttl_seconds.
        void expire();
        uint32_t session_count() const;

    private:
        struct Session {
            Manifest          man;
            Sink              sink;
            std::vector<bool> got;
            uint32_t          have    = 0;
            double            updated = 0.0;
        };

        double now() const;
        Session* find(const std::string& id);

        Limits                        _limits;
        NowFn                         _now;
        std::map<std::string, Session> _sessions;
    };

}
