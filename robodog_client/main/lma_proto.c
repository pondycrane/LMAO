/*
 * lma_proto.c -- hand-rolled protobuf codec, see lma_proto.h for the field
 * contract.  stdint/stddef/stdbool + fixed buffers only (no malloc, no lwip),
 * so it is host-compilable for the self-test.
 *
 * Every decode path reads through the same two primitives -- read_var() for a
 * varint and read_ld() for a length-delimited payload -- so a field case is a
 * wire-type check plus one call, and there is no per-case copy of the
 * bounds-checking boilerplate (the copy-paste class that let `event_decode`
 * bound against `end` while the bundle level bounded against `len`).  Each
 * submessage is decoded over its OWN slice [0, len), so a truncated nested
 * message can never read past its parent's declared length.
 */
#include "lma_proto.h"

#include <string.h> /* memcpy/memset */

/* ------------------------------------------------------------------ varint */

/* Encode v in LEB128.  Returns bytes written (1..10). */
static size_t uv_encode(uint64_t v, uint8_t *out)
{
    size_t n = 0;
    do {
        uint8_t b = (uint8_t)(v & 0x7fu);
        v >>= 7;
        if (v != 0) {
            b |= 0x80u;
        }
        out[n++] = b;
    } while (v != 0);
    return n;
}

/*
 * Decode a varint at *pos within data[0..len).  On success stores the value in
 * *val, advances *pos and returns 0.  Returns 1 on truncation, 2 if the varint
 * overflows 64 bits (more than 10 bytes / bits beyond bit 63 set).
 */
static int uv_decode(const uint8_t *data, size_t len, size_t *pos, uint64_t *val)
{
    uint64_t v = 0;
    unsigned shift = 0;
    size_t i = *pos;

    while (i < len) {
        uint8_t b = data[i++];
        if (shift >= 64) {
            /* Bits 64+ must be zero in a well-formed varint. */
            if ((b & 0x7fu) != 0) {
                return 2;
            }
            if ((b & 0x80u) == 0) {
                *val = v;
                *pos = i;
                return 0;
            }
            continue; /* continuation byte of zero-only overflow */
        }
        v |= (uint64_t)(b & 0x7fu) << shift;
        if ((b & 0x80u) == 0) {
            *val = v;
            *pos = i;
            return 0;
        }
        shift += 7;
    }
    return 1; /* ran out of bytes */
}

/* ------------------------------------------------------------------ fields */

/* Read one varint value at *pos; 1 on success, 0 on truncation/overflow. */
static int read_var(const uint8_t *data, size_t len, size_t *pos, uint64_t *val)
{
    return uv_decode(data, len, pos, val) == 0;
}

/* Read one length-delimited payload at *pos: on success *out points at the
 * payload bytes and *outlen is its length, with *pos advanced past it.  A
 * length that runs past `len` fails (never a partial payload). */
static int read_ld(const uint8_t *data, size_t len, size_t *pos,
                   const uint8_t **out, size_t *outlen)
{
    uint64_t v;
    if (!read_var(data, len, pos, &v)) {
        return 0;
    }
    if (v > len - *pos) {
        return 0;
    }
    *out = data + *pos;
    *outlen = (size_t)v;
    *pos += (size_t)v;
    return 1;
}

/* Encode a varint field: tag then value.  0 on no room. */
static int write_varint_field(uint8_t *buf, size_t cap, size_t *pos,
                              uint32_t fld, uint64_t val)
{
    uint8_t key[10];
    uint8_t valb[10];
    size_t kn = uv_encode(((uint64_t)fld << 3) | 0u, key);
    size_t vn = uv_encode(val, valb);
    if (*pos + kn + vn > cap) {
        return 0;
    }
    memcpy(buf + *pos, key, kn);
    *pos += kn;
    memcpy(buf + *pos, valb, vn);
    *pos += vn;
    return 1;
}

/* Encode a length-delimited (string) field.  0 on no room. */
static int write_ld_field(uint8_t *buf, size_t cap, size_t *pos,
                          uint32_t fld, const char *s)
{
    size_t slen = strlen(s);
    uint8_t key[10];
    uint8_t lenb[10];
    size_t kn = uv_encode(((uint64_t)fld << 3) | 2u, key);
    size_t ln = uv_encode(slen, lenb);
    if (*pos + kn + ln + slen > cap) {
        return 0;
    }
    memcpy(buf + *pos, key, kn);
    *pos += kn;
    memcpy(buf + *pos, lenb, ln);
    *pos += ln;
    memcpy(buf + *pos, s, slen);
    *pos += slen;
    return 1;
}

/*
 * Skip one unknown field of wire type wt starting at *pos (bounds = len).
 * Groups (3/4) are rejected.  Returns 0 on success.
 */
static int skip_field(const uint8_t *data, size_t len, size_t *pos, unsigned wt)
{
    const uint8_t *p;
    size_t n;
    uint64_t v;
    switch (wt) {
    case 0: /* varint */
        return read_var(data, len, pos, &v) ? 0 : 1;
    case 1: /* fixed64 */
        if (*pos + 8 > len) {
            return 1;
        }
        *pos += 8;
        return 0;
    case 2: /* length-delimited */
        return read_ld(data, len, pos, &p, &n) ? 0 : 1;
    case 5: /* fixed32 */
        if (*pos + 4 > len) {
            return 1;
        }
        *pos += 4;
        return 0;
    default: /* 3, 4 = deprecated groups */
        return 1;
    }
}

/*
 * Copy a length-delimited string into dst[0..cap): the bytes are consumed
 * whole even when truncated to fit, and dst is always NUL-terminated.  UTF-8
 * is copied verbatim; truncation can split a sequence, which the renderer
 * tolerates (it never splits a glyph itself).
 */
static void copy_str(const uint8_t *src, size_t n, char *dst, size_t cap)
{
    size_t copy = (n < cap - 1) ? n : (cap - 1);
    memcpy(dst, src, copy);
    dst[copy] = '\0';
}

/* ------------------------------------------------------------ request side */

void lma_proto_req_init(lma_cal_request_t *req)
{
    memset(req, 0, sizeof(*req));
}

int lma_proto_req_encode(const lma_cal_request_t *req, uint8_t *buf, size_t cap)
{
    size_t pos = 0;

    if (req == NULL || buf == NULL) {
        return -1;
    }
    if (req->calendar_id[0] != '\0' &&
        !write_ld_field(buf, cap, &pos, LMA_PB_CALREQ_ID, req->calendar_id)) {
        return -1;
    }
    if (req->since_ms != 0 &&
        !write_varint_field(buf, cap, &pos, LMA_PB_CALREQ_SINCE, req->since_ms)) {
        return -1;
    }
    if (req->max_events != 0 &&
        !write_varint_field(buf, cap, &pos, LMA_PB_CALREQ_MAX, req->max_events)) {
        return -1;
    }
    if (req->participant[0] != '\0' &&
        !write_ld_field(buf, cap, &pos, LMA_PB_CALREQ_PARTICIPANT,
                        req->participant)) {
        return -1;
    }
    return (int)pos;
}

int lma_proto_req_decode(const uint8_t *data, size_t len, lma_cal_request_t *out)
{
    size_t pos = 0;
    uint64_t v;
    const uint8_t *p;
    size_t n;

    if (out == NULL) {
        return 0;
    }
    lma_proto_req_init(out);

    while (pos < len) {
        uint64_t key;
        if (!read_var(data, len, &pos, &key)) {
            return 0;
        }
        uint32_t fld = (uint32_t)(key >> 3);
        unsigned wt = (unsigned)(key & 7u);

        switch (fld) {
        case LMA_PB_CALREQ_ID:
            if (wt != 2 || !read_ld(data, len, &pos, &p, &n)) {
                if (skip_field(data, len, &pos, wt) != 0) {
                    return 0;
                }
                break;
            }
            copy_str(p, n, out->calendar_id, sizeof(out->calendar_id));
            break;
        case LMA_PB_CALREQ_SINCE:
            if (wt != 0 || !read_var(data, len, &pos, &v)) {
                if (skip_field(data, len, &pos, wt) != 0) {
                    return 0;
                }
                break;
            }
            out->since_ms = v;
            break;
        case LMA_PB_CALREQ_MAX:
            if (wt != 0 || !read_var(data, len, &pos, &v)) {
                if (skip_field(data, len, &pos, wt) != 0) {
                    return 0;
                }
                break;
            }
            out->max_events = (uint32_t)v;
            break;
        case LMA_PB_CALREQ_PARTICIPANT:
            if (wt != 2 || !read_ld(data, len, &pos, &p, &n)) {
                if (skip_field(data, len, &pos, wt) != 0) {
                    return 0;
                }
                break;
            }
            copy_str(p, n, out->participant, sizeof(out->participant));
            break;
        default:
            if (skip_field(data, len, &pos, wt) != 0) {
                return 0;
            }
            break;
        }
    }
    return 1;
}

/* ---------------------------------------------------------- bundle side */

/* protobuf sint32/sint64 are zigzag-encoded: n <-> (n << 1) ^ (n >> 31).
 * Decoding the raw varint as a signed int would turn -2.5 degC (zigzag 4)
 * into +4, i.e. a winter panel printing "39F" instead of "27F". */
static int32_t uv_decode_zigzag32(uint64_t v)
{
    uint32_t u = (uint32_t)v;
    return (int32_t)((u >> 1) ^ (~(u & 1u) + 1u));
}

/*
 * Decode a Weather submessage over its own slice [0, len).  Sets
 * out->present on any successfully parsed field, so a Weather that only
 * carries a code (no temperature) still reads as "a forecast exists".
 */
static int weather_decode(const uint8_t *data, size_t len, lma_weather_t *out)
{
    size_t pos = 0;
    uint64_t v;
    bool saw_field = false;

    memset(out, 0, sizeof(*out));
    while (pos < len) {
        uint64_t key;
        if (!read_var(data, len, &pos, &key)) {
            return 0;
        }
        uint32_t fld = (uint32_t)(key >> 3);
        unsigned wt = (unsigned)(key & 7u);

        switch (fld) {
        case LMA_PB_WX_TEMP:
            if (wt != 0 || !read_var(data, len, &pos, &v)) {
                if (skip_field(data, len, &pos, wt) != 0) {
                    return 0;
                }
                break;
            }
            out->temp_c10 = uv_decode_zigzag32(v);
            saw_field = true;
            break;
        case LMA_PB_WX_CODE:
            if (wt != 0 || !read_var(data, len, &pos, &v)) {
                if (skip_field(data, len, &pos, wt) != 0) {
                    return 0;
                }
                break;
            }
            out->code = (uint32_t)v;
            saw_field = true;
            break;
        default:
            if (skip_field(data, len, &pos, wt) != 0) {
                return 0;
            }
            break;
        }
    }
    out->present = saw_field;
    return 1;
}

/*
 * Decode one CalEvent over its own slice [0, len).  The event's pic_data (the
 * headline tile) lands in the BUNDLE (bundle->pic_data / pic_uid), because the
 * server serves exactly one tile per reply.
 */
static int event_decode(const uint8_t *data, size_t len, lma_cal_event_t *ev,
                        lma_cal_bundle_t *bundle)
{
    size_t pos = 0;
    uint64_t v;
    const uint8_t *p;
    size_t n;
    bool saw_pic = false;

    memset(ev, 0, sizeof(*ev));
    while (pos < len) {
        uint64_t key;
        if (!read_var(data, len, &pos, &key)) {
            return 0;
        }
        uint32_t fld = (uint32_t)(key >> 3);
        unsigned wt = (unsigned)(key & 7u);

        switch (fld) {
        case LMA_PB_EV_UID:
        case LMA_PB_EV_TITLE:
        case LMA_PB_EV_NOTES:
        case LMA_PB_EV_PIC:
            if (wt != 2 || !read_ld(data, len, &pos, &p, &n)) {
                if (skip_field(data, len, &pos, wt) != 0) {
                    return 0;
                }
                break;
            }
            {
                char *dst = (fld == LMA_PB_EV_UID)    ? ev->uid :
                            (fld == LMA_PB_EV_TITLE)  ? ev->title :
                            (fld == LMA_PB_EV_NOTES)  ? ev->notes :
                                                        ev->pic;
                size_t cap = (fld == LMA_PB_EV_UID)    ? sizeof(ev->uid) :
                             (fld == LMA_PB_EV_TITLE)  ? sizeof(ev->title) :
                             (fld == LMA_PB_EV_NOTES)  ? sizeof(ev->notes) :
                                                         sizeof(ev->pic);
                copy_str(p, n, dst, cap);
            }
            break;
        case LMA_PB_EV_START:
        case LMA_PB_EV_END:
        case LMA_PB_EV_DELETED:
            if (wt != 0 || !read_var(data, len, &pos, &v)) {
                if (skip_field(data, len, &pos, wt) != 0) {
                    return 0;
                }
                break;
            }
            if (fld == LMA_PB_EV_START) {
                ev->start_ms = v;
            } else if (fld == LMA_PB_EV_END) {
                ev->end_ms = v;
            } else {
                ev->deleted = (v != 0);
            }
            break;
        case LMA_PB_EV_PICDATA:
            /* The headline tile: stored once at bundle level. */
            if (wt != 2 || !read_ld(data, len, &pos, &p, &n)) {
                if (skip_field(data, len, &pos, wt) != 0) {
                    return 0;
                }
                break;
            }
            {
                size_t copy = (n > LMA_PB_PIC_TILE_BYTES) ? LMA_PB_PIC_TILE_BYTES : n;
                memcpy(bundle->pic_data, p, copy);
                bundle->pic_data_len = copy;
                saw_pic = true;
            }
            break;
        case LMA_PB_EV_WEATHER:
            /* Weather submessage: forecast at start_ms (absent = none). */
            if (wt != 2 || !read_ld(data, len, &pos, &p, &n)) {
                if (skip_field(data, len, &pos, wt) != 0) {
                    return 0;
                }
                break;
            }
            if (!weather_decode(p, n, &ev->weather)) {
                return 0;
            }
            break;
        default:
            if (skip_field(data, len, &pos, wt) != 0) {
                return 0;
            }
            break;
        }
    }
    /* Bind the tile to the event that carried it.  uid is normally field 1
     * (written first), but protobuf field order is not guaranteed -- binding
     * after the whole event is decoded makes it order-independent. */
    if (saw_pic) {
        copy_str((const uint8_t *)ev->uid, strlen(ev->uid),
                 bundle->pic_uid, sizeof(bundle->pic_uid));
    }
    return 1;
}

void lma_proto_bundle_init(lma_cal_bundle_t *out)
{
    memset(out, 0, sizeof(*out));
}

int lma_proto_bundle_decode(const uint8_t *data, size_t len, lma_cal_bundle_t *out)
{
    size_t pos = 0;
    uint64_t v;
    const uint8_t *p;
    size_t n;

    if (out == NULL) {
        return 0;
    }
    lma_proto_bundle_init(out);

    while (pos < len) {
        uint64_t key;
        if (!read_var(data, len, &pos, &key)) {
            return 0;
        }
        uint32_t fld = (uint32_t)(key >> 3);
        unsigned wt = (unsigned)(key & 7u);

        switch (fld) {
        case LMA_PB_BUNDLE_FORMAT:
            if (wt != 0 || !read_var(data, len, &pos, &v)) {
                if (skip_field(data, len, &pos, wt) != 0) {
                    return 0;
                }
                break;
            }
            out->format = (uint32_t)v;
            break;
        case LMA_PB_BUNDLE_CALID:
            if (wt != 2 || !read_ld(data, len, &pos, &p, &n)) {
                if (skip_field(data, len, &pos, wt) != 0) {
                    return 0;
                }
                break;
            }
            copy_str(p, n, out->calendar_id, sizeof(out->calendar_id));
            break;
        case LMA_PB_BUNDLE_EVENT:
            if (wt != 2 || !read_ld(data, len, &pos, &p, &n)) {
                if (skip_field(data, len, &pos, wt) != 0) {
                    return 0;
                }
                break;
            }
            if (out->event_count < LMA_PB_EVENT_MAX) {
                if (!event_decode(p, n, &out->events[out->event_count], out)) {
                    return 0;
                }
                out->event_count++;
            }
            /* else: the event is already consumed by read_ld; drop it. */
            break;
        case LMA_PB_BUNDLE_WM:
            if (wt != 0 || !read_var(data, len, &pos, &v)) {
                if (skip_field(data, len, &pos, wt) != 0) {
                    return 0;
                }
                break;
            }
            out->watermark_ms = v;
            break;
        case LMA_PB_BUNDLE_CURRENT:
            /* Weather: conditions at reply time (absent = weather disabled). */
            if (wt != 2 || !read_ld(data, len, &pos, &p, &n)) {
                if (skip_field(data, len, &pos, wt) != 0) {
                    return 0;
                }
                break;
            }
            if (!weather_decode(p, n, &out->current)) {
                return 0;
            }
            break;
        default:
            if (skip_field(data, len, &pos, wt) != 0) {
                return 0;
            }
            break;
        }
    }
    return 1;
}

/* ---------------------------------------------------------- encode side */

/* Sizes are computed first, then written: a length-delimited submessage needs
 * its byte length BEFORE its body, so the encoder measures the body with the
 * same helpers it writes with (no scratch buffer large enough for a 4 KB tile
 * would fit a device stack). */
static size_t uv_len(uint64_t v)
{
    size_t n = 1;
    while ((v >>= 7) != 0) {
        n++;
    }
    return n;
}

static size_t var_field_size(uint32_t fld, uint64_t v)
{
    return uv_len(((uint64_t)fld << 3) | 0u) + uv_len(v);
}

static size_t ld_field_size(uint32_t fld, size_t n)
{
    return uv_len(((uint64_t)fld << 3) | 2u) + uv_len(n) + n;
}

/* sint32 zigzag: (n << 1) ^ (n >> 31) without shifting a negative int (UB). */
static uint64_t zigzag32(int32_t v)
{
    const uint32_t u = (uint32_t)v;
    return (uint64_t)((u << 1) ^ ((u & 0x80000000u) ? 0xFFFFFFFFu : 0u));
}

/* Write a length-delimited header (tag + length) for `n` body bytes. */
static int write_ld_head(uint8_t *buf, size_t cap, size_t *pos, uint32_t fld,
                         size_t n)
{
    uint8_t key[10];
    uint8_t lenb[10];
    size_t kn = uv_encode(((uint64_t)fld << 3) | 2u, key);
    size_t ln = uv_encode(n, lenb);
    if (*pos + kn + ln > cap) {
        return 0;
    }
    memcpy(buf + *pos, key, kn);
    *pos += kn;
    memcpy(buf + *pos, lenb, ln);
    *pos += ln;
    return 1;
}

static size_t weather_size(const lma_weather_t *wx, uint32_t fld)
{
    if (!wx->present) {
        return 0;   /* absent message == "no forecast for this slot" */
    }
    const size_t body = var_field_size(LMA_PB_WX_TEMP, zigzag32(wx->temp_c10)) +
                        var_field_size(LMA_PB_WX_CODE, wx->code);
    return ld_field_size(fld, body);
}

static int write_weather(uint8_t *buf, size_t cap, size_t *pos, uint32_t fld,
                         const lma_weather_t *wx)
{
    if (!wx->present) {
        return 1;
    }
    const size_t body = var_field_size(LMA_PB_WX_TEMP, zigzag32(wx->temp_c10)) +
                        var_field_size(LMA_PB_WX_CODE, wx->code);
    if (!write_ld_head(buf, cap, pos, fld, body) ||
        !write_varint_field(buf, cap, pos, LMA_PB_WX_TEMP,
                            zigzag32(wx->temp_c10)) ||
        !write_varint_field(buf, cap, pos, LMA_PB_WX_CODE, wx->code)) {
        return 0;
    }
    return 1;
}

static bool event_has_tile(const lma_cal_event_t *ev,
                           const lma_cal_bundle_t *b)
{
    return b->pic_data_len > 0 && strcmp(ev->uid, b->pic_uid) == 0;
}

static size_t event_size(const lma_cal_event_t *ev,
                         const lma_cal_bundle_t *b)
{
    size_t n = ld_field_size(LMA_PB_EV_UID, strlen(ev->uid)) +
               ld_field_size(LMA_PB_EV_TITLE, strlen(ev->title)) +
               ld_field_size(LMA_PB_EV_NOTES, strlen(ev->notes)) +
               var_field_size(LMA_PB_EV_START, ev->start_ms) +
               var_field_size(LMA_PB_EV_END, ev->end_ms) +
               var_field_size(LMA_PB_EV_DELETED, ev->deleted ? 1u : 0u) +
               ld_field_size(LMA_PB_EV_PIC, strlen(ev->pic)) +
               weather_size(&ev->weather, LMA_PB_EV_WEATHER);
    if (event_has_tile(ev, b)) {
        n += ld_field_size(LMA_PB_EV_PICDATA, b->pic_data_len);
    }
    return n;
}

static int write_event(uint8_t *buf, size_t cap, size_t *pos,
                       const lma_cal_event_t *ev, const lma_cal_bundle_t *b)
{
    if (!write_ld_head(buf, cap, pos, LMA_PB_BUNDLE_EVENT, event_size(ev, b)) ||
        !write_ld_field(buf, cap, pos, LMA_PB_EV_UID, ev->uid) ||
        !write_ld_field(buf, cap, pos, LMA_PB_EV_TITLE, ev->title) ||
        !write_ld_field(buf, cap, pos, LMA_PB_EV_NOTES, ev->notes) ||
        !write_varint_field(buf, cap, pos, LMA_PB_EV_START, ev->start_ms) ||
        !write_varint_field(buf, cap, pos, LMA_PB_EV_END, ev->end_ms) ||
        !write_varint_field(buf, cap, pos, LMA_PB_EV_DELETED,
                            ev->deleted ? 1u : 0u) ||
        !write_ld_field(buf, cap, pos, LMA_PB_EV_PIC, ev->pic) ||
        !write_weather(buf, cap, pos, LMA_PB_EV_WEATHER, &ev->weather)) {
        return 0;
    }
    if (event_has_tile(ev, b)) {
        /* The headline tile rides on the event it belongs to. */
        if (!write_ld_head(buf, cap, pos, LMA_PB_EV_PICDATA, b->pic_data_len)) {
            return 0;
        }
        if (*pos + b->pic_data_len > cap) {
            return 0;
        }
        memcpy(buf + *pos, b->pic_data, b->pic_data_len);
        *pos += b->pic_data_len;
    }
    return 1;
}

int lma_proto_bundle_encode(const lma_cal_bundle_t *bundle, uint8_t *buf,
                            size_t cap)
{
    size_t pos = 0;

    if (bundle == NULL || buf == NULL) {
        return -1;
    }
    if (!write_varint_field(buf, cap, &pos, LMA_PB_BUNDLE_FORMAT,
                            bundle->format) ||
        !write_ld_field(buf, cap, &pos, LMA_PB_BUNDLE_CALID,
                        bundle->calendar_id)) {
        return -1;
    }
    for (size_t i = 0; i < bundle->event_count; i++) {
        if (!write_event(buf, cap, &pos, &bundle->events[i], bundle)) {
            return -1;
        }
    }
    if (!write_varint_field(buf, cap, &pos, LMA_PB_BUNDLE_WM,
                            bundle->watermark_ms) ||
        !write_weather(buf, cap, &pos, LMA_PB_BUNDLE_CURRENT,
                       &bundle->current)) {
        return -1;
    }
    return (int)pos;
}
