#pragma once
#include <functional>
#include "lma_calendar.h"
#include "rtreticulum/identity.h"
#include "rtreticulum/destination.h"

namespace calendar_net {
    using ReplyCallback = std::function<void(uint32_t seq, const lma_calendar::CalendarBundle&)>;

    // Creates + registers our IN lxmf/delivery destination and wires its
    // packet callback to parse_lxm_reply -> cb.  Returns the destination so
    // the caller can announce it.
    RNS::Destination init(const RNS::Identity& identity, ReplyCallback cb);

    // Send a CalendarRequest (LMAOEnvelope, request_ack=true) as an
    // opportunistic LXMF message to the server's lxmf/delivery destination.
    bool send_request(const RNS::Identity& my_identity, const RNS::Identity& server_identity,
                      uint32_t seq, uint64_t since_ms, const char* calendar_id, uint64_t unix_s);

    // Raw LMAOEnvelope bytes of the most recent successfully parsed reply
    // (for the NVS cache).  Empty until the first reply.
    RNS::Bytes last_reply_envelope();

    // Parse an LXM reply body (src16 + sig64 + msgpack([ts, title, content,
    // fields])); content is the LMAOEnvelope reply.  Tolerates a leading
    // 16-byte dest hash.  Exposed for host tests.  Never throws, never OOB.
    bool parse_lxm_reply(const RNS::Bytes& data, uint32_t* seq_out, lma_calendar::CalendarBundle* out);
}
