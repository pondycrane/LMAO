/*
 * rd_net.h -- WiFi + the framed-TCP calendar transport.
 *
 * Transport decision (architectural review P1): the board speaks the plain
 * framed-TCP calendar service on :4247, NOT gRPC.  A hand-rolled HTTP/2 client
 * (connection preface, SETTINGS/ACK, literal HPACK, DATA accumulation) bought
 * nothing for a single unary RPC and could not decode response HEADERS, so a
 * non-zero grpc-status was indistinguishable from an empty reply, and any
 * proxy or server-side HTTP/2 change would break it.  The framed-TCP service
 * is a 4-byte length prefix over the same protobuf messages, and the server
 * now assembles that reply with the SAME shared helper as gRPC/LXMF
 * (lma_core.calendar_bundle.build_bundle), so the two transports cannot drift.
 *
 * Wire:
 *   client -> server  [u32 BE len][CalendarRequest]
 *   server -> client  [u32 BE len][CalendarBundle]
 *   (len == 0 request == the documented defaults: "family", since 0)
 */
#ifndef RD_NET_H
#define RD_NET_H

#include <stdbool.h>
#include <stdint.h>

#include "lma_proto.h"
#include "rd_store.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Initialise the WiFi driver + event handlers (idempotent). */
bool rd_net_init(void);

/* Join the configured AP.  False on timeout (the caller renders the offline
 * state and retries next cycle). */
bool rd_net_connect(const rd_config_t *cfg);

/* Leave the AP (the hold loop runs with WiFi off: the panel is the product,
 * the radio is not). */
void rd_net_disconnect(void);

/* One calendar request/reply.  Fills `bundle` on success.  Uses separate
 * connect and read budgets (RD_TCP_CONNECT_TIMEOUT_MS / RD_TCP_READ_TIMEOUT_MS)
 * and a TOTAL read deadline, so a slow-drip server cannot stall a whole cycle
 * one byte at a time. */
bool rd_net_sync(const rd_config_t *cfg, const lma_cal_request_t *req,
                 lma_cal_bundle_t *bundle);

#ifdef __cplusplus
}
#endif

#endif /* RD_NET_H */
