/*
 * RoboDog reply sanitiser — see voice_think.h.
 *
 * SPDX-License-Identifier: Apache-2.0
 */
#include "voice_think.h"

#include <string.h>

/* Built from parts so the literal never appears in this file (a model reading
 * the source would otherwise see its own marker). */
#define THINK_OPEN  "<" "think" ">"
#define THINK_CLOSE "<" "/" "think" ">"

size_t robodog_think_strip(const char *raw, char *out, size_t cap)
{
    if (out == NULL || cap == 0) {
        return 0;
    }
    if (raw == NULL) {
        out[0] = '\0';
        return 0;
    }
    size_t o = 0;
    bool in_think = false;
    for (const char *p = raw; *p != '\0' && o + 1 < cap; p++) {
        if (!in_think) {
            if (strncmp(p, THINK_OPEN, sizeof(THINK_OPEN) - 1) == 0) {
                in_think = true;
                p += (sizeof(THINK_OPEN) - 1) - 1; /* loop ++p eats the last */
                continue;
            }
            out[o++] = *p;
        } else if (strncmp(p, THINK_CLOSE, sizeof(THINK_CLOSE) - 1) == 0) {
            in_think = false;
            p += (sizeof(THINK_CLOSE) - 1) - 1;
            /* A block removed mid-sentence leaves " .  " behind; collapse the
             * doubled space so the spoken/rendered text reads naturally. */
            if (o > 0 && out[o - 1] == ' ' && p[1] == ' ') {
                p++;
            }
        }
        /* else: inside a reasoning block, drop the byte */
    }
    out[o] = '\0';

    /* The template leaves a blank line where the block was. */
    size_t lead = 0;
    while (out[lead] == '\n' || out[lead] == ' ' || out[lead] == '\t') {
        lead++;
    }
    if (lead > 0) {
        memmove(out, out + lead, o - lead + 1);
        o -= lead;
    }
    return o;
}

bool robodog_think_present(const char *raw)
{
    return raw != NULL && strstr(raw, THINK_OPEN) != NULL;
}
