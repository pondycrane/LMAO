/*
 * lma_proto.h -- Tiny hand-rolled protobuf codec for the LMAO family-calendar
 * wire messages exchanged over the framed-TCP service (lma_core/calendar_tcp_api.py).
 * It implements ONLY the CalendarRequest / CalEvent / CalendarBundle fields
 * pinned in proto/lma_messages.proto:
 *
 *   CalendarRequest { string calendar_id = 1;  uint64 since_ms    = 2;
 *                     uint32 max_events = 3;  string participant = 4; }
 *                     (participant empty == no filter / family mode)
 *   CalEvent        { string uid = 1;  string title = 2;  string notes = 3;
 *                     uint64 start_ms = 4;  uint64 end_ms = 5;
 *                     bool deleted = 6;  string pic = 7; }
 *   CalendarBundle  { uint32 format = 1;  string calendar_id = 2;
 *                     repeated CalEvent events = 3;  uint64 watermark_ms = 4; }
 *
 * Deliberately dependency-free (no malloc, no strings heap, no lwip): strings
 * land in fixed-size buffers, so this file builds and self-tests on a bare
 * host `cc -std=c11 -Wall -Wextra` as well as inside the ESP-IDF main
 * component.  UTF-8 bytes are copied verbatim and NUL-terminated; oversized
 * fields are truncated (never overflow).
 */
#ifndef LMA_PROTO_H
#define LMA_PROTO_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Field-number tags -- keep in sync with proto/lma_messages.proto. */
#define LMA_PB_CALREQ_ID          1u /* CalendarRequest.calendar_id  string        */
#define LMA_PB_CALREQ_SINCE       2u /* CalendarRequest.since_ms    varint uint64 */
#define LMA_PB_CALREQ_MAX         3u /* CalendarRequest.max_events  varint uint32 */
#define LMA_PB_CALREQ_PARTICIPANT 4u /* CalendarRequest.participant string; ""=family */

#define LMA_PB_BUNDLE_FORMAT 1u /* CalendarBundle.format      varint uint32   */
#define LMA_PB_BUNDLE_CALID  2u /* CalendarBundle.calendar_id string          */
#define LMA_PB_BUNDLE_EVENT  3u /* CalendarBundle.events      repeated CalEvent */
#define LMA_PB_BUNDLE_WM     4u /* CalendarBundle.watermark_ms varint uint64  */

#define LMA_PB_EV_UID     1u /* CalEvent.uid      string          */
#define LMA_PB_EV_TITLE   2u /* CalEvent.title    string          */
#define LMA_PB_EV_NOTES   3u /* CalEvent.notes    string          */
#define LMA_PB_EV_START   4u /* CalEvent.start_ms varint uint64   */
#define LMA_PB_EV_END     5u /* CalEvent.end_ms   varint uint64   */
#define LMA_PB_EV_DELETED 6u /* CalEvent.deleted  varint bool     */
#define LMA_PB_EV_PIC     7u /* CalEvent.pic      string          */
#define LMA_PB_EV_PICDATA 8u /* CalEvent.pic_data bytes (1bpp tile) */

/* RLCD image contract (see lma_core/calendar_pics.py): 144x144 1-bit (the
 * whole line drawing), row-major, MSB first, bit 1 = ink.  2592 bytes.  The
 * server attaches pic_data only to the single headline event per reply, so
 * one image at a time is on the wire despite the per-event buffer. */
#define LMA_PB_PIC_TILE       144u
#define LMA_PB_PIC_TILE_BYTES (LMA_PB_PIC_TILE * LMA_PB_PIC_TILE / 8u)

/* Fixed-buffer sizes (character byte caps, NUL not counted in these macros). */
#define LMA_PB_CAL_ID_MAX    31u
#define LMA_PB_PARTICIPANT_MAX 31u  /* kid name; empty == family (no filter) */
/* 63 was NOT enough: Google Calendar recurring-instance uids are
 * <base>_YYYYMMDDTHHMMSSZ and real bases reach ~57 chars (74 total), so two
 * instances truncated to the SAME 63-char prefix and cache_apply merged them
 * — the later instance overwrote the earlier one, silently dropping the
 * headline event (and its served pic_data) from the panel.  127 covers the
 * observed maximum with headroom. */
#define LMA_PB_EVENT_UID_MAX  127u
#define LMA_PB_EVENT_TITLE_MAX 127u
#define LMA_PB_EVENT_NOTES_MAX 255u
#define LMA_PB_EVENT_PIC_MAX   31u

/* Maximum number of events one bundle may carry (cache/NVS arrays sized by it). */
#define LMA_PB_EVENT_MAX 16u

/*
 * Largest payload frame this build accepts from the server.  The server caps
 * frames at 1 MiB; a family delta is small, but each event may carry a 512 B
 * 64x64 pic tile, so the receive buffer must hold LMA_PB_EVENT_MAX tiles
 * (16 x 512 = 8 KiB) plus text.  16 KiB is generous on the PSRAM S3 and keeps
 * the buffer static.
 */
#define LMA_PB_MAX_FRAME 16384u

typedef struct {
    char calendar_id[LMA_PB_CAL_ID_MAX + 1];
    uint64_t since_ms;    /* 0 = full fetch from server */
    uint32_t max_events;  /* 0 = server default page cap */
    /* kid participant -- empty string == no filter (family mode).  Encoded on
     * the wire only when non-empty, so family/default frames stay byte-same. */
    char participant[LMA_PB_PARTICIPANT_MAX + 1];
} lma_cal_request_t;

typedef struct {
    char uid[LMA_PB_EVENT_UID_MAX + 1];
    char title[LMA_PB_EVENT_TITLE_MAX + 1];
    char notes[LMA_PB_EVENT_NOTES_MAX + 1];
    uint64_t start_ms;    /* epoch ms UTC */
    uint64_t end_ms;      /* epoch ms UTC */
    bool deleted;         /* tombstone: client must drop this uid */
    char pic[LMA_PB_EVENT_PIC_MAX + 1]; /* icon-pack key, "" = none */
    uint8_t pic_data[LMA_PB_PIC_TILE_BYTES]; /* 1bpp 144x144 image (XBM) */
    size_t pic_data_len;  /* 0 = no image served */
} lma_cal_event_t;

typedef struct {
    uint32_t format;
    char calendar_id[LMA_PB_CAL_ID_MAX + 1];
    lma_cal_event_t events[LMA_PB_EVENT_MAX];
    size_t event_count;
    uint64_t watermark_ms; /* server pre-read watermark; use as next since_ms */
} lma_cal_bundle_t;

/* Build an all-default request: "" calendar_id, since 0, max 0, i.e. the
 * documented "defaults" frame the server maps to {calendar_id:"family"}.
 * An all-default request encodes to a 0-length payload (empty frame). */
void lma_proto_req_init(lma_cal_request_t *req);

/* Serialize a request; returns bytes written (0 + empty frame == defaults),
 * or -1 if it does not fit in cap bytes. */
int lma_proto_req_encode(const lma_cal_request_t *req, uint8_t *buf, size_t cap);

/* Round-trip / host-test decoder for the request; returns 1 on success. */
int lma_proto_req_decode(const uint8_t *data, size_t len, lma_cal_request_t *out);

/* Zero a bundle (all strings "", no events, format/watermark 0). */
void lma_proto_bundle_init(lma_cal_bundle_t *out);

/* Decode a CalendarBundle payload; returns 1 on success, 0 on malformed or
 * truncated input.  Unknown fields (including wire types fixed32/fixed64) are
 * skipped; groups (deprecated) are rejected. */
int lma_proto_bundle_decode(const uint8_t *data, size_t len, lma_cal_bundle_t *out);

#ifdef __cplusplus
}
#endif

#endif /* LMA_PROTO_H */
