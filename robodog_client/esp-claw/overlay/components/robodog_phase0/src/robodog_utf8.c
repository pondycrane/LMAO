/*
 * RoboDog ESP-Claw UTF-8 helpers (LMAO-owned, PURE C).  See robodog_utf8.h.
 *
 * SPDX-License-Identifier: Apache-2.0
 */
#include "robodog_utf8.h"

#include <string.h>

/* ── glyph coverage ─────────────────────────────────────────────────────── */

/* MUST mirror the -r ranges in gen_font_tc_16.sh: a codepoint reported as
 * supported but absent from the font renders as LVGL's placeholder box, and
 * one reported unsupported is replaced with '?' by the sanitizer. */
static bool in_range(uint32_t cp, uint32_t lo, uint32_t hi)
{
    return cp >= lo && cp <= hi;
}

bool robodog_utf8_glyph_supported(uint32_t cp)
{
    return in_range(cp, 0x0020, 0x007E) ||   /* printable ASCII */
           in_range(cp, 0x00A0, 0x00FF) ||   /* Latin-1 supplement (°C, ±, ×) */
           in_range(cp, 0x2000, 0x206F) ||   /* general punctuation */
           in_range(cp, 0x3000, 0x303F) ||   /* CJK symbols + punctuation */
           in_range(cp, 0x3100, 0x312F) ||   /* Bopomofo */
           in_range(cp, 0x3400, 0x4DBF) ||   /* CJK ext. A */
           in_range(cp, 0x4E00, 0x9FFF) ||   /* CJK unified ideographs */
           in_range(cp, 0xF900, 0xFAFF) ||   /* CJK compatibility ideographs */
           in_range(cp, 0xFE30, 0xFE4F) ||   /* CJK compatibility forms */
           in_range(cp, 0xFF00, 0xFFEF);     /* fullwidth/halfwidth forms */
}

/* ── decoding ───────────────────────────────────────────────────────────── */

size_t robodog_utf8_seq_len(unsigned char lead)
{
    if (lead < 0x80) {
        return 1;
    }
    if ((lead & 0xE0) == 0xC0) {
        return 2;
    }
    if ((lead & 0xF0) == 0xE0) {
        return 3;
    }
    if ((lead & 0xF8) == 0xF0) {
        return 4;
    }
    return 0; /* continuation byte or 0xF8..0xFF: not a lead */
}

size_t robodog_utf8_decode(const char *s, size_t len, uint32_t *cp)
{
    if (s == NULL || len == 0) {
        if (cp != NULL) {
            *cp = ROBODOG_UTF8_INVALID;
        }
        return 0;
    }
    const unsigned char c0 = (unsigned char)s[0];
    if (c0 < 0x80) {
        if (cp != NULL) {
            *cp = c0;
        }
        return 1;
    }
    size_t need;
    uint32_t v;
    if ((c0 & 0xE0) == 0xC0) {
        need = 2;
        v = c0 & 0x1Fu;
    } else if ((c0 & 0xF0) == 0xE0) {
        need = 3;
        v = c0 & 0x0Fu;
    } else if ((c0 & 0xF8) == 0xF0) {
        need = 4;
        v = c0 & 0x07u;
    } else {
        if (cp != NULL) {
            *cp = ROBODOG_UTF8_INVALID;
        }
        return 0;
    }
    if (len < need) {
        if (cp != NULL) {
            *cp = ROBODOG_UTF8_INVALID;
        }
        return 0;
    }
    for (size_t i = 1; i < need; i++) {
        const unsigned char ci = (unsigned char)s[i];
        if ((ci & 0xC0) != 0x80) {
            if (cp != NULL) {
                *cp = ROBODOG_UTF8_INVALID;
            }
            return 0;
        }
        v = (v << 6) | (uint32_t)(ci & 0x3F);
    }
    /* Reject overlong encodings, surrogates and out-of-range codepoints. */
    if ((need == 2 && v < 0x80) || (need == 3 && v < 0x800) ||
        (need == 4 && v < 0x10000) || v > 0x10FFFF ||
        (v >= 0xD800 && v <= 0xDFFF)) {
        if (cp != NULL) {
            *cp = ROBODOG_UTF8_INVALID;
        }
        return 0;
    }
    if (cp != NULL) {
        *cp = v;
    }
    return need;
}

/* ── boundaries ─────────────────────────────────────────────────────────── */

size_t robodog_utf8_prev_boundary(const char *s, size_t i)
{
    if (s == NULL) {
        return 0;
    }
    while (i > 0 && ((unsigned char)s[i] & 0xC0) == 0x80) {
        i--;
    }
    return i;
}

size_t robodog_utf8_next_boundary(const char *s, size_t i)
{
    if (s == NULL) {
        return 0;
    }
    const size_t len = strlen(s);
    if (i >= len) {
        return len;
    }
    /* If i is inside a sequence, step over its continuation bytes first. */
    while (i < len && ((unsigned char)s[i] & 0xC0) == 0x80) {
        i++;
    }
    return i < len ? i : len;
}

/* ── sanitisation ───────────────────────────────────────────────────────── */

size_t robodog_utf8_sanitize(char *dst, size_t dst_cap, const char *src,
                             char replace)
{
    if (dst == NULL || dst_cap == 0) {
        return 0;
    }
    dst[0] = '\0';
    if (src == NULL) {
        return 0;
    }
    const size_t len = strlen(src);
    size_t d = 0;
    size_t pos = 0;
    while (pos < len) {
        uint32_t cp = ROBODOG_UTF8_INVALID;
        const size_t clen = robodog_utf8_decode(src + pos, len - pos, &cp);
        if (clen == 0) {
            /* Malformed byte: one replacement glyph, advance one byte. */
            if (d + 1 >= dst_cap) {
                break;
            }
            dst[d++] = replace;
            pos++;
            continue;
        }
        if (cp == '\n' || cp == '\t') {
            if (d + 1 >= dst_cap) {
                break;
            }
            dst[d++] = (char)cp;
            pos += clen;
            continue;
        }
        if (cp < 0x20 || cp == 0x7F) {
            /* Other control chars (incl. CRLF's '\r'): dropped silently. */
            pos += clen;
            continue;
        }
        if (robodog_utf8_glyph_supported(cp)) {
            if (d + clen >= dst_cap) {
                break;
            }
            memcpy(dst + d, src + pos, clen);
            d += clen;
            pos += clen;
            continue;
        }
        /* Undrawable codepoint (emoji, exotic script, ...): one '?'. */
        if (d + 1 >= dst_cap) {
            break;
        }
        dst[d++] = replace;
        pos += clen;
    }
    dst[d] = '\0';
    return d;
}

size_t robodog_utf8_strlen(const char *s)
{
    if (s == NULL) {
        return 0;
    }
    const size_t len = strlen(s);
    size_t n = 0;
    size_t pos = 0;
    while (pos < len) {
        uint32_t cp = 0;
        const size_t clen = robodog_utf8_decode(s + pos, len - pos, &cp);
        pos += clen > 0 ? clen : 1;
        n++;
    }
    return n;
}

bool robodog_utf8_has_cjk(const char *s)
{
    if (s == NULL) {
        return false;
    }
    const size_t len = strlen(s);
    size_t pos = 0;
    while (pos < len) {
        uint32_t cp = 0;
        const size_t clen = robodog_utf8_decode(s + pos, len - pos, &cp);
        if (clen == 0) {
            pos++;
            continue;
        }
        if ((cp >= 0x3400 && cp <= 0x4DBF) ||   /* CJK ext. A */
            (cp >= 0x4E00 && cp <= 0x9FFF) ||   /* CJK unified ideographs */
            (cp >= 0xF900 && cp <= 0xFAFF)) {   /* CJK compatibility ideographs */
            return true;
        }
        pos += clen;
    }
    return false;
}
