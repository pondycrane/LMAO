#pragma once

#include <stddef.h>
#include <stdint.h>
#include <string>
#include <vector>

// Wire-compatible Family-calendar codec (CalendarRequest / CalEvent /
// CalendarBundle), matching cardputer_client/proto/lma_encoder.py so the
// server's LXMF handler accepts our requests and our client parses its
// replies byte-exactly.
namespace lma_calendar {

    struct CalEvent {
        std::string uid, title, notes, pic;
        uint64_t start_ms = 0, end_ms = 0;
        bool deleted = false;
    };

    struct CalendarBundle {
        uint32_t format = 0;
        std::string calendar_id;
        std::vector<CalEvent> events;
        uint64_t watermark_ms = 0;
    };

    // CalendarRequest{ calendar_id=1, since_ms=2, max_events=3 } — empty/zero
    // fields are omitted (proto3 defaults), like the Python encoder.
    std::string encode_calendar_request(const std::string& calendar_id, uint64_t since_ms, uint32_t max_events);

    // LMAOEnvelope{ seq=1, request_ack=2, request=3{ calendar=3 } }
    std::string encode_calendar_request_envelope(uint32_t seq, const std::string& calendar_id, uint64_t since_ms, uint32_t max_events, bool request_ack);

    // CalendarBundle{ format=1, calendar_id=2, events=3(repeated CalEvent),
    //                 watermark_ms=4 }; CalEvent{ uid=1, title=2, notes=3,
    //                 start_ms=4, end_ms=5, deleted=6, pic=7 }.
    // Unknown fields are skipped; returns false on truncation.
    bool decode_calendar_bundle(const uint8_t* data, size_t len, CalendarBundle* out);

    // Reply LMAOEnvelope: seq (field 1 varint) + calendar payload (field 24
    // LD CalendarBundle). Returns true only if a calendar payload was found
    // and decoded. Unknown fields skipped.
    bool decode_calendar_reply(const uint8_t* data, size_t len, uint32_t* seq_out, CalendarBundle* out);

}
