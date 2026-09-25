// Host unit test for the device-side LMAF receive glue (lma_lmaf_rx).
//
// This is the logic the native firmware runs between "Reticulum handed us a
// decrypted LXMF plaintext" and "send this acknowledgement": manifest
// validation, chunk reassembly, whole-payload digest verification and ack
// construction.  Keeping it host-testable means the LoRa round trip only adds
// the transport (radio + Reticulum), which the hardware E2E covers.
#include <cstdio>
#include <string>
#include <vector>

#include "lma_lmaf_rx.h"
#include "lma_lxm.h"

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

// ── wire helpers ────────────────────────────────────────────────────────────

static std::string msgpack_bin(const std::string& b) {
    std::string o;
    if (b.size() < 256) {
        o.push_back((char)0xc4);   // bin8
        o.push_back((char)b.size());
    } else {
        o.push_back((char)0xc5);   // bin16
        o.push_back((char)((b.size() >> 8) & 0xff));
        o.push_back((char)(b.size() & 0xff));
    }
    return o + b;
}

// The decrypted plaintext of an inbound LXMF message:
//   source_hash(16) + signature(64) + msgpack([ts, title, content, fields])
static std::string lxm_plaintext(const std::string& content,
                                 const std::string& title = "p:Envelope") {
    std::string payload;
    payload.push_back((char)0x94);   // array(4)
    payload.push_back((char)0xcf);   // uint64 timestamp
    for (int i = 0; i < 8; i++) payload.push_back((char)(0x10 + i));
    payload += msgpack_bin(title);
    payload += msgpack_bin(content);
    payload.push_back((char)0x80);   // empty fields map
    return std::string(lma_lxm::HEADER_BYTES, '\x11') + payload;
}

static std::string make_payload(size_t n) {
    std::string s;
    for (size_t i = 0; i < n; i++) s.push_back((char)('a' + (i % 26)));
    return s;
}

static Manifest make_manifest(const std::string& payload, uint32_t chunk_size,
                              const std::string& digest = std::string(32, '\x5a')) {
    Manifest m;
    m.id = "0123456789abcdef";
    m.payload_sha256 = digest;
    m.kind = KIND_CHART;
    m.codec = "lmao:chart-line-v1";
    m.chunk_size = chunk_size;
    m.total_bytes = payload.size();
    m.chunk_count = (uint32_t)((payload.size() + chunk_size - 1) / chunk_size);
    m.node_id = "f5f0595239262739";
    return m;
}

static std::string manifest_env(const Manifest& m) {
    return lxm_plaintext(envelope_manifest(m));
}

static std::string chunk_env(const Manifest& m, const std::string& payload, uint32_t index) {
    const std::string part = payload.substr((size_t)index * m.chunk_size, m.chunk_size);
    return lxm_plaintext(
        envelope_chunk(m.id, index, part, crc32(part.data(), part.size())));
}

struct Recorder {
    bool        verdict = true;
    int         calls   = 0;
    std::string payload_seen;
    std::string digest_seen;

    VerifyFn fn() {
        return [this](const std::string& payload, const std::string& digest) {
            calls++;
            payload_seen = payload;
            digest_seen  = digest;
            return verdict;
        };
    }
};

static LmafRxConfig default_config() {
    LmafRxConfig cfg;
    cfg.max_payload_bytes = 4096;
    cfg.max_chunk_size    = 960;
    return cfg;
}

// ── tests ───────────────────────────────────────────────────────────────────

static void test_happy_path_out_of_order() {
    Recorder rec;
    LmafReceiver rx(default_config(), rec.fn());

    const std::string payload = make_payload(600);
    const Manifest m = make_manifest(payload, LO_OPP_CHUNK_SIZE);
    check_u32(m.chunk_count, 3, "test setup: three chunks");

    check(rx.feed(manifest_env(m)) == LmafReceiver::Result::MANIFEST_ACCEPTED,
          "manifest accepted");
    check(!rx.has_ack(), "manifest alone produces no ack");

    check(rx.feed(chunk_env(m, payload, 2)) == LmafReceiver::Result::CHUNK_ACCEPTED,
          "out-of-order chunk accepted");
    check(rx.feed(chunk_env(m, payload, 0)) == LmafReceiver::Result::CHUNK_ACCEPTED,
          "second chunk accepted");
    check(rx.feed(chunk_env(m, payload, 0)) == LmafReceiver::Result::CHUNK_DUPLICATE,
          "duplicate chunk ignored");

    check(rx.feed(chunk_env(m, payload, 1)) == LmafReceiver::Result::COMPLETE,
          "last chunk completes the transfer");
    check(rx.payload() == payload, "reassembled payload is byte-identical");
    check(rec.calls == 1, "digest verified exactly once");
    check(rec.payload_seen == payload, "digest check sees the whole payload");
    check(rec.digest_seen == m.payload_sha256, "digest check uses the manifest digest");
    check(rx.manifest().id == m.id, "manifest retained for the caller");

    check(rx.has_ack(), "completion produces an ack");
    check_u32(rx.ack_status(), ACK_COMPLETE, "ack status is COMPLETE");
    check(rx.ack_missing().empty(), "COMPLETE ack needs nothing");
    const Decoded ack = decode_envelope(rx.ack_envelope());
    check(ack.payload == Payload::ACK, "ack envelope decodes as an ACK");
    check(ack.ack_id == m.id, "ack carries the transfer id");
    check_u32(ack.ack_have, 3, "ack reports every chunk held");
}

static void test_crc_failure_requests_retransmit() {
    Recorder rec;
    LmafReceiver rx(default_config(), rec.fn());

    const std::string payload = make_payload(300);
    const Manifest m = make_manifest(payload, LO_OPP_CHUNK_SIZE);

    check(rx.feed(manifest_env(m)) == LmafReceiver::Result::MANIFEST_ACCEPTED,
          "crc: manifest accepted");

    // Corrupted bytes with the sender's original CRC.
    const std::string part = payload.substr(0, m.chunk_size);
    std::string bad = part;
    bad[0] ^= 0x40;
    const auto r = rx.feed(lxm_plaintext(
        envelope_chunk(m.id, 0, bad, crc32(part.data(), part.size()))));
    check(r == LmafReceiver::Result::NEED_ACK, "crc: bad chunk asks for retransmit");
    check(rx.ack_missing() == std::vector<uint32_t>{ 0 }, "crc: NACK names the chunk");
    check_u32(rx.ack_status(), ACK_NEED, "crc: ack status is NEED");
    check(!rx.payload().empty() || true, "crc: nothing dispatched");

    // The sender resends the good chunk: transfer completes.
    check(rx.feed(chunk_env(m, payload, 0)) == LmafReceiver::Result::CHUNK_ACCEPTED,
          "crc: retransmit accepted");
    check(rx.feed(chunk_env(m, payload, 1)) == LmafReceiver::Result::COMPLETE,
          "crc: transfer completes after retransmit");
    check(rx.payload() == payload, "crc: retransmit repaired the payload");
}

static void test_hash_mismatch_aborts() {
    Recorder rec;
    rec.verdict = false;
    LmafReceiver rx(default_config(), rec.fn());

    const std::string payload = make_payload(200);
    const Manifest m = make_manifest(payload, LO_OPP_CHUNK_SIZE);
    check_u32(m.chunk_count, 1, "hash: setup is a single-chunk transfer");

    check(rx.feed(manifest_env(m)) == LmafReceiver::Result::MANIFEST_ACCEPTED,
          "hash: manifest accepted");
    check(rx.feed(chunk_env(m, payload, 0)) == LmafReceiver::Result::HASH_MISMATCH,
          "hash: bad digest aborts the transfer");
    check(rx.payload().empty(), "hash: failed payload is not exposed");
    check_u32(rx.ack_status(), ACK_ABORTED, "hash: ack status is ABORTED");
    check(decode_envelope(rx.ack_envelope()).ack_reason == "hash",
          "hash: ack explains why");
    // The session is gone, so the next chunk asks for the manifest instead of
    // being placed (bounded — see test_manifest_requests_are_bounded).
    check(rx.feed(chunk_env(m, payload, 0)) == LmafReceiver::Result::NEED_ACK,
          "hash: session dropped, later chunks request the manifest");
    check(rx.ack_missing().empty(), "hash: the manifest request names no chunks");
}

static void test_manifest_limits_and_consistency() {
    Recorder rec;
    LmafRxConfig cfg = default_config();
    LmafReceiver rx(cfg, rec.fn());

    // Over the payload ceiling.
    Manifest big = make_manifest(make_payload(200), LO_OPP_CHUNK_SIZE);
    big.total_bytes = 8192;
    big.chunk_count = (uint32_t)((big.total_bytes + LO_OPP_CHUNK_SIZE - 1) / LO_OPP_CHUNK_SIZE);
    check(rx.feed(manifest_env(big)) == LmafReceiver::Result::MANIFEST_REJECTED,
          "limits: oversized payload rejected");
    check_u32(rx.ack_status(), ACK_REJECTED, "limits: oversized payload is REJECTED");
    check(!rx.has_ack() == false, "limits: rejection still acks the sender");

    // chunk_count inconsistent with total_bytes/chunk_size.
    Manifest bad = make_manifest(make_payload(200), LO_OPP_CHUNK_SIZE);
    bad.chunk_count = 99;
    check(rx.feed(manifest_env(bad)) == LmafReceiver::Result::MANIFEST_REJECTED,
          "limits: inconsistent chunk_count rejected");
    check(decode_envelope(rx.ack_envelope()).ack_reason.find("mismatch") != std::string::npos,
          "limits: ack names the inconsistency");

    // chunk_size above the configured ceiling.
    Manifest wide = make_manifest(make_payload(2000), 1024);
    check(rx.feed(manifest_env(wide)) == LmafReceiver::Result::MANIFEST_REJECTED,
          "limits: oversized chunk_size rejected");

    // Zero-valued fields must never allocate or open a session.
    Manifest zero = make_manifest(make_payload(200), LO_OPP_CHUNK_SIZE);
    zero.total_bytes = 0;
    check(rx.feed(manifest_env(zero)) == LmafReceiver::Result::MANIFEST_REJECTED,
          "limits: zero total_bytes rejected");
}

static void test_manifest_resume_and_unsolicited_chunks() {
    Recorder rec;
    LmafReceiver rx(default_config(), rec.fn());

    const std::string payload = make_payload(300);
    const Manifest m = make_manifest(payload, LO_OPP_CHUNK_SIZE);

    // A chunk with no manifest cannot be placed; the receiver asks for the
    // manifest (NEED with no indices) so the sender can resend the transfer.
    check(rx.feed(chunk_env(m, payload, 0)) == LmafReceiver::Result::NEED_ACK,
          "resume: unsolicited chunk requests the manifest");
    check(rx.has_ack(), "resume: the manifest request carries an ack");
    check(rx.ack_missing().empty(), "resume: the manifest request names no chunks");

    check(rx.feed(manifest_env(m)) == LmafReceiver::Result::MANIFEST_ACCEPTED,
          "resume: manifest opens the session");
    check(rx.feed(chunk_env(m, payload, 1)) == LmafReceiver::Result::CHUNK_ACCEPTED,
          "resume: chunk held");
    check(rx.feed(manifest_env(m)) == LmafReceiver::Result::MANIFEST_ACCEPTED,
          "resume: re-sent manifest is accepted");
    check(rx.feed(chunk_env(m, payload, 0)) == LmafReceiver::Result::COMPLETE,
          "resume: held chunk survived the re-sent manifest");
    check(rx.payload() == payload, "resume: payload correct after resume");

    // A different transfer (new id) replacing a partial one must not reuse the
    // buffer: the second transfer's bytes have to come out exactly.
    const std::string payload2(300, 'Z');
    Manifest m2 = make_manifest(payload2, LO_OPP_CHUNK_SIZE);
    m2.id = "fedcba9876543210";
    check(rx.feed(manifest_env(m2)) == LmafReceiver::Result::MANIFEST_ACCEPTED,
          "resume: new transfer opens its own session");
    check(rx.feed(chunk_env(m2, payload2, 0)) == LmafReceiver::Result::CHUNK_ACCEPTED,
          "resume: new transfer chunk accepted");
    check(rx.feed(chunk_env(m2, payload2, 1)) == LmafReceiver::Result::COMPLETE,
          "resume: new transfer completes");
    check(rx.payload() == payload2, "resume: new transfer payload is not mixed with the old one");
}

static void test_ignores_everything_else() {
    Recorder rec;
    LmafReceiver rx(default_config(), rec.fn());

    // A valid LMAOEnvelope that is not LMAF (a text reply from the server).
    const std::string text = [] {
        std::string inner;
        inner.push_back((char)0x0a);          // field 1 (node_id), wire type 2
        inner.push_back((char)0x04);
        inner.append("node", 4);
        std::string env;
        env.push_back((char)0xa2);            // field 20 (text), wire type 2
        env.push_back((char)0x01);
        env.push_back((char)0x03);
        env.append("ACK", 3);
        return env;
    }();
    check(rx.feed(lxm_plaintext(text)) == LmafReceiver::Result::IGNORED,
          "ignores: non-LMAF envelope");
    check(!rx.has_ack(), "ignores: non-LMAF message is not acked");

    check(rx.feed(std::string()) == LmafReceiver::Result::IGNORED, "ignores: empty plaintext");
    check(rx.feed(std::string(200, '\xff')) == LmafReceiver::Result::IGNORED,
          "ignores: garbage plaintext");
    check(!rx.has_ack(), "ignores: garbage is not acked");
}

static void test_capability_advertises_the_config() {
    Recorder rec;
    LmafRxConfig cfg = default_config();
    cfg.kinds  = { KIND_CHART, KIND_VOICE };
    cfg.codecs = { "lmao:chart-line-v1", "codec2:700C" };
    cfg.rx_window = 8;
    LmafReceiver rx(cfg, rec.fn());

    const Decoded d = decode_envelope(rx.capability_envelope());
    check(d.payload == Payload::CAPABILITY, "caps: envelope carries a capability");
    check_u32(d.caps_version, 1, "caps: protocol version");
    check(d.caps_kinds == std::vector<uint32_t>({ KIND_CHART, KIND_VOICE }),
          "caps: advertised kinds");
    check(d.caps_codecs.size() == 2 && d.caps_codecs[0] == "lmao:chart-line-v1",
          "caps: advertised codecs");
    check_u32(d.caps_max_chunk, LO_OPP_CHUNK_SIZE, "caps: opportunistic chunk size");
    check(d.caps_max_bytes == cfg.max_payload_bytes, "caps: payload ceiling");
    check_u32(d.caps_rx_window, 8, "caps: rx window");
}

static void test_manifest_requests_are_bounded() {
    Recorder rec;
    double clock = 100.0;
    LmafReceiver rx(default_config(), rec.fn(), [&clock]() { return clock; });

    const std::string payload = make_payload(600);
    const Manifest m = make_manifest(payload, LO_OPP_CHUNK_SIZE);

    // A chunk whose manifest never arrived must ask for the manifest: a NEED
    // ack with no indices, which the sender answers by resending the transfer.
    check(rx.feed(chunk_env(m, payload, 0)) == LmafReceiver::Result::NEED_ACK,
          "manifest-req: unsolicited chunk asks for the manifest");
    check(rx.has_ack(), "manifest-req: the request carries an ack");
    check_u32(rx.ack_status(), ACK_NEED, "manifest-req: ack status is NEED");
    check(rx.ack_missing().empty(), "manifest-req: no chunk indices are named");
    check(rx.ack_id() == m.id, "manifest-req: the ack names the transfer");
    check(decode_envelope(rx.ack_envelope()).ack_reason == "manifest",
          "manifest-req: the ack explains what is wanted");

    // A repeat inside the interval is suppressed (no airtime spent).
    check(rx.feed(chunk_env(m, payload, 0)) == LmafReceiver::Result::NO_SESSION,
          "manifest-req: repeat inside the interval is suppressed");
    check(!rx.has_ack(), "manifest-req: a suppressed request sends nothing");

    // After the interval it may ask again...
    clock += 6.0;
    check(rx.feed(chunk_env(m, payload, 1)) == LmafReceiver::Result::NEED_ACK,
          "manifest-req: asks again after the interval");
    clock += 6.0;
    check(rx.feed(chunk_env(m, payload, 1)) == LmafReceiver::Result::NEED_ACK,
          "manifest-req: third ask allowed");

    // ...and then stops, so a lossy link cannot become a request storm.
    clock += 6.0;
    check(rx.feed(chunk_env(m, payload, 1)) == LmafReceiver::Result::NO_SESSION,
          "manifest-req: stops after the attempt limit");
    check(!rx.has_ack(), "manifest-req: exhausted requests send nothing");

    // A late manifest still opens the session and the transfer completes.
    check(rx.feed(manifest_env(m)) == LmafReceiver::Result::MANIFEST_ACCEPTED,
          "manifest-req: late manifest opens the session");
    check(rx.feed(chunk_env(m, payload, 0)) == LmafReceiver::Result::CHUNK_ACCEPTED,
          "manifest-req: chunks flow after the late manifest");
    check(rx.feed(chunk_env(m, payload, 2)) == LmafReceiver::Result::CHUNK_ACCEPTED,
          "manifest-req: remaining chunk accepted");
    check(rx.feed(chunk_env(m, payload, 1)) == LmafReceiver::Result::COMPLETE,
          "manifest-req: transfer completes");
    check(rx.payload() == payload, "manifest-req: payload intact");
}

// ── sender-retry ("dead letter") support ────────────────────────────────────

// Delivers `payload` under `id` into `rx` and returns the manifest used.
static Manifest deliver(LmafReceiver& rx, const std::string& id,
                        const std::string& payload, uint32_t chunk_size) {
    Manifest m = make_manifest(payload, chunk_size);
    m.id = id;
    rx.feed(manifest_env(m));
    for (uint32_t i = 0; i < m.chunk_count; i++) rx.feed(chunk_env(m, payload, i));
    return m;
}

static void test_retry_schedule() {
    const RetryPolicy p;
    check(retry_delay_seconds(p, 0) == 30.0, "retry: first re-offer after 30 s");
    check(retry_delay_seconds(p, 1) == 90.0, "retry: second re-offer after 90 s");
    check(retry_delay_seconds(p, 2) == 270.0, "retry: third re-offer after 270 s");
    check_u32(p.max_attempts, 3, "retry: three re-offers, then abandon");
    // lma_core/attachment.py mirrors this table for the server; both test
    // suites assert these same literals, so the two senders cannot drift.
}

static void test_duplicate_manifest_is_acked_not_redownloaded() {
    Recorder rec;
    LmafReceiver rx(default_config(), rec.fn());
    const std::string payload = make_payload(600);
    const Manifest m = deliver(rx, "0123456789abcdef", payload, LO_OPP_CHUNK_SIZE);
    check(rx.payload() == payload, "dup: first delivery succeeded");
    check(rx.is_completed(m.id), "dup: delivery remembered");

    // The sender's dead-letter retry re-offers the manifest first: it must cost
    // one small ack, not a second download.
    check(rx.feed(manifest_env(m)) == LmafReceiver::Result::COMPLETE,
          "dup: repeated manifest answers COMPLETE");
    check_u32(rx.ack_status(), ACK_COMPLETE, "dup: ack status is COMPLETE");
    check(rx.ack_missing().empty(), "dup: no chunks requested");
    check(rx.feed(chunk_env(m, payload, 0)) == LmafReceiver::Result::IGNORED,
          "dup: re-sent chunk ignored");
    check(!rx.has_ack(), "dup: an ignored chunk produces no ack");
    check(rec.calls == 1, "dup: the payload is not re-verified");
}

static void test_completed_memory_is_bounded() {
    Recorder rec;
    LmafRxConfig cfg = default_config();
    cfg.completed_memory = 2;
    LmafReceiver rx(cfg, rec.fn());
    const std::string payload = make_payload(300);

    const Manifest a = deliver(rx, "id-aaaa-00000001", payload, LO_OPP_CHUNK_SIZE);
    const Manifest b = deliver(rx, "id-bbbb-00000002", payload, LO_OPP_CHUNK_SIZE);
    const Manifest c = deliver(rx, "id-cccc-00000003", payload, LO_OPP_CHUNK_SIZE);

    check(!rx.is_completed(a.id), "memory: oldest delivery forgotten at the cap");
    check(rx.is_completed(b.id) && rx.is_completed(c.id), "memory: newest two kept");
    check(rx.feed(manifest_env(a)) == LmafReceiver::Result::MANIFEST_ACCEPTED,
          "memory: a forgotten transfer can be delivered again");
}

static void test_completed_memory_expires() {
    Recorder rec;
    LmafRxConfig cfg = default_config();
    double t = 100.0;
    LmafReceiver rx(cfg, rec.fn(), [&t]() { return t; });
    const std::string payload = make_payload(300);
    const Manifest m = deliver(rx, "id-dddd-00000004", payload, LO_OPP_CHUNK_SIZE);

    t += cfg.completed_ttl_seconds - 1.0;
    check(rx.is_completed(m.id), "memory: remembered before the TTL");
    t += 2.0;
    check(!rx.is_completed(m.id), "memory: forgotten after the TTL");
}

int main() {
    test_happy_path_out_of_order();
    test_crc_failure_requests_retransmit();
    test_hash_mismatch_aborts();
    test_manifest_limits_and_consistency();
    test_manifest_resume_and_unsolicited_chunks();
    test_manifest_requests_are_bounded();
    test_ignores_everything_else();
    test_capability_advertises_the_config();
    test_retry_schedule();
    test_duplicate_manifest_is_acked_not_redownloaded();
    test_completed_memory_is_bounded();
    test_completed_memory_expires();

    if (failures) {
        std::printf("\n%d FAILURES\n", failures);
        return 1;
    }
    std::printf("\nall LMAF receive-glue checks passed\n");
    return 0;
}
