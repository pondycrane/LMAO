#pragma once

/* Downstream decode of an inbound LMAOEnvelope — the receive-side mirror of
 * lma_encoder: pulls the LMAOEnvelope → TextMessage → content bytes out of a
 * serialized envelope so a client can display the server's reply
 * ("ACK ...\nDATA ..." chart line).
 *
 * Wire format (proto/lma_messages.proto):
 *   LMAOEnvelope { oneof payload { TextMessage text = 20; ... } }
 *   TextMessage  { string content = 1; ... }   [content is field 2]
 *
 * Hand-rolled like lma_encoder (varint / length-delimited walking) — the
 * firmware has no protobuf runtime.  Shared DRY copy for both firmware trees.
 */

#include <string>

namespace lma_decode {

    /* Extract the TextMessage content string (field 20 → field 2) from a
     * serialized LMAOEnvelope.  Returns "" when the envelope is not a text
     * message or is malformed.  Never throws. */
    std::string text_content(const std::string& envelope_bytes);

}
