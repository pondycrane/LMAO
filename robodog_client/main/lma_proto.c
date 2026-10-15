/*
 * lma_proto.c -- hand-rolled protobuf codec, see lma_proto.h for the field
 * contract.  stdint/stddef/stdbool + fixed buffers only (no malloc, no lwip),
 * so it is host-compilable for the self-test.
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
    uint64_t v;
    switch (wt) {
    case 0: /* varint */
        return uv_decode(data, len, pos, &v);
    case 1: /* fixed64 */
        if (*pos + 8 > len) {
            return 1;
        }
        *pos += 8;
        return 0;
    case 2: /* length-delimited */
        if (uv_decode(data, len, pos, &v) != 0) {
            return 1;
        }
        if (*pos + (size_t)v > len) {
            return 1;
        }
        *pos += (size_t)v;
        return 0;
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
 * Copy a length-delimited string; flen bytes are consumed even if truncated to
 * fit dst[0..cap).  dst is always NUL-terminated.
 */
static void read_string(const uint8_t *data, size_t *pos,
                        size_t flen, char *dst, size_t cap)
{
    size_t copy = (flen < cap - 1) ? flen : (cap - 1);
    memcpy(dst, data + *pos, copy);
    dst[copy] = '\0';
    *pos += flen;
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
    return (int)pos;
}

int lma_proto_req_decode(const uint8_t *data, size_t len, lma_cal_request_t *out)
{
    size_t pos = 0;
    uint64_t v;

    if (out == NULL) {
        return 0;
    }
    memset(out->calendar_id, 0, sizeof(out->calendar_id));
    out->since_ms = 0;
    out->max_events = 0;

    while (pos < len) {
        uint64_t key;
        if (uv_decode(data, len, &pos, &key) != 0) {
            return 0;
        }
        uint32_t fld = (uint32_t)(key >> 3);
        unsigned wt = (unsigned)(key & 7u);

        switch (fld) {
        case LMA_PB_CALREQ_ID:
            if (wt != 2 || uv_decode(data, len, &pos, &v) != 0 ||
                (size_t)v > len - pos) {
                if (skip_field(data, len, &pos, wt) != 0) {
                    return 0;
                }
                break;
            }
            read_string(data, &pos, (size_t)v, out->calendar_id,
                        sizeof(out->calendar_id));
            break;
        case LMA_PB_CALREQ_SINCE:
            if (wt != 0 || uv_decode(data, len, &pos, &v) != 0) {
                if (skip_field(data, len, &pos, wt) != 0) {
                    return 0;
                }
                break;
            }
            out->since_ms = v;
            break;
        case LMA_PB_CALREQ_MAX:
            if (wt != 0 || uv_decode(data, len, &pos, &v) != 0) {
                if (skip_field(data, len, &pos, wt) != 0) {
                    return 0;
                }
                break;
            }
            out->max_events = (uint32_t)v;
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

/*
 * Decode one CalEvent (length-delimited field 3 of the bundle).  `end` is the
 * exclusive byte offset of the event's payload within data; it doubles as the
 * hard bound for every read so truncation can never overrun the buffer.
 */
static int event_decode(const uint8_t *data, size_t *pos, size_t end,
                        lma_cal_event_t *ev)
{
    uint64_t v;

    memset(ev, 0, sizeof(*ev));
    while (*pos < end) {
        uint64_t key;
        if (uv_decode(data, end, pos, &key) != 0 || *pos > end) {
            return 0;
        }
        uint32_t fld = (uint32_t)(key >> 3);
        unsigned wt = (unsigned)(key & 7u);

        switch (fld) {
        case LMA_PB_EV_UID:
        case LMA_PB_EV_TITLE:
        case LMA_PB_EV_NOTES:
        case LMA_PB_EV_PIC:
            if (wt != 2 || uv_decode(data, end, pos, &v) != 0 ||
                (size_t)v > end - *pos) {
                if (skip_field(data, end, pos, wt) != 0) {
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
                read_string(data, pos, (size_t)v, dst, cap);
            }
            break;
        case LMA_PB_EV_START:
            if (wt != 0 || uv_decode(data, end, pos, &v) != 0) {
                if (skip_field(data, end, pos, wt) != 0) {
                    return 0;
                }
                break;
            }
            ev->start_ms = v;
            break;
        case LMA_PB_EV_END:
            if (wt != 0 || uv_decode(data, end, pos, &v) != 0) {
                if (skip_field(data, end, pos, wt) != 0) {
                    return 0;
                }
                break;
            }
            ev->end_ms = v;
            break;
        case LMA_PB_EV_DELETED:
            if (wt != 0 || uv_decode(data, end, pos, &v) != 0) {
                if (skip_field(data, end, pos, wt) != 0) {
                    return 0;
                }
                break;
            }
            ev->deleted = (v != 0);
            break;
        case LMA_PB_EV_PICDATA:
            /* bytes: 1-bit 32x32 tile served from CalEvent.pic_data. */
            if (wt != 2 || uv_decode(data, end, pos, &v) != 0 ||
                (size_t)v > end - *pos) {
                if (skip_field(data, end, pos, wt) != 0) {
                    return 0;
                }
                break;
            }
            {
                size_t n = (size_t)v;
                if (n > LMA_PB_PIC_TILE_BYTES) {
                    n = LMA_PB_PIC_TILE_BYTES;
                }
                memcpy(ev->pic_data, data + *pos, n);
                *pos += (size_t)v; /* consume the full field even if capped */
                ev->pic_data_len = n;
            }
            break;
        default:
            if (skip_field(data, end, pos, wt) != 0) {
                return 0;
            }
            break;
        }
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

    if (out == NULL) {
        return 0;
    }
    lma_proto_bundle_init(out);

    while (pos < len) {
        uint64_t key;
        if (uv_decode(data, len, &pos, &key) != 0) {
            return 0;
        }
        uint32_t fld = (uint32_t)(key >> 3);
        unsigned wt = (unsigned)(key & 7u);

        switch (fld) {
        case LMA_PB_BUNDLE_FORMAT:
            if (wt != 0 || uv_decode(data, len, &pos, &v) != 0) {
                if (skip_field(data, len, &pos, wt) != 0) {
                    return 0;
                }
                break;
            }
            out->format = (uint32_t)v;
            break;
        case LMA_PB_BUNDLE_CALID:
            if (wt != 2 || uv_decode(data, len, &pos, &v) != 0 ||
                (size_t)v > len - pos) {
                if (skip_field(data, len, &pos, wt) != 0) {
                    return 0;
                }
                break;
            }
            read_string(data, &pos, (size_t)v, out->calendar_id,
                        sizeof(out->calendar_id));
            break;
        case LMA_PB_BUNDLE_EVENT:
            if (wt != 2 || uv_decode(data, len, &pos, &v) != 0 ||
                (size_t)v > len - pos) {
                if (skip_field(data, len, &pos, wt) != 0) {
                    return 0;
                }
                break;
            }
            if (out->event_count < LMA_PB_EVENT_MAX) {
                size_t end = pos + (size_t)v;
                if (!event_decode(data, &pos, end,
                                  &out->events[out->event_count])) {
                    return 0;
                }
                out->event_count++;
            } else {
                pos += (size_t)v; /* drop overflowing events */
            }
            break;
        case LMA_PB_BUNDLE_WM:
            if (wt != 0 || uv_decode(data, len, &pos, &v) != 0) {
                if (skip_field(data, len, &pos, wt) != 0) {
                    return 0;
                }
                break;
            }
            out->watermark_ms = v;
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
