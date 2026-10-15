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
    uint8_t b[1024];
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

    /* Non-default request: calendar_id + since_ms + max_events. */
    lma_cal_request_t req;
    lma_proto_req_init(&req);
    snprintf(req.calendar_id, sizeof(req.calendar_id), "family");
    req.since_ms = 1234567890123ULL;
    req.max_events = 5;

    int n = lma_proto_req_encode(&req, buf, sizeof(buf));
    CHECK(n > 0);

    lma_cal_request_t out;
    CHECK(lma_proto_req_decode(buf, (size_t)n, &out) == 1);
    CHECK(strcmp(out.calendar_id, "family") == 0);
    CHECK(out.since_ms == 1234567890123ULL);
    CHECK(out.max_events == 5);

    /* All-default request -> empty payload (documented "defaults" frame). */
    lma_cal_request_t def;
    lma_proto_req_init(&def);
    CHECK(lma_proto_req_encode(&def, buf, sizeof(buf)) == 0);
    CHECK(lma_proto_req_decode(buf, 0, &out) == 1);
    CHECK(out.calendar_id[0] == '\0' && out.since_ms == 0 && out.max_events == 0);

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
        test_robustness() != 0) {
        printf("RESULT: FAIL\n");
        return 1;
    }
    printf("RESULT: PASS\n");
    return 0;
}
