/*
 * lma_proto_selftest.c -- host-only self-test + tiny demo main for the wire
 * layer.  NOT part of the ESP-IDF build (it defines its own main()).
 *
 * Build & run (from robodog_client/):
 *   cc -std=c11 -Wall -Wextra main/lma_proto_selftest.c main/lma_proto.c \
 *      -o /tmp/lma_selftest && /tmp/lma_selftest
 */
#include <assert.h>
#include <stdio.h>
#include <string.h>

#include "lma_proto.h"

/* ------------------------- tiny hand-serializer for the fixture ------------------------ */

typedef struct {
    /* Large enough for a whole reply: 16 events at their text caps plus the
     * headline 176x176 tile (3872 B) -- the fixture must hold what the wire
     * holds, or a tile fixture silently overruns it. */
    uint8_t b[16384];
    size_t n;
} wbuf;

static void wvar(wbuf *w, uint64_t v)
{
    do {
        uint8_t b = (uint8_t)(v & 0x7fu);
        v >>= 7;
        if (v) b |= 0x80u;
        w->b[w->n++] = b;
    } while (v);
}

/* protobuf sint32 zigzag: (n << 1) ^ (n >> 31), computed without shifting a
 * negative int (UB). */
static void wvar_zigzag32(wbuf *w, int32_t v)
{
    const uint32_t u = (uint32_t)v;
    wvar(w, (u << 1) ^ ((u & 0x80000000u) ? 0xFFFFFFFFu : 0u));
}

static void wfield_var(wbuf *w, uint32_t fld, uint64_t v)
{
    wvar(w, ((uint64_t)fld << 3) | 0u);
    wvar(w, v);
}

static void wfield_ld(wbuf *w, uint32_t fld, const char *s)
{
    size_t slen = strlen(s);
    wvar(w, ((uint64_t)fld << 3) | 2u);
    wvar(w, slen);
    memcpy(w->b + w->n, s, slen);
    w->n += slen;
}

static void wfield_bytes(wbuf *w, uint32_t fld, const uint8_t *data, size_t n)
{
    wvar(w, ((uint64_t)fld << 3) | 2u);
    wvar(w, n);
    memcpy(w->b + w->n, data, n);
    w->n += n;
}

static void emit_unknown_fixed32(wbuf *w, uint32_t fld)
{
    wvar(w, ((uint64_t)fld << 3) | 5u);
    for (int i = 0; i < 4; i++) {
        w->b[w->n++] = (uint8_t)('A' + i);
    }
}

/* Append field `fld` = length-delimited submessage `sub`. */
static void wfield_msg(wbuf *w, uint32_t fld, const wbuf *sub)
{
    wvar(w, ((uint64_t)fld << 3) | 2u);
    wvar(w, sub->n);
    memcpy(w->b + w->n, sub->b, sub->n);
    w->n += sub->n;
}

#define CHECK(cond)                                                        \
    do {                                                                   \
        if (!(cond)) {                                                     \
            fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
            return 1;                                                      \
        }                                                                  \
        passed++;                                                          \
    } while (0)

static int test_req_roundtrip(void)
{
    int passed = 0;
    uint8_t buf[64];

    /* Non-default request: calendar_id + since_ms + max_events + participant. */
    lma_cal_request_t req;
    lma_proto_req_init(&req);
    snprintf(req.calendar_id, sizeof(req.calendar_id), "family");
    req.since_ms = 1234567890123ULL;
    req.max_events = 5;
    snprintf(req.participant, sizeof(req.participant), "juliet");

    int n = lma_proto_req_encode(&req, buf, sizeof(buf));
    CHECK(n > 0);

    lma_cal_request_t out;
    CHECK(lma_proto_req_decode(buf, (size_t)n, &out) == 1);
    CHECK(strcmp(out.calendar_id, "family") == 0);
    CHECK(out.since_ms == 1234567890123ULL);
    CHECK(out.max_events == 5);
    CHECK(strcmp(out.participant, "juliet") == 0);

    /* All-default request -> empty payload (documented "defaults" frame). */
    lma_cal_request_t def;
    lma_proto_req_init(&def);
    CHECK(lma_proto_req_encode(&def, buf, sizeof(buf)) == 0);
    CHECK(lma_proto_req_decode(buf, 0, &out) == 1);
    CHECK(out.calendar_id[0] == '\0' && out.since_ms == 0 && out.max_events == 0);
    CHECK(out.participant[0] == '\0');

    /* An empty participant adds 0 bytes vs the same request without it. */
    {
        lma_cal_request_t no_kid = req;
        no_kid.participant[0] = '\0';
        CHECK((size_t)lma_proto_req_encode(&req, buf, sizeof(buf)) ==
              (size_t)lma_proto_req_encode(&no_kid, buf, sizeof(buf)) + 8);
        /* 8 bytes = tag(2)+len(1)+"juliet"(6): tag varint 0x22, len 0x06. */
    }

    /* Manual field-4 wire encoding decodes into participant. */
    {
        wbuf w = {{0}, 0};
        wfield_ld(&w, 4u, "juliet");
        CHECK(lma_proto_req_decode(w.b, w.n, &out) == 1);
        CHECK(strcmp(out.participant, "juliet") == 0);
        CHECK(out.calendar_id[0] == '\0' && out.since_ms == 0 && out.max_events == 0);
    }

    /* Max uint64 since_ms round-trips (10-byte varint). */
    lma_cal_request_t big;
    lma_proto_req_init(&big);
    big.since_ms = 0xFFFFFFFFFFFFFFFFULL;
    n = lma_proto_req_encode(&big, buf, sizeof(buf));
    CHECK(n > 0);
    CHECK(lma_proto_req_decode(buf, (size_t)n, &out) == 1);
    CHECK(out.since_ms == 0xFFFFFFFFFFFFFFFFULL);

    /* Encoder returns -1 when it cannot fit. */
    CHECK(lma_proto_req_encode(&req, buf, 2) == -1);

    printf("test_req_roundtrip: %d checks passed\n", passed);
    return 0;
}

static int test_bundle_decode(void)
{
    int passed = 0;
    wbuf ev1 = {{0}, 0}, ev2 = {{0}, 0}, w = {{0}, 0};

    /* event 1: uid/title/notes/start/end/pic, deleted unset (false). */
    wfield_ld(&ev1, LMA_PB_EV_UID, "ev-1");
    wfield_ld(&ev1, LMA_PB_EV_TITLE, "Mom pickup");
    wfield_ld(&ev1, LMA_PB_EV_NOTES, "school gate");
    wfield_var(&ev1, LMA_PB_EV_START, 1700000000000ULL);
    wfield_var(&ev1, LMA_PB_EV_END, 1700003600000ULL);
    wfield_ld(&ev1, LMA_PB_EV_PIC, "school");

    /* event 2: uid/title + deleted=true; no notes, no pic. */
    wfield_ld(&ev2, LMA_PB_EV_UID, "ev-2");
    wfield_ld(&ev2, LMA_PB_EV_TITLE, "Dentist");
    wfield_var(&ev2, LMA_PB_EV_DELETED, 1);

    wfield_var(&w, LMA_PB_BUNDLE_FORMAT, 1);
    wfield_ld(&w, LMA_PB_BUNDLE_CALID, "family");
    wfield_msg(&w, LMA_PB_BUNDLE_EVENT, &ev1);
    wfield_msg(&w, LMA_PB_BUNDLE_EVENT, &ev2);
    wfield_var(&w, LMA_PB_BUNDLE_WM, 1705000000000ULL);

    lma_cal_bundle_t b;
    CHECK(lma_proto_bundle_decode(w.b, w.n, &b) == 1);
    CHECK(b.format == 1);
    CHECK(strcmp(b.calendar_id, "family") == 0);
    CHECK(b.event_count == 2);
    CHECK(b.watermark_ms == 1705000000000ULL);

    CHECK(strcmp(b.events[0].uid, "ev-1") == 0);
    CHECK(strcmp(b.events[0].title, "Mom pickup") == 0);
    CHECK(strcmp(b.events[0].notes, "school gate") == 0);
    CHECK(b.events[0].start_ms == 1700000000000ULL);
    CHECK(b.events[0].end_ms == 1700003600000ULL);
    CHECK(b.events[0].deleted == false);
    CHECK(strcmp(b.events[0].pic, "school") == 0);

    CHECK(strcmp(b.events[1].uid, "ev-2") == 0);
    CHECK(strcmp(b.events[1].title, "Dentist") == 0);
    CHECK(b.events[1].deleted == true);
    CHECK(b.events[1].pic[0] == '\0');
    CHECK(b.events[1].notes[0] == '\0');

    printf("test_bundle_decode: %d checks passed\n", passed);
    return 0;
}

/* Weather submessage: CalendarBundle.current (field 5) + CalEvent.weather
 * (field 9).  This is the wire the server fills from Open-Meteo, so the
 * decode must survive zigzag-negative temperatures and absent slots. */
static int test_weather_decode(void)
{
    int passed = 0;
    wbuf cur = {{0}, 0}, ev1 = {{0}, 0}, ev2 = {{0}, 0}, w = {{0}, 0};

    /* current: -2.5 degC (zigzag 49) and WMO 71 (snow). */
    wvar(&cur, ((uint64_t)LMA_PB_WX_TEMP << 3) | 0u);
    wvar_zigzag32(&cur, -25);
    wfield_var(&cur, LMA_PB_WX_CODE, 71);

    /* event 1 carries a forecast (4.6 degC clear); event 2 carries none. */
    wfield_ld(&ev1, LMA_PB_EV_UID, "ev-wx");
    wfield_var(&ev1, LMA_PB_EV_START, 1700000000000ULL);
    {
        wbuf wx = {{0}, 0};
        wfield_var(&wx, LMA_PB_WX_CODE, 0);
        /* temp_c10 = 46 -> zigzag 92 */
        wfield_var(&wx, LMA_PB_WX_TEMP, 92);
        wfield_msg(&ev1, LMA_PB_EV_WEATHER, &wx);
    }
    wfield_ld(&ev2, LMA_PB_EV_UID, "ev-nowx");

    wfield_var(&w, LMA_PB_BUNDLE_FORMAT, 1);
    wfield_msg(&w, LMA_PB_BUNDLE_EVENT, &ev1);
    wfield_msg(&w, LMA_PB_BUNDLE_EVENT, &ev2);
    wfield_msg(&w, LMA_PB_BUNDLE_CURRENT, &cur);

    lma_cal_bundle_t b;
    CHECK(lma_proto_bundle_decode(w.b, w.n, &b) == 1);
    CHECK(b.event_count == 2);

    CHECK(b.current.present == true);
    CHECK(b.current.temp_c10 == -25);   /* zigzag negative round-trips */
    CHECK(b.current.code == 71);

    CHECK(b.events[0].weather.present == true);
    CHECK(b.events[0].weather.temp_c10 == 46);
    CHECK(b.events[0].weather.code == 0);

    /* An event without a Weather message must read as "no forecast" -- not as
     * a 0 degC/clear reading. */
    CHECK(b.events[1].weather.present == false);
    CHECK(b.events[1].weather.temp_c10 == 0);
    CHECK(b.events[1].weather.code == 0);

    /* A Weather carrying only a code still marks the slot as present. */
    {
        wbuf wx = {{0}, 0}, ev3 = {{0}, 0}, w3 = {{0}, 0};
        wfield_var(&wx, LMA_PB_WX_CODE, 95);
        wfield_ld(&ev3, LMA_PB_EV_UID, "ev-code");
        wfield_msg(&ev3, LMA_PB_EV_WEATHER, &wx);
        wfield_msg(&w3, LMA_PB_BUNDLE_EVENT, &ev3);
        lma_cal_bundle_t b3;
        CHECK(lma_proto_bundle_decode(w3.b, w3.n, &b3) == 1);
        CHECK(b3.events[0].weather.present == true);
        CHECK(b3.events[0].weather.code == 95);
        CHECK(b3.events[0].weather.temp_c10 == 0);
    }

    /* Unknown fields inside Weather are skipped (forward compatibility). */
    {
        wbuf wx = {{0}, 0}, w4 = {{0}, 0};
        wfield_var(&wx, LMA_PB_WX_CODE, 3);
        wfield_var(&wx, 9u, 1234);       /* unknown varint inside Weather */
        emit_unknown_fixed32(&wx, 7u);   /* unknown fixed32 inside Weather */
        wfield_msg(&w4, LMA_PB_BUNDLE_CURRENT, &wx);
        lma_cal_bundle_t b4;
        CHECK(lma_proto_bundle_decode(w4.b, w4.n, &b4) == 1);
        CHECK(b4.current.present == true);
        CHECK(b4.current.code == 3);
    }

    /* Truncated Weather payloads must be rejected, never half-applied. */
    {
        uint8_t bad[4] = {0x2A, 0x03, 0x08, 0x80}; /* current, len 3, truncated */
        lma_cal_bundle_t t;
        CHECK(lma_proto_bundle_decode(bad, sizeof(bad), &t) == 0);
    }
    {
        uint8_t bad2[3] = {0x2A, 0x05, 0x08}; /* current claims 5 bytes, 1 given */
        lma_cal_bundle_t t;
        CHECK(lma_proto_bundle_decode(bad2, sizeof(bad2), &t) == 0);
    }

    /* Every prefix of the weather-bearing bundle decodes without faulting. */
    for (size_t cut = 0; cut <= w.n; cut++) {
        lma_cal_bundle_t t;
        (void)lma_proto_bundle_decode(w.b, cut, &t);
    }

    /* An empty payload leaves both weather slots absent. */
    lma_cal_bundle_t z;
    CHECK(lma_proto_bundle_decode(w.b, 0, &z) == 1);
    CHECK(z.current.present == false);

    printf("test_weather_decode: %d checks passed\n", passed);
    return 0;
}

/* The headline tile (CalEvent.pic_data) is decoded into the BUNDLE, not the
 * event: the server serves exactly one tile per reply, and 16 per-event
 * 3872-byte slots would waste RAM on bytes that can never arrive. */
static int test_pic_tile_decode(void)
{
    int passed = 0;
    wbuf ev1 = {{0}, 0}, ev2 = {{0}, 0}, w = {{0}, 0};
    uint8_t tile[LMA_PB_PIC_TILE_BYTES];

    for (size_t i = 0; i < sizeof(tile); i++) {
        tile[i] = (uint8_t)(i * 7u + 1u);
    }

    wfield_ld(&ev1, LMA_PB_EV_UID, "hl-uid");
    wfield_ld(&ev1, LMA_PB_EV_TITLE, "Headline");
    wfield_var(&ev1, LMA_PB_EV_START, 1700000000000ULL);
    wfield_bytes(&ev1, LMA_PB_EV_PICDATA, tile, sizeof(tile));
    wfield_ld(&ev2, LMA_PB_EV_UID, "plain-uid");
    wfield_ld(&ev2, LMA_PB_EV_TITLE, "No picture");

    wfield_var(&w, LMA_PB_BUNDLE_FORMAT, 1);
    wfield_msg(&w, LMA_PB_BUNDLE_EVENT, &ev1);
    wfield_msg(&w, LMA_PB_BUNDLE_EVENT, &ev2);

    lma_cal_bundle_t b;
    CHECK(lma_proto_bundle_decode(w.b, w.n, &b) == 1);
    CHECK(b.event_count == 2);
    CHECK(b.pic_data_len == sizeof(tile));
    CHECK(memcmp(b.pic_data, tile, sizeof(tile)) == 0);
    CHECK(strcmp(b.pic_uid, "hl-uid") == 0);

    /* An oversize tile is capped at the contract size (never an overflow),
     * and still binds to its event. */
    {
        uint8_t big[LMA_PB_PIC_TILE_BYTES + 64];
        memset(big, 0xA5, sizeof(big));
        wbuf ev3 = {{0}, 0}, w3 = {{0}, 0};
        wfield_ld(&ev3, LMA_PB_EV_UID, "big-uid");
        wfield_bytes(&ev3, LMA_PB_EV_PICDATA, big, sizeof(big));
        wfield_msg(&w3, LMA_PB_BUNDLE_EVENT, &ev3);
        lma_cal_bundle_t b3;
        CHECK(lma_proto_bundle_decode(w3.b, w3.n, &b3) == 1);
        CHECK(b3.pic_data_len == LMA_PB_PIC_TILE_BYTES);
        CHECK(strcmp(b3.pic_uid, "big-uid") == 0);
    }

    /* No tile served -> no tile, and pic_uid stays empty so a stale drawing
     * can never be paired with a different event. */
    {
        wbuf w4 = {{0}, 0};
        wfield_msg(&w4, LMA_PB_BUNDLE_EVENT, &ev2);
        lma_cal_bundle_t b4;
        CHECK(lma_proto_bundle_decode(w4.b, w4.n, &b4) == 1);
        CHECK(b4.pic_data_len == 0);
        CHECK(b4.pic_uid[0] == '\0');
    }

    /* Every prefix decodes without faulting (tile bytes included). */
    for (size_t cut = 0; cut <= w.n; cut++) {
        lma_cal_bundle_t t;
        (void)lma_proto_bundle_decode(w.b, cut, &t);
    }

    printf("test_pic_tile_decode: %d checks passed\n", passed);
    return 0;
}

/* Encode/decode round-trip through the PRODUCTION encoder.  The fixtures
 * above are hand-serialized (they pin the field numbers independently), but a
 * hand-written serializer cannot prove the production encode matches the
 * decode -- the two could drift together and stay green. */
static int test_encode_roundtrip(void)
{
    int passed = 0;
    lma_cal_bundle_t b;
    lma_proto_bundle_init(&b);
    b.format = 1;
    snprintf(b.calendar_id, sizeof(b.calendar_id), "family");
    b.watermark_ms = 1705000000000ULL;

    /* event 0: the headline, with a served tile and a forecast. */
    snprintf(b.events[0].uid, sizeof(b.events[0].uid), "hl-uid");
    snprintf(b.events[0].title, sizeof(b.events[0].title), "Swim class");
    snprintf(b.events[0].notes, sizeof(b.events[0].notes), "bring towel");
    b.events[0].start_ms = 1700000000000ULL;
    b.events[0].end_ms = 1700003600000ULL;
    snprintf(b.events[0].pic, sizeof(b.events[0].pic), "swim");
    b.events[0].weather.present = true;
    b.events[0].weather.temp_c10 = -25;      /* zigzag negative */
    b.events[0].weather.code = 71;

    /* event 1: a tombstone, no weather, no pic. */
    snprintf(b.events[1].uid, sizeof(b.events[1].uid), "gone-uid");
    snprintf(b.events[1].title, sizeof(b.events[1].title), "Dentist");
    b.events[1].start_ms = 1700000000000ULL;
    b.events[1].deleted = true;
    b.event_count = 2;

    for (size_t i = 0; i < LMA_PB_PIC_TILE_BYTES; i++) {
        b.pic_data[i] = (uint8_t)(i * 3u + 7u);
    }
    b.pic_data_len = LMA_PB_PIC_TILE_BYTES;
    snprintf(b.pic_uid, sizeof(b.pic_uid), "hl-uid");

    b.current.present = true;
    b.current.temp_c10 = 46;
    b.current.code = 0;

    static uint8_t wire[LMA_PB_MAX_FRAME];
    const int n = lma_proto_bundle_encode(&b, wire, sizeof(wire));
    CHECK(n > 0);
    CHECK((size_t)n < sizeof(wire));

    lma_cal_bundle_t out;
    CHECK(lma_proto_bundle_decode(wire, (size_t)n, &out) == 1);
    CHECK(out.format == 1);
    CHECK(strcmp(out.calendar_id, "family") == 0);
    CHECK(out.watermark_ms == b.watermark_ms);
    CHECK(out.event_count == 2);

    CHECK(strcmp(out.events[0].uid, "hl-uid") == 0);
    CHECK(strcmp(out.events[0].title, "Swim class") == 0);
    CHECK(strcmp(out.events[0].notes, "bring towel") == 0);
    CHECK(out.events[0].start_ms == 1700000000000ULL);
    CHECK(out.events[0].end_ms == 1700003600000ULL);
    CHECK(out.events[0].deleted == false);
    CHECK(strcmp(out.events[0].pic, "swim") == 0);
    CHECK(out.events[0].weather.present == true);
    CHECK(out.events[0].weather.temp_c10 == -25);
    CHECK(out.events[0].weather.code == 71);

    CHECK(out.events[1].deleted == true);
    CHECK(out.events[1].weather.present == false);

    /* The tile lands at bundle level, bound to the headline uid. */
    CHECK(out.pic_data_len == LMA_PB_PIC_TILE_BYTES);
    CHECK(memcmp(out.pic_data, b.pic_data, LMA_PB_PIC_TILE_BYTES) == 0);
    CHECK(strcmp(out.pic_uid, "hl-uid") == 0);

    CHECK(out.current.present == true);
    CHECK(out.current.temp_c10 == 46);
    CHECK(out.current.code == 0);

    /* Encoding is deterministic: same input -> same bytes. */
    static uint8_t wire2[LMA_PB_MAX_FRAME];
    const int n2 = lma_proto_bundle_encode(&b, wire2, sizeof(wire2));
    CHECK(n2 == n);
    CHECK(memcmp(wire, wire2, (size_t)n) == 0);

    /* A too-small buffer fails closed (never a truncated message). */
    CHECK(lma_proto_bundle_encode(&b, wire, 32) == -1);

    /* Empty bundle encodes to just the format + calendar + watermark. */
    lma_cal_bundle_t e;
    lma_proto_bundle_init(&e);
    const int en = lma_proto_bundle_encode(&e, wire, sizeof(wire));
    CHECK(en > 0);
    lma_cal_bundle_t eout;
    CHECK(lma_proto_bundle_decode(wire, (size_t)en, &eout) == 1);
    CHECK(eout.event_count == 0);
    CHECK(eout.pic_data_len == 0);

    printf("test_encode_roundtrip: %d checks passed\n", passed);
    return 0;
}

static int test_robustness(void)
{
    int passed = 0;
    wbuf ev = {{0}, 0}, w = {{0}, 0};

    /* Unknown fields are skipped: a bogus varint field 9 at bundle level, and
     * a bogus varint + fixed32 field inside an event, must not break decode. */
    wfield_var(&w, LMA_PB_BUNDLE_FORMAT, 1);
    wfield_var(&w, 9u, 1234); /* unknown bundle-level varint */

    wfield_ld(&ev, LMA_PB_EV_UID, "ev-1");
    wfield_var(&ev, 8u, 77);           /* unknown event-level varint */
    emit_unknown_fixed32(&ev, 8u);     /* unknown event-level fixed32 */
    wfield_var(&ev, LMA_PB_EV_START, 42);
    wfield_msg(&w, LMA_PB_BUNDLE_EVENT, &ev);
    wfield_var(&w, LMA_PB_BUNDLE_WM, 9);

    lma_cal_bundle_t b;
    CHECK(lma_proto_bundle_decode(w.b, w.n, &b) == 1);
    CHECK(b.format == 1);
    CHECK(b.event_count == 1);
    CHECK(strcmp(b.events[0].uid, "ev-1") == 0);
    CHECK(b.events[0].start_ms == 42);
    CHECK(b.watermark_ms == 9);

    /*
     * Memory-safety sweep: decode every prefix of the buffer.  Some prefixes
     * are legitimately complete messages (e.g. up to the first event), so we
     * only require that decoding never faults, and separately pin down
     * hand-known truncation points that MUST fail.
     */
    for (size_t cut = 0; cut <= w.n; cut++) {
        lma_cal_bundle_t t;
        (void)lma_proto_bundle_decode(w.b, cut, &t);
    }

    /* Cut right after the leading tag but before its value: malformed. */
    {
        lma_cal_bundle_t t;
        CHECK(lma_proto_bundle_decode(w.b, 1, &t) == 0);
    }

    /* Civilized prefix <- up to and including the complete format field. */
    {
        lma_cal_bundle_t t;
        CHECK(lma_proto_bundle_decode(w.b, 2, &t) == 1);
        CHECK(t.format == 1 && t.event_count == 0);
    }

    /* Empty payload decodes to an all-empty bundle. */
    lma_cal_bundle_t z;
    CHECK(lma_proto_bundle_decode(w.b, 0, &z) == 1);
    CHECK(z.event_count == 0 && z.format == 0 && z.watermark_ms == 0);
    CHECK(z.calendar_id[0] == '\0');

    /* A varint longer than 10 bytes/reaching bit 64 is rejected. */
    {
        uint8_t over[16];
        memset(over, 0xFF, sizeof(over)); /* endless continuation */
        over[15] = 0x7F;                  /* terminates but with high bits set */
        lma_cal_bundle_t t;
        CHECK(lma_proto_bundle_decode(over, sizeof(over), &t) == 0);
    }

    /* Length prefix claiming far more bytes than present is rejected. */
    {
        uint8_t hi[6] = {0x1A, 0xFF, 0xFF, 0xFF, 0xFF, 0x7F};
        lma_cal_bundle_t t;
        CHECK(lma_proto_bundle_decode(hi, sizeof(hi), &t) == 0);
    }

    printf("test_robustness: %d checks passed\n", passed);
    return 0;
}

int main(void)
{
    printf("lma_proto self-test\n");
    if (test_req_roundtrip() != 0 ||
        test_bundle_decode() != 0 ||
        test_weather_decode() != 0 ||
        test_pic_tile_decode() != 0 ||
        test_encode_roundtrip() != 0 ||
        test_robustness() != 0) {
        printf("RESULT: FAIL\n");
        return 1;
    }
    printf("RESULT: PASS\n");
    return 0;
}
