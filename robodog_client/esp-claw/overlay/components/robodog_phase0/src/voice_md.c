/*
 * RoboDog ESP-Claw markdown -> speech-friendly text (LMAO-owned, PURE C).
 *
 * Implementation of robodog_voice_md_strip() — see voice_md.h for the
 * contract.  Line-oriented constructs (headings, bullets, blockquotes,
 * whole-line noise) are handled per line; inline markers (emphasis, code,
 * strike, links, table pipes) per character.  Deliberately small and
 * deterministic: this turns an agent markdown reply into prose the TTS
 * endpoint speaks without "asterisk" — it is not a general markdown parser.
 *
 * SPDX-License-Identifier: Apache-2.0
 */
#include "voice_md.h"

#include <stdbool.h>

static bool is_hsp(char c)
{
    return c == ' ' || c == '\t';
}

/* Append one char if the buffer has room (always reserving the NUL slot).
 * Returns false when the destination is full (callers stop writing). */
static bool put(char *dst, size_t cap, size_t *di, char c)
{
    if (*di + 1 >= cap) {
        return false;
    }
    dst[(*di)++] = c;
    return true;
}

/* ── Whole-line constructs (span [s, e); `s` is the first non-ws char) ──── */

/* Code-fence delimiter line: only backticks (>= 3) or only tildes (>= 3),
 * plus optional trailing whitespace. */
static bool is_fence_line(const char *src, size_t s, size_t e)
{
    const char c = src[s];
    if (c != '`' && c != '~') {
        return false;
    }
    size_t n = 0;
    for (size_t i = s; i < e; i++) {
        if (src[i] == c) {
            n++;
        } else if (!is_hsp(src[i])) {
            return false;
        }
    }
    return n >= 3;
}

/* Horizontal rule `---`/`***`/`___`: after dropping spaces, 3+ of a single
 * one of `- * _` (and nothing else). */
static bool is_hr_line(const char *src, size_t s, size_t e)
{
    char c = 0;
    size_t n = 0;
    for (size_t i = s; i < e; i++) {
        const char ch = src[i];
        if (is_hsp(ch)) {
            continue;
        }
        if (ch != '-' && ch != '*' && ch != '_') {
            return false;
        }
        if (c == 0) {
            c = ch;
        } else if (ch != c) {
            return false;
        }
        n++;
    }
    return n >= 3;
}

/* Table separator row `| --- | --- |`: after dropping spaces, only `|` `-`
 * `:` remain and there is at least one `|` AND one `-` (disjoint from a
 * plain horizontal rule, which has no pipe). */
static bool is_table_sep_line(const char *src, size_t s, size_t e)
{
    bool pipe = false, dash = false;
    for (size_t i = s; i < e; i++) {
        const char ch = src[i];
        if (is_hsp(ch)) {
            continue;
        }
        if (ch == '|') {
            pipe = true;
        } else if (ch == '-') {
            dash = true;
        } else if (ch != ':') {
            return false;
        }
    }
    return pipe && dash;
}

/* ── Inline filtering ───────────────────────────────────────────────────── */

/* Append the filtered characters of span [s, e).  Handles emphasis/code/
 * strike runs, link[text](url) (keeps only the visible text), table pipes,
 * and collapses horizontal whitespace runs to a single space. */
static void inline_flush(const char *src, size_t s, size_t e,
                         char *dst, size_t cap, size_t *di)
{
    size_t i = s;
    while (i < e) {
        const char c = src[i];
        /* Emphasis `**` `*` `__` `_`, code `` ` ``, strike `~`: skip the
         * whole run.  A lone `*` from prose "7*8" is sacrificed to the far
         * more common bold/italic markers (the model writes `x`/`times`). */
        if (c == '*' || c == '_' || c == '`' || c == '~') {
            while (i < e && src[i] == c) {
                i++;
            }
            continue;
        }
        /* Link `[text](url)`: keep ONLY the visible `text`, drop the URL. */
        if (c == '[') {
            size_t close = i + 1;
            while (close < e && src[close] != ']') {
                close++;
            }
            if (close < e && close + 1 < e && src[close + 1] == '(') {
                size_t pend = close + 2;
                while (pend < e && src[pend] != ')') {
                    pend++;
                }
                if (pend < e) {
                    inline_flush(src, i + 1, close, dst, cap, di);
                    i = pend + 1;
                    continue;
                }
            }
            /* The '[' was not a link opener: keep it literally. */
            if (!put(dst, cap, di, '[')) {
                return;
            }
            i++;
            continue;
        }
        /* Table cell separator `|` between words -> one space. */
        if (c == '|') {
            if (*di > 0 && dst[*di - 1] != ' ' && dst[*di - 1] != '\n') {
                if (!put(dst, cap, di, ' ')) {
                    return;
                }
            }
            i++;
            continue;
        }
        if (c == '\r') { /* CRLF sources: drop the CR (LF becomes the break) */
            i++;
            continue;
        }
        if (c == ' ') {
            /* Collapse horizontal whitespace runs to a single space (and
             * never start a line with one, e.g. after a table pipe). */
            if (*di > 0 && dst[*di - 1] != ' ' && dst[*di - 1] != '\n') {
                if (!put(dst, cap, di, ' ')) {
                    return;
                }
            }
            i++;
            continue;
        }
        if (!put(dst, cap, di, c)) {
            return;
        }
        i++;
    }
}

size_t robodog_voice_md_strip(const char *src, char *dst, size_t dst_cap)
{
    if (dst_cap == 0) {
        return 0;
    }
    size_t di = 0;
    size_t pos = 0;
    while (src[pos] != '\0') {
        /* Current line span [pos, eol); eol points at '\n' or NUL. */
        size_t eol = pos;
        while (src[eol] != '\0' && src[eol] != '\n') {
            eol++;
        }
        const bool has_nl = (src[eol] == '\n');

        /* First non-whitespace char of the line (content start). */
        size_t li = pos;
        while (li < eol && is_hsp(src[li])) {
            li++;
        }

        bool wrote_line = false;
        if (li < eol) {
            if (is_fence_line(src, li, eol) || is_table_sep_line(src, li, eol) ||
                is_hr_line(src, li, eol)) {
                /* Whole-line noise: drop it; the paragraphs around it keep
                 * their own newline, so prose stays separated. */
            } else {
                /* Headings `#`... `##` ... (+ a space or EOL). */
                size_t h = li;
                while (h < eol && src[h] == '#') {
                    h++;
                }
                if (h > li && (h >= eol || is_hsp(src[h]))) {
                    li = h;
                    while (li < eol && is_hsp(src[li])) {
                        li++;
                    }
                }
                /* Blockquote `>` (+ optional one space), possibly nested. */
                while (li < eol && src[li] == '>') {
                    li++;
                    if (li < eol && src[li] == ' ') {
                        li++;
                    }
                }
                /* List bullets `- ` `* ` `+ `: marker + ws (or EOL).  NOT a
                 * bullet when the next char is non-ws, so a leading "-5" or
                 * "+5" survives as a numeric token. */
                if (li < eol && (src[li] == '-' || src[li] == '*' ||
                                 src[li] == '+') &&
                    (li + 1 >= eol || is_hsp(src[li + 1]))) {
                    li++;
                    while (li < eol && is_hsp(src[li])) {
                        li++;
                    }
                }

                const size_t line_start = di;
                inline_flush(src, li, eol, dst, dst_cap, &di);
                /* Trim any trailing space this line emitted. */
                while (di > line_start && dst[di - 1] == ' ') {
                    di--;
                }
                wrote_line = (di > line_start);
            }
        }
        if (wrote_line && has_nl && !put(dst, dst_cap, &di, '\n')) {
            break;
        }
        pos = has_nl ? (eol + 1) : eol;
    }
    /* Drop trailing newlines (a final line ends the text; no trailing
     * break makes document ends cleaner in display + TTS). */
    while (di > 0 && dst[di - 1] == '\n') {
        di--;
    }
    if (di < dst_cap) {
        dst[di] = '\0';
    }
    return di;
}
