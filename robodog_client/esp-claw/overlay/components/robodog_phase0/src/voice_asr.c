/*
 * RoboDog explicit ASR response parser — see voice_asr.h.
 *
 * SPDX-License-Identifier: Apache-2.0
 */
#include "voice_asr.h"

#include <stdint.h>
#include <string.h>

/* ── JSON string scanning ───────────────────────────────────────────────── */

static bool is_ws(char c)
{
    return c == ' ' || c == '\t' || c == '\n' || c == '\r';
}

static const char *skip_ws(const char *p)
{
    while (p != NULL && is_ws(*p)) {
        p++;
    }
    return p;
}

/* Append one UTF-8 codepoint, refusing to split it at the cap. */
static size_t put_cp(char *out, size_t o, size_t cap, uint32_t cp)
{
    unsigned char b[4];
    size_t n;
    if (cp < 0x80) {
        b[0] = (unsigned char)cp;
        n = 1;
    } else if (cp < 0x800) {
        b[0] = (unsigned char)(0xC0 | (cp >> 6));
        b[1] = (unsigned char)(0x80 | (cp & 0x3F));
        n = 2;
    } else if (cp < 0x10000) {
        b[0] = (unsigned char)(0xE0 | (cp >> 12));
        b[1] = (unsigned char)(0x80 | ((cp >> 6) & 0x3F));
        b[2] = (unsigned char)(0x80 | (cp & 0x3F));
        n = 3;
    } else {
        b[0] = (unsigned char)(0xF0 | (cp >> 18));
        b[1] = (unsigned char)(0x80 | ((cp >> 12) & 0x3F));
        b[2] = (unsigned char)(0x80 | ((cp >> 6) & 0x3F));
        b[3] = (unsigned char)(0x80 | (cp & 0x3F));
        n = 4;
    }
    if (o + n > cap - 1) {
        return o; /* truncated on a codepoint boundary */
    }
    memcpy(out + o, b, n);
    return o + n;
}

/* Parse one JSON string starting at the opening quote.  Returns the pointer
 * just past the closing quote, or NULL on malformed input. */
static const char *json_string(const char *p, char *out, size_t cap)
{
    if (p == NULL || *p != '"' || cap == 0) {
        return NULL;
    }
    size_t o = 0;
    p++;
    while (*p != '\0') {
        const unsigned char c = (unsigned char)*p++;
        if (c == '"') {
            out[o] = '\0';
            return p;
        }
        if (c != '\\') {
            if (o + 1 > cap - 1) {
                out[o] = '\0'; /* the string is longer than the caller's cap */
                return NULL;
            }
            out[o++] = (char)c;
            continue;
        }
        const char e = *p++;
        switch (e) {
        case '"':  o = put_cp(out, o, cap, '"');  break;
        case '\\': o = put_cp(out, o, cap, '\\'); break;
        case '/':  o = put_cp(out, o, cap, '/');  break;
        case 'b':  o = put_cp(out, o, cap, '\b'); break;
        case 'f':  o = put_cp(out, o, cap, '\f'); break;
        case 'n':  o = put_cp(out, o, cap, '\n'); break;
        case 'r':  o = put_cp(out, o, cap, '\r'); break;
        case 't':  o = put_cp(out, o, cap, '\t'); break;
        case 'u': {
            unsigned cp = 0;
            for (int i = 0; i < 4; i++) {
                const char h = *p++;
                cp <<= 4;
                if (h >= '0' && h <= '9') {
                    cp |= (unsigned)(h - '0');
                } else if (h >= 'a' && h <= 'f') {
                    cp |= (unsigned)(h - 'a' + 10);
                } else if (h >= 'A' && h <= 'F') {
                    cp |= (unsigned)(h - 'A' + 10);
                } else {
                    return NULL;
                }
            }
            /* Combine a UTF-16 surrogate pair when the model escaped it. */
            if (cp >= 0xD800 && cp <= 0xDBFF && p[0] == '\\' && p[1] == 'u') {
                unsigned lo = 0;
                const char *q = p + 2;
                bool ok = true;
                for (int i = 0; i < 4; i++) {
                    const char h = *q++;
                    lo <<= 4;
                    if (h >= '0' && h <= '9') {
                        lo |= (unsigned)(h - '0');
                    } else if (h >= 'a' && h <= 'f') {
                        lo |= (unsigned)(h - 'a' + 10);
                    } else if (h >= 'A' && h <= 'F') {
                        lo |= (unsigned)(h - 'A' + 10);
                    } else {
                        ok = false;
                        break;
                    }
                }
                if (ok && lo >= 0xDC00 && lo <= 0xDFFF) {
                    cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                    p = q;
                }
            }
            o = put_cp(out, o, cap, cp);
            break;
        }
        default:
            o = put_cp(out, o, cap, (unsigned char)e);
            break;
        }
    }
    return NULL; /* unterminated */
}

/* Locate the string value of `key` inside `obj` (a JSON object text).
 * Returns true when found + parsed. */
static bool object_string(const char *obj, const char *key,
                          char *out, size_t cap)
{
    if (obj == NULL || key == NULL) {
        return false;
    }
    char pat[40];
    const size_t klen = strlen(key);
    if (klen + 3 >= sizeof(pat)) {
        return false;
    }
    pat[0] = '"';
    memcpy(pat + 1, key, klen);
    pat[klen + 1] = '"';
    pat[klen + 2] = '\0';
    const char *p = strstr(obj, pat);
    if (p == NULL) {
        return false;
    }
    p = skip_ws(p + klen + 2);
    if (p == NULL || *p != ':') {
        return false;
    }
    p = skip_ws(p + 1);
    if (p == NULL || *p != '"') {
        return false;
    }
    return json_string(p, out, cap) != NULL;
}

/* Trim leading/trailing ASCII whitespace in place. */
static char *trim(char *s)
{
    while (*s != '\0' && is_ws(*s)) {
        s++;
    }
    size_t n = strlen(s);
    while (n > 0 && is_ws(s[n - 1])) {
        s[--n] = '\0';
    }
    return s;
}

/* UTF-8-safe bounded copy: never split a multi-byte sequence. */
static void copy_utf8(char *dst, size_t cap, const char *src)
{
    if (cap == 0) {
        return;
    }
    size_t n = strlen(src);
    if (n > cap - 1) {
        n = cap - 1;
        while (n > 0 && ((unsigned char)src[n] & 0xC0) == 0x80) {
            n--;
        }
    }
    memcpy(dst, src, n);
    dst[n] = '\0';
}

/* Strip a ```json … ``` fence (the model wraps the object that way half the
 * time).  `s` is already leading-trimmed; the trailing fence/whitespace is
 * removed in place. */
static void strip_fence(char *s)
{
    if (strncmp(s, "```", 3) != 0) {
        return;
    }
    char *nl = strchr(s, '\n');
    if (nl == NULL) {
        return;
    }
    memmove(s, nl + 1, strlen(nl + 1) + 1);
    trim(s);
    size_t n = strlen(s);
    if (n >= 3 && strcmp(s + n - 3, "```") == 0) {
        s[n - 3] = '\0';
        trim(s);
    }
}

/* ── public entry point ─────────────────────────────────────────────────── */

bool robodog_asr_parse(const char *body, char *lang, size_t lang_cap,
                       char *text, size_t text_cap)
{
    if (lang != NULL && lang_cap > 0) {
        lang[0] = '\0';
    }
    if (text != NULL && text_cap > 0) {
        text[0] = '\0';
    }
    if (body == NULL || text == NULL || text_cap == 0) {
        return false;
    }

    /* The transcript is `choices[0].message.content`; a Whisper-shaped body
     * (`{"text":"..."}`) is also accepted as a fallback. */
    char content[1024];
    content[0] = '\0';
    const char *msg = strstr(body, "\"message\"");
    const char *cp = msg != NULL ? strstr(msg, "\"content\"")
                                 : strstr(body, "\"content\"");
    if (cp != NULL) {
        cp = skip_ws(cp + 9);
        if (cp != NULL && *cp == ':') {
            cp = skip_ws(cp + 1);
            if (cp != NULL && *cp == '"') {
                (void)json_string(cp, content, sizeof(content));
            }
        }
    }
    if (content[0] == '\0') {
        /* Whisper / transcription-endpoint shape. */
        if (!object_string(body, "text", content, sizeof(content))) {
            return false;
        }
    }

    char *s = trim(content);
    strip_fence(s);
    s = trim(s);
    if (s[0] == '\0') {
        return false;
    }

    /* The prompt asks for {"language":"..","text":".."}; use it when the
     * model actually returned the object. */
    if (s[0] == '{') {
        char inner[1024];
        char inner_lang[16];
        if (object_string(s, "text", inner, sizeof(inner))) {
            if (lang != NULL && lang_cap > 0) {
                (void)object_string(s, "language", inner_lang,
                                    sizeof(inner_lang));
                copy_utf8(lang, lang_cap, inner_lang);
            }
            char *t = trim(inner);
            if (t[0] != '\0') {
                copy_utf8(text, text_cap, t);
                return true;
            }
        }
    }

    /* Plain transcript (no inner object): the whole content is the text. */
    copy_utf8(text, text_cap, s);
    return text[0] != '\0';
}
