// Calendar networking over RNS-TCP / LXMF — portable C++ (compiles on POSIX
// hosts and under ESP-IDF; no ESP-IDF includes except logging, guarded by
// #ifdef ESP_PLATFORM so the host interop binary can link it too).
//
// Request path:  CalendarRequest wrapped in LMAOEnvelope -> LXMF body
//                (dest16 + src16 + sig64 + msgpack) -> opportunistic RNS
//                packet to the server's lxmf/delivery destination.
// Reply path:    server LXM arrives at our IN lxmf/delivery destination's
//                packet callback: src16 + sig64 + msgpack([ts, title,
//                content, fields]) where content is the LMAOEnvelope reply
//                (field 24 = CalendarBundle).
#include "calendar_net.h"

#include <cstdint>
#include <cstdio>
#include <cstring>

#include "lxmf_send.h"
#include "rtreticulum/msgpack.h"
#include "rtreticulum/transport.h"

#ifdef ESP_PLATFORM
#include "esp_log.h"
#define CN_LOGI(...) ESP_LOGI("calnet", __VA_ARGS__)
#define CN_LOGW(...) ESP_LOGW("calnet", __VA_ARGS__)
#else
#define CN_LOGI(...) do { fprintf(stderr, __VA_ARGS__); fputc('\n', stderr); } while (0)
#define CN_LOGW(...) do { fprintf(stderr, __VA_ARGS__); fputc('\n', stderr); } while (0)
#endif

using namespace RNS;

namespace {

RNS::Bytes s_last_envelope;

// Try to decode the msgpack envelope [ts, title, content, fields] at the
// given offset and hand content to decode_calendar_reply.  Returns true on a
// fully decoded CalendarBundle.
bool try_parse_at(const Bytes& data, size_t off, uint32_t* seq_out,
                  lma_calendar::CalendarBundle* out) {
    if (off >= data.size()) return false;
    MsgPack::Reader r(data.data() + off, data.size() - off);
    // Array header: fixarray (0x90..0x9F) or array16/32 (0xDC/0xDD).
    const uint8_t p = r.peek();
    if (!((p & 0xF0) == 0x90 || p == 0xDC || p == 0xDD)) return false;
    if (r.read_array_header() < 4) return false;
    // [0] timestamp: float64 (0xCB) or int.
    const uint8_t tp = r.peek();
    if (tp == 0xCB) { r.read_float64(); }
    else { r.read_int(); }
    // [1] title (bin) — don't require a specific value, just consume it.
    Bytes title = r.read_bin();
    (void)title;
    // [2] content (bin) = LMAOEnvelope reply bytes.
    Bytes content = r.read_bin();
    if (content.size() == 0) return false;
    // [3] fields — ignore the rest.
    if (!lma_calendar::decode_calendar_reply(content.data(), content.size(), seq_out, out))
        return false;
    s_last_envelope = content;
    return true;
}

}  // namespace

namespace calendar_net {

RNS::Bytes last_reply_envelope() { return s_last_envelope; }

RNS::Destination init(const RNS::Identity& identity, ReplyCallback cb) {
    Destination delivery(identity, Type::Destination::IN, Type::Destination::SINGLE,
                         "lxmf", "delivery");
    delivery.set_packet_callback([cb](const Bytes& data, const Packet&) {
        uint32_t seq = 0;
        lma_calendar::CalendarBundle bundle;
        if (parse_lxm_reply(data, &seq, &bundle)) {
            CN_LOGI("[calnet] reply seq=%u events=%u watermark=%llu",
                    (unsigned)seq, (unsigned)bundle.events.size(),
                    (unsigned long long)bundle.watermark_ms);
            if (cb) cb(seq, bundle);
        } else {
            CN_LOGW("[calnet] unparseable LXM reply (%u bytes)", (unsigned)data.size());
        }
    });
    Transport::register_destination(delivery);
    CN_LOGI("[calnet] IN lxmf/delivery destination registered");
    return delivery;
}

bool send_request(const RNS::Identity& my_identity, const RNS::Identity& server_identity,
                  uint32_t seq, uint64_t since_ms, const char* calendar_id, uint64_t unix_s) {
    std::string envelope = lma_calendar::encode_calendar_request_envelope(
        seq, calendar_id ? calendar_id : "family", since_ms, /*max_events=*/0, /*request_ack=*/true);
    Destination my_delivery(my_identity, Type::Destination::OUT, Type::Destination::SINGLE,
                            "lxmf", "delivery");
    Destination server_delivery(server_identity, Type::Destination::OUT, Type::Destination::SINGLE,
                                "lxmf", "delivery");
    Bytes body = lxmf_send::build_body(my_delivery, server_delivery, my_identity,
                                       Bytes(envelope), Bytes("p:Envelope"), unix_s);
    Bytes frame = lxmf_send::opportunistic_frame(server_delivery, body);
    if (frame.empty()) { CN_LOGW("[calnet] no opportunistic frame"); return false; }
    CN_LOGI("[calnet] TX CalendarRequest seq=%u since=%llu frame=%uB",
            (unsigned)seq, (unsigned long long)since_ms, (unsigned)frame.size());
    Transport::broadcast(frame, nullptr);
    return true;
}

bool parse_lxm_reply(const RNS::Bytes& data, uint32_t* seq_out, lma_calendar::CalendarBundle* out) {
    if (seq_out) *seq_out = 0;
    if (!out) return false;
    // Canonical: src16 + sig64 + msgpack  => msgpack at offset 80.
    constexpr size_t kOffNoDest = 16 + 64;
    // Tolerated: dest16 + src16 + sig64 + msgpack => msgpack at offset 96.
    constexpr size_t kOffWithDest = 16 + 16 + 64;
    if (try_parse_at(data, kOffNoDest, seq_out, out)) return true;
    if (try_parse_at(data, kOffWithDest, seq_out, out)) return true;
    return false;
}

}  // namespace calendar_net
